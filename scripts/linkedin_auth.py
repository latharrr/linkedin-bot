"""LinkedIn OAuth (SPEC §3.4) — run on your laptop, ~every 60 days.

    python scripts/linkedin_auth.py

Opens the LinkedIn consent page, catches the redirect on localhost, exchanges
the code, looks up your member id once, and writes the three `settings` keys:
linkedin_access_token, linkedin_token_issued_at, linkedin_author_urn.
The token is never printed or logged.

Requires LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET / LINKEDIN_REDIRECT_URI in
.env, and that exact redirect URI registered on the LinkedIn app (Auth tab).
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.linkedin import LinkedInClient  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPES = "w_member_social openid profile"
WAIT_SECONDS = 300


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "scope": SCOPES}
    return f"{AUTHORIZE}?{urlencode(query)}"


def auth_hint(error: str) -> str:
    """Plain-language fix for the errors LinkedIn app setup usually causes."""
    if error == "unauthorized_scope_error":
        return (
            "\n\nFix: in your app at developer.linkedin.com → Products, add BOTH\n"
            "  • 'Sign In with LinkedIn using OpenID Connect'  (grants openid + profile)\n"
            "  • 'Share on LinkedIn'                           (grants w_member_social)\n"
            "Both are self-serve and usually approved instantly. Then run this script again."
        )
    if error in ("invalid_redirect_uri", "redirect_uri_mismatch") or "redirect" in error:
        return "\n\nFix: add exactly http://localhost:8765/callback under your app's Auth tab → Authorized redirect URLs."
    if error == "user_cancelled_authorize":
        return "\n\nYou clicked Cancel on LinkedIn's page. Run the script again and click Allow."
    return ""


def wait_for_code(redirect_uri: str, expected_state: str) -> str:
    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.port:
        sys.exit("LINKEDIN_REDIRECT_URI must be http://localhost:<port>/<path> for this script.")
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            result.update(params)
            ok = params.get("state") == expected_state and "code" in params
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"LinkedIn connected. You can close this tab." if ok else b"Authorization failed. Check the terminal.")

        def log_message(self, *args: object) -> None:  # keep the code out of logs
            return

    server = HTTPServer((parsed.hostname, parsed.port), Handler)
    server.timeout = 1
    done = threading.Event()

    def serve() -> None:
        while not done.is_set() and "code" not in result and "error" not in result:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    thread.join(WAIT_SECONDS)
    done.set()
    server.server_close()
    if "error" in result:
        sys.exit(f"LinkedIn returned an error: {result.get('error')}: {result.get('error_description', '')}{auth_hint(result.get('error', ''))}")
    if result.get("state") != expected_state:
        sys.exit("State mismatch or no redirect received — aborting (possible CSRF or timeout).")
    return result["code"]


async def exchange_and_store(code: str) -> None:
    s = get_settings()
    async with httpx.AsyncClient(timeout=s.http_timeout) as http:
        resp = await http.post(
            TOKEN,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": s.linkedin_redirect_uri,
                "client_id": s.linkedin_client_id,
                "client_secret": s.linkedin_client_secret.get_secret_value(),
            },
        )
        if resp.status_code != 200:
            sys.exit(f"Token exchange failed: HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        token, expires_in = body["access_token"], int(body.get("expires_in", 0))
        sub = await LinkedInClient(http, token, s.linkedin_api_version).userinfo_sub()
    db = await Database.connect(s.supabase_url, s.supabase_service_key.get_secret_value(), s.supabase_bucket, s.http_timeout)
    await db.set_setting("linkedin_access_token", token)
    await db.set_setting("linkedin_token_issued_at", utcnow().isoformat())
    await db.set_setting("linkedin_author_urn", f"urn:li:person:{sub}")
    days = expires_in // 86400 if expires_in else "?"
    print(f"Saved token, issue date and author URN (urn:li:person:{sub}). Token valid for ~{days} days.")


def main() -> None:
    s = get_settings()
    if not s.linkedin_client_id or not s.linkedin_client_secret.get_secret_value():
        sys.exit("Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first.")
    state = secrets.token_urlsafe(24)
    url = authorize_url(s.linkedin_client_id, s.linkedin_redirect_uri, state)
    print(f"Opening LinkedIn consent page. If it doesn't open, visit:\n\n{url}\n")
    webbrowser.open(url)
    code = wait_for_code(s.linkedin_redirect_uri, state)
    asyncio.run(exchange_and_store(code))


if __name__ == "__main__":
    main()
