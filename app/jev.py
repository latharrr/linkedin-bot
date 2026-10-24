"""Jev (TypeSafe AI System One model): calibrated yes/no decisions, not text.

Used as a second opinion when the CHAT model decides on its own to start drafts
("yes go for it", "draft that"). Explicit "make a post about …" requests skip it.
Fails open: on a rate limit, timeout or error the chat model's decision stands.
Starting drafts never publishes anything, so failing open costs at most one run.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.log import get_logger
from app.usage import call_row

log = get_logger(__name__)

URL = "https://www.jevai.org/api/v1/decisions"
MODEL = "typesafe-ai/jev"
THRESHOLD = 0.5
TIMEOUT = 8.0
QUESTION = (
    "Does the latest user message ask for LinkedIn post drafts to be written right now "
    "(an explicit request, or a yes to a post idea just proposed)? Greetings, questions, "
    "brainstorming and requests to publish an existing draft are no."
)


class JevGate:
    def __init__(self, http: httpx.AsyncClient, api_key: str, timeout: float = TIMEOUT) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout
        self.recorder: Any = None

    async def wants_drafts(self, history: list[dict[str, str]]) -> float | None:
        """P(the last user message asks for drafts now), or None if Jev couldn't answer."""
        *earlier, last = history
        body = {
            "model": MODEL,
            "state": {"recent_conversation": earlier[-6:], "latest_user_message": last["content"]},
            "questions": {"wants_drafts": {"type": "noul", "instructions": QUESTION}},
        }
        resp: httpx.Response | None = None
        try:
            resp = await self._http.post(URL, json=body, headers=self._headers, timeout=self._timeout)
            p = float(resp.json()["data"]["answers"]["wants_drafts"]["noul"]) if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("jev_unavailable", extra={"error": type(exc).__name__})
            p = None
        finally:
            if self.recorder is not None:
                try:
                    self.recorder(call_row("jev", "decisions", MODEL, resp, None if resp is not None else RuntimeError()))
                except Exception:
                    log.warning("usage_record_skipped", extra={"endpoint": "jev"})
        if p is None and resp is not None:
            log.warning("jev_unavailable", extra={"status": resp.status_code})
        return p
