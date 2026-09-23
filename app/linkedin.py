"""LinkedIn: little-text escaping (A1), x-restli-id → URL, and the REST client (SPEC §6, §7)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

import httpx

# ── Little text (A1) ─────────────────────────────────────────────────────────
# `commentary` is parsed as LinkedIn "little text": these characters are
# reserved and must be backslash-escaped or the post is rejected / mangled.
RESERVED = frozenset("\\|{}@[]()<>#*_~")

# Hashtag = '#' not preceded by a word char or '#', followed by letters/digits
# containing at least one letter ("#1" is "number one", not a tag). Underscore
# ends the tag (it is reserved and would need escaping inside the template).
# URLs are matched first so "site.com/#anchor" is never turned into a hashtag.
_URL_OR_TAG = re.compile(
    r"(?P<url>https?://\S+)"
    r"|(?<![\w#])#(?P<tag>[^\W_]*[^\W\d_][^\W_]*)"
)


def _escape_plain(text: str) -> str:
    return "".join("\\" + ch if ch in RESERVED else ch for ch in text)


def escape_little_text(text: str) -> str:
    """Escape reserved chars; convert #word into the clickable {hashtag|\\#|word} template."""
    out: list[str] = []
    pos = 0
    for m in _URL_OR_TAG.finditer(text):
        out.append(_escape_plain(text[pos : m.start()]))
        if m.group("tag") is not None:
            out.append("{hashtag|\\#|" + m.group("tag") + "}")
        else:
            out.append(_escape_plain(m.group("url")))
        pos = m.end()
    out.append(_escape_plain(text[pos:]))
    return "".join(out)


# ── Post URL ─────────────────────────────────────────────────────────────────
def post_url_from_restli_id(header_value: str | None) -> str:
    """x-restli-id header (e.g. 'urn:li:share:123', possibly %-encoded) → public feed URL."""
    urn = unquote((header_value or "").strip())
    if not urn.startswith("urn:li:"):
        raise ValueError(f"unexpected x-restli-id: {header_value!r}")
    return f"https://www.linkedin.com/feed/update/{urn}"


_POST_URL = re.compile(r"^https://(www\.)?linkedin\.com/\S+$", re.I)


def looks_like_post_url(text: str) -> bool:
    return bool(_POST_URL.match((text or "").strip()))


# ── REST client ──────────────────────────────────────────────────────────────
API = "https://api.linkedin.com"


class LinkedInError(RuntimeError):
    """Base. Subclasses encode whether the post could already be live."""


class ImageUploadError(LinkedInError):
    """Image download/init/PUT failed. Nothing was posted → safe to retry."""


class PostRejected(LinkedInError):
    """POST /rest/posts definitely did not publish (4xx, or the request never
    reached LinkedIn) → safe to retry."""


class PostMaybeLive(LinkedInError):
    """Timeout / 5xx / broken response on POST /rest/posts: it may be live.
    Never retried automatically; a human confirms via Live ✓ / Not live."""


class AuthLookupError(LinkedInError):
    """/v2/userinfo failed (usually an expired token)."""


# Errors raised before any bytes reach LinkedIn: the request was never sent.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _describe(resp: httpx.Response) -> str:
    detail = resp.text[:300].replace("\n", " ")
    hint = " (token expired or revoked? run scripts/linkedin_auth.py)" if resp.status_code == 401 else ""
    return f"HTTP {resp.status_code}{hint}: {detail}"


def build_post_payload(author_urn: str, text: str, image_urn: str, title: str) -> dict[str, Any]:
    """Single-image post body. `text` is escaped here so no caller can forget."""
    return {
        "author": author_urn,
        "commentary": escape_little_text(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"media": {"id": image_urn, "title": title[:100]}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


class LinkedInClient:
    def __init__(self, http: httpx.AsyncClient, access_token: str, api_version: str) -> None:
        self._http = http
        self._token = access_token
        self._version = api_version

    def _headers(self, rest: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if rest:  # required on every /rest/ call
            headers |= {"LinkedIn-Version": self._version, "X-Restli-Protocol-Version": "2.0.0"}
        return headers

    async def userinfo_sub(self) -> str:
        try:
            resp = await self._http.get(f"{API}/v2/userinfo", headers=self._headers(rest=False))
        except httpx.HTTPError as exc:
            raise AuthLookupError(f"userinfo: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise AuthLookupError(f"userinfo: {_describe(resp)}")
        try:
            sub = resp.json().get("sub")
        except ValueError:
            sub = None
        if not sub:
            raise AuthLookupError("userinfo: response has no 'sub'")
        return str(sub)

    async def init_upload(self, owner_urn: str) -> tuple[str, str]:
        """→ (uploadUrl, image URN)."""
        body = {"initializeUploadRequest": {"owner": owner_urn}}
        try:
            resp = await self._http.post(f"{API}/rest/images?action=initializeUpload", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ImageUploadError(f"initializeUpload: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise ImageUploadError(f"initializeUpload: {_describe(resp)}")
        try:
            value = resp.json()["value"]
            return str(value["uploadUrl"]), str(value["image"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ImageUploadError("initializeUpload: unexpected response shape") from exc

    async def put_image(self, upload_url: str, data: bytes, content_type: str) -> None:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": content_type}
        try:
            resp = await self._http.put(upload_url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise ImageUploadError(f"image PUT: {type(exc).__name__}") from exc
        if resp.status_code >= 300:
            raise ImageUploadError(f"image PUT: {_describe(resp)}")

    async def create_post(self, payload: dict[str, Any]) -> str:
        """POST /rest/posts → x-restli-id. Raises PostRejected or PostMaybeLive."""
        try:
            resp = await self._http.post(f"{API}/rest/posts", json=payload, headers=self._headers())
        except _NOT_SENT as exc:
            raise PostRejected(f"posts: {type(exc).__name__} (request never reached LinkedIn)") from exc
        except httpx.HTTPError as exc:  # read/write timeout, dropped connection…
            raise PostMaybeLive(f"posts: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise PostMaybeLive(f"posts: {_describe(resp)}")
        if resp.status_code >= 400:
            raise PostRejected(f"posts: {_describe(resp)}")
        restli_id = resp.headers.get("x-restli-id")
        if not restli_id:
            raise PostMaybeLive(f"posts: HTTP {resp.status_code} but no x-restli-id header")
        return restli_id
