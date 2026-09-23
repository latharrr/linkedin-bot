"""Scrape YOUR past LinkedIn posts (text + likes + comments) via Bright Data,
and write them as seed_posts.jsonl for scripts/seed_past_posts.py.

    python scripts/scrape_linkedin_posts.py https://www.linkedin.com/in/<you>/
    python scripts/scrape_linkedin_posts.py <profile-url> --limit 100 --top 20 --since 2024-01-01

Uses the "LinkedIn posts — discover by profile URL" dataset (asynchronous:
trigger → poll → download). Cost is Bright Data's per-record rate (~$1.50 per
1,000 records), so --limit 100 costs at most ~$0.15. Reposts are excluded.

Writes:
  out/linkedin_posts_raw.json  every record exactly as returned (to check the mapping)
  seed_posts.jsonl             the top --top posts by likes + comments, ready to seed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

API = "https://api.brightdata.com/datasets/v3"
DATASET = "gd_lyy3tktm25m4avu764"  # LinkedIn posts — discover by profile URL
POLL_SECONDS = 10
MAX_WAIT_SECONDS = 900

# Bright Data field names vary a little between dataset versions; take the first present.
TEXT_KEYS = ("post_text", "text", "content", "description", "title")
DATE_KEYS = ("date_posted", "posted_at", "date", "created_at")
LIKE_KEYS = ("num_likes", "likes", "likes_count", "num_reactions", "reactions")
COMMENT_KEYS = ("num_comments", "comments", "comments_count")
REPOST_TYPES = {"repost", "reshare", "shared"}


class ScrapeError(RuntimeError):
    pass


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = record.get(k)
        if v not in (None, "", []):
            return v
    return None


def _count(value: Any) -> int:
    if isinstance(value, list):  # some versions return the comment objects themselves
        return len(value)
    try:
        return max(int(float(str(value).replace(",", ""))), 0)
    except (TypeError, ValueError):
        return 0


def profile_slug(profile_url: str) -> str:
    """https://www.linkedin.com/in/deepanshulathar/ → 'deepanshulathar'."""
    parts = [p for p in profile_url.split("?")[0].rstrip("/").split("/") if p]
    return parts[-1].lower() if parts else ""


def to_seed(record: dict[str, Any], author: str | None = None) -> dict[str, Any] | None:
    """Bright Data record → {text, posted_at, likes, comments}; None for reposts, errors, empty
    posts, and — when `author` is given — posts written by anyone else (your activity feed
    includes other people's posts you engaged with; they must never enter your voice corpus)."""
    if record.get("error") or str(record.get("post_type") or "").lower() in REPOST_TYPES:
        return None
    if author and str(record.get("user_id") or "").lower() != author:
        return None
    text = _first(record, TEXT_KEYS)
    if not isinstance(text, str) or not text.strip():
        return None
    date = _first(record, DATE_KEYS)
    return {
        "text": text.strip(),
        "posted_at": str(date)[:10] if date else None,
        "likes": _count(_first(record, LIKE_KEYS)),
        "comments": _count(_first(record, COMMENT_KEYS)),
        "url": record.get("url"),
    }


def best_posts(records: list[dict[str, Any]], top: int, author: str | None = None) -> list[dict[str, Any]]:
    seeds, seen = [], set()
    for rec in records:
        seed = to_seed(rec, author)
        if seed and seed["text"] not in seen:
            seen.add(seed["text"])
            seeds.append(seed)
    seeds.sort(key=lambda s: s["likes"] + s["comments"], reverse=True)
    return seeds[:top]


def _check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        hint = " — activate the Bright Data account (add a payment method)." if "not active" in resp.text.lower() else ""
        raise ScrapeError(f"{what}: HTTP {resp.status_code}: {resp.text[:200]}{hint}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ScrapeError(f"{what}: non-JSON response: {resp.text[:200]}") from exc


def scrape(
    http: httpx.Client, key: str, profile_url: str, limit: int, since: str | None, sleep: Any = time.sleep, authored_only: bool = True
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {key}"}
    item: dict[str, Any] = {"url": profile_url, "only_authored_posts": authored_only}
    if since:
        item["start_date"] = f"{since}T00:00:00.000Z"
    params = {"dataset_id": DATASET, "include_errors": "true", "type": "discover_new", "discover_by": "profile_url", "limit_per_input": limit}
    snapshot = _check(http.post(f"{API}/trigger", params=params, headers=headers, json=[item], timeout=60), "trigger").get("snapshot_id")
    if not snapshot:
        raise ScrapeError("trigger: no snapshot_id in response")
    print(f"Scrape started (snapshot {snapshot}); polling every {POLL_SECONDS}s…", flush=True)
    waited = 0
    while True:
        status = _check(http.get(f"{API}/progress/{snapshot}", headers=headers, timeout=30), "progress").get("status")
        if status == "ready":
            break
        if status == "failed":
            raise ScrapeError(f"Bright Data reported the scrape failed (snapshot {snapshot})")
        if waited >= MAX_WAIT_SECONDS:
            raise ScrapeError(f"still '{status}' after {MAX_WAIT_SECONDS}s; download later: {API}/snapshot/{snapshot}?format=json")
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    data = _check(http.get(f"{API}/snapshot/{snapshot}", params={"format": "json"}, headers=headers, timeout=120), "download")
    return data if isinstance(data, list) else [data]


def collect_by_url(http: httpx.Client, key: str, urls: list[str], sleep: Any = time.sleep) -> list[dict[str, Any]]:
    """Fetch specific posts (text + likes + comments) by their URLs — e.g. the ShareLinks
    from your LinkedIn data export, which reach posts the public profile feed no longer shows."""
    headers = {"Authorization": f"Bearer {key}"}
    params = {"dataset_id": DATASET, "include_errors": "true"}
    body = [{"url": u} for u in urls]
    snapshot = _check(http.post(f"{API}/trigger", params=params, headers=headers, json=body, timeout=60), "trigger").get("snapshot_id")
    if not snapshot:
        raise ScrapeError("trigger: no snapshot_id in response")
    print(f"Fetching {len(urls)} posts by URL (snapshot {snapshot})…", flush=True)
    waited = 0
    while (status := _check(http.get(f"{API}/progress/{snapshot}", headers=headers, timeout=30), "progress").get("status")) != "ready":
        if status == "failed":
            raise ScrapeError(f"Bright Data reported the collection failed (snapshot {snapshot})")
        if waited >= MAX_WAIT_SECONDS:
            raise ScrapeError(f"still '{status}' after {MAX_WAIT_SECONDS}s (snapshot {snapshot})")
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    data = _check(http.get(f"{API}/snapshot/{snapshot}", params={"format": "json"}, headers=headers, timeout=120), "download")
    return data if isinstance(data, list) else [data]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile_url")
    parser.add_argument("--limit", type=int, default=100, help="max records to scrape (billed per record)")
    parser.add_argument("--top", type=int, default=20, help="how many best posts to write to seed_posts.jsonl")
    parser.add_argument("--since", help="only posts on/after this date, YYYY-MM-DD")
    parser.add_argument("--out", default="seed_posts.jsonl")
    parser.add_argument("--include-reposts", action="store_true", help="also fetch your activity feed; only posts you authored are kept")
    parser.add_argument("--raw-out", default="out/linkedin_posts_raw.json")
    args = parser.parse_args()
    s = get_settings()
    if not s.brightdata_api_key:
        sys.exit("BRIGHTDATA_API_KEY is not set in .env")
    try:
        with httpx.Client() as http:
            records = scrape(
                http, s.brightdata_api_key.get_secret_value(), args.profile_url, args.limit, args.since,
                authored_only=not args.include_reposts,
            )
    except ScrapeError as exc:
        sys.exit(str(exc))
    Path("out").mkdir(exist_ok=True)
    Path(args.raw_out).write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    posts = best_posts(records, args.top, author=profile_slug(args.profile_url))
    if not posts:
        sys.exit(f"No usable posts in {len(records)} records — check out/linkedin_posts_raw.json for the field names.")
    with open(args.out, "w", encoding="utf-8") as f:
        for p in posts:
            f.write(json.dumps({k: p[k] for k in ("text", "posted_at", "likes", "comments")}, ensure_ascii=False) + "\n")
    engaged = sum(1 for p in posts if p["likes"] + p["comments"] > 0)
    print(f"\n{len(records)} records → {len(posts)} best posts written to {args.out} ({engaged} with engagement > 0):")
    for p in posts:
        print(f"  {p['posted_at'] or '?':<10} {p['likes']:>5} likes {p['comments']:>4} comments | {p['text'][:70].replace(chr(10), ' ')}")
    print(f"\nNext: .venv/bin/python scripts/seed_past_posts.py {args.out}")


if __name__ == "__main__":
    main()
