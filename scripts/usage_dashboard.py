"""Local usage dashboard: every API/resource the bot uses — consumed vs left.

    python scripts/usage_dashboard.py              # http://127.0.0.1:8787
    python scripts/usage_dashboard.py --port 9000
    python scripts/usage_dashboard.py --once       # print the JSON once and exit

Numbers come from three places, labelled on the page:
  live     — asked from the provider now (OpenRouter, Tavily, Supabase, Telegram)
  headers  — rate-limit headers on the bot's latest call (Groq)
  ledger   — the bot's own call log in Supabase (bot_api_calls, scripts/usage.sql)
  estimate — derived from the ledger where the provider exposes nothing (NVIDIA credits)

Security: provider keys are used only inside this process. The page and
/api/usage carry numbers and statuses, never keys. The server binds to
127.0.0.1 so only this machine can open it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.log import setup_logging  # noqa: E402
from app.usage import collect  # noqa: E402

PAGE = ROOT / "app" / "usage_page.html"
CACHE_SECONDS = 30


async def gather_usage() -> dict[str, Any]:
    s = get_settings()
    db = await Database.connect(s.supabase_url, s.supabase_service_key.get_secret_value(), s.supabase_bucket, s.http_timeout)
    async with httpx.AsyncClient() as http:
        return await collect(s, db, http)


class Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: dict[str, Any] | None = None

    def get(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if force or self._data is None or time.monotonic() - self._at > CACHE_SECONDS:
                self._data = asyncio.run(gather_usage())
                self._at = time.monotonic()
            return self._data


CACHE = Cache()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/usage":
            try:
                data = CACHE.get(force="refresh=1" in query)
                self._send(200, json.dumps(data).encode(), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args: object) -> None:  # quiet
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you know why (the page shows account usage)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--once", action="store_true", help="print the usage JSON once and exit")
    args = parser.parse_args()
    s = get_settings()
    setup_logging("WARNING", s.secret_values())
    if args.once:
        print(json.dumps(asyncio.run(gather_usage()), indent=2))
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Usage dashboard: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
