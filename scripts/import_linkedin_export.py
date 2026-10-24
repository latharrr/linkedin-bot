"""Import ALL your LinkedIn posts from LinkedIn's own data export, then seed them.

LinkedIn only shows recent activity publicly, so scrapers can't reach older posts.
Your data export has every post you wrote. Get it at:
  LinkedIn → Settings → Data privacy → Get a copy of your data → pick "Posts"
  (or the full archive) → LinkedIn emails a download link, usually within minutes.

    python scripts/import_linkedin_export.py ~/Downloads/Basic_LinkedInDataExport_*.zip
    python scripts/import_linkedin_export.py Shares.csv --enrich     # + likes/comments via Bright Data
    python scripts/seed_past_posts.py seed_posts.jsonl                # then load them

- The export is the authoritative list of YOUR posts. Posts already scraped
  (out/linkedin_posts_raw*.json) only lend their text + engagement to matching export
  posts (so re-seeding never duplicates); scraped records are never added on their own.
- --enrich fetches likes/comments for the rest by URL through Bright Data
  (~$1.50 per 1,000 posts). Without it they're seeded with engagement 0: still
  used for voice and few-shot retrieval, just not for the engagement weighting.
- Pure reshares (no text of your own) are skipped.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from scrape_linkedin_posts import ScrapeError, collect_by_url, to_seed  # noqa: E402

from app.config import get_settings  # noqa: E402

csv.field_size_limit(10_000_000)


def text_key(text: str) -> str:
    """Match the same post across sources despite styling/whitespace differences."""
    t = unicodedata.normalize("NFKC", text).replace("\u200b", "").lower()
    return re.sub(r"\s+", " ", t).strip()[:60]


def read_shares(path: Path) -> list[dict[str, Any]]:
    """Shares.csv (or the export .zip containing it) → [{text, posted_at, link}]."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("shares.csv")), None)
            if name is None:
                raise SystemExit(f"No Shares.csv inside {path.name}. Request the export with 'Posts' included.")
            raw = z.read(name).decode("utf-8-sig")
    else:
        raw = path.read_text(encoding="utf-8-sig")
    posts = []
    for row in csv.DictReader(io.StringIO(raw)):
        text = (row.get("ShareCommentary") or "").strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1].replace('""', '"').strip()
        if not text:
            continue  # a pure reshare: none of your own words
        posts.append({"text": text, "posted_at": (row.get("Date") or "")[:10] or None, "link": (row.get("ShareLink") or "").strip()})
    return posts


def known_posts(raw_files: list[Path]) -> dict[str, dict[str, Any]]:
    """Already-scraped posts by text key (authored ones only)."""
    known: dict[str, dict[str, Any]] = {}
    for f in raw_files:
        for rec in json.loads(f.read_text(encoding="utf-8")):
            seed = to_seed(rec)
            if seed and rec.get("user_id") and seed["text"]:
                known.setdefault(text_key(seed["text"]), seed)
    return known


def merge(export: list[dict[str, Any]], known: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """→ (merged seed posts, export posts still lacking engagement)."""
    merged, missing, seen = [], [], set()
    for post in export:
        key = text_key(post["text"])
        if key in seen:
            continue
        seen.add(key)
        hit = known.get(key)
        if hit:
            merged.append({"text": hit["text"], "posted_at": hit["posted_at"] or post["posted_at"], "likes": hit["likes"], "comments": hit["comments"]})
        else:
            entry = {"text": post["text"], "posted_at": post["posted_at"], "likes": 0, "comments": 0, "link": post["link"]}
            merged.append(entry)
            missing.append(entry)
    # Scraped posts are only used to enrich posts that are IN your export. They are never
    # added on their own: activity-feed scrapes include other people's posts.
    return merged, missing


def apply_enrichment(missing: list[dict[str, Any]], records: list[dict[str, Any]]) -> int:
    by_key = {text_key(s["text"]): s for s in (to_seed(r) for r in records) if s}
    filled = 0
    for entry in missing:
        hit = by_key.get(text_key(entry["text"]))
        if hit:
            entry.update(text=hit["text"], likes=hit["likes"], comments=hit["comments"])
            filled += 1
    return filled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export", type=Path, help="LinkedIn export .zip or its Shares.csv")
    parser.add_argument("--enrich", action="store_true", help="fetch likes/comments for posts not yet scraped (Bright Data)")
    parser.add_argument("--out", default="seed_posts.jsonl")
    args = parser.parse_args()
    export = read_shares(args.export)
    known = known_posts(sorted(Path("out").glob("linkedin_posts_raw*.json")))
    merged, missing = merge(export, known)
    print(f"Export: {len(export)} posts with your own text · already scraped: {len(merged) - len(missing)} · new: {len(missing)}")
    if args.enrich and missing:
        s = get_settings()
        if not s.brightdata_api_key:
            sys.exit("--enrich needs BRIGHTDATA_API_KEY in .env")
        urls = [m["link"] for m in missing if m.get("link")]
        try:
            with httpx.Client() as http:
                records = collect_by_url(http, s.brightdata_api_key.get_secret_value(), urls)
        except ScrapeError as exc:
            sys.exit(str(exc))
        Path("out").mkdir(exist_ok=True)
        Path("out/linkedin_posts_raw_export.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Engagement filled for {apply_enrichment(missing, records)} of {len(missing)} new posts.")
    merged.sort(key=lambda p: p["likes"] + p["comments"], reverse=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for p in merged:
            f.write(json.dumps({k: p[k] for k in ("text", "posted_at", "likes", "comments")}, ensure_ascii=False) + "\n")
    print(f"Wrote {len(merged)} posts to {args.out}. Next: .venv/bin/python scripts/seed_past_posts.py {args.out}")


if __name__ == "__main__":
    main()
