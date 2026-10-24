import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extract_voice import ask_current_role, finalize_profile  # noqa: E402
from linkedin_auth import authorize_url  # noqa: E402
from seed_past_posts import SeedPost, parse_seed_lines  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_parse_seed_lines():
    lines = [
        '{"text": " Post one ", "posted_at": "2025-11-03", "likes": 120, "comments": 14}',
        "",
        '{"text": "Post two", "posted_at": "2025-10-01T09:00:00Z", "likes": 5}',
        '{"text": "Post three"}',
    ]
    assert parse_seed_lines(lines) == [
        SeedPost("Post one", "2025-11-03", 134),
        SeedPost("Post two", "2025-10-01", 5),
        SeedPost("Post three", None, 0),
    ]


@pytest.mark.parametrize(
    "bad",
    ['{"likes": 1}', "not json", '{"text": ""}', '{"text": "x", "likes": -1}', '{"text": "x", "posted_at": "yesterday"}', '{"text": "x", "likes": "many"}'],
)
def test_parse_seed_lines_rejects(bad):
    with pytest.raises(ValueError, match="line 1"):
        parse_seed_lines([bad])


def test_role_is_asked_never_guessed():
    answers = iter(["", "  ", "Founder, Example Labs"])
    assert ask_current_role(None, lambda _: next(answers)) == "Founder, Example Labs"


def test_role_enter_keeps_existing_or_replaces():
    assert ask_current_role("CTO, X", lambda _: "") == "CTO, X"
    assert ask_current_role("CTO, X", lambda _: "CEO, Y") == "CEO, Y"


def test_model_guess_for_role_is_overwritten():
    assert finalize_profile({"current_role": "<ask the user, do not guess>", "tone": "dry"}, "Founder")["current_role"] == "Founder"


def test_authorize_url_matches_spec():
    q = parse_qs(urlparse(authorize_url("cid", "http://localhost:8765/callback", "st8")).query)
    assert q == {
        "response_type": ["code"], "client_id": ["cid"], "redirect_uri": ["http://localhost:8765/callback"],
        "state": ["st8"], "scope": ["w_member_social openid profile"],
    }


def test_crontab_matches_spec():
    cron = (ROOT / "deploy" / "crontab.txt").read_text()
    assert "CRON_TZ=Asia/Kolkata" in cron
    for schedule, cmd in [("*/5 * * * *", "dispatcher.py"), ("0 8 * * *", "bot.py --nudge"), ("0 14 * * *", "bot.py --fallback"), ("0 9 * * 1", "bot.py --token-check")]:
        assert any(line.startswith(schedule) and cmd in line for line in cron.splitlines()), cmd


# ── Bright Data scraper (scripts/scrape_linkedin_posts.py) ───────────────────
import httpx  # noqa: E402
from scrape_linkedin_posts import ScrapeError, best_posts, scrape, to_seed  # noqa: E402


def test_to_seed_maps_fields_and_skips_reposts_errors_empty():
    assert to_seed({"post_text": " Hi ", "date_posted": "2025-11-03T09:00:00Z", "num_likes": "1,204", "num_comments": 31}) == {
        "text": "Hi", "posted_at": "2025-11-03", "likes": 1204, "comments": 31, "url": None,
    }
    assert to_seed({"text": "alt names", "likes": 5, "comments": [{}, {}]})["comments"] == 2
    assert to_seed({"post_text": "x", "post_type": "repost"}) is None
    assert to_seed({"error": "private profile"}) is None
    assert to_seed({"post_text": "   "}) is None


def test_best_posts_ranks_by_engagement_and_dedupes():
    recs = [{"post_text": "a", "num_likes": 1}, {"post_text": "b", "num_likes": 50, "num_comments": 5},
            {"post_text": "b", "num_likes": 50}, {"post_text": "c", "num_likes": 10}]
    assert [p["text"] for p in best_posts(recs, 2)] == ["b", "c"]


def test_scrape_trigger_poll_download():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/trigger"):
            assert req.url.params["discover_by"] == "profile_url" and req.url.params["limit_per_input"] == "50"
            body = __import__("json").loads(req.content)
            assert body == [{"url": "https://www.linkedin.com/in/me/", "only_authored_posts": True, "start_date": "2025-01-01T00:00:00.000Z"}]
            return httpx.Response(200, json={"snapshot_id": "s_1"})
        if "/progress/" in req.url.path:
            status = "running" if sum(1 for c in calls if "/progress/" in c[1]) == 1 else "ready"
            return httpx.Response(200, json={"status": status})
        return httpx.Response(200, json=[{"post_text": "p1", "num_likes": 3}])

    records = scrape(httpx.Client(transport=httpx.MockTransport(handler)), "k", "https://www.linkedin.com/in/me/", 50, "2025-01-01", sleep=lambda s: None)
    assert records == [{"post_text": "p1", "num_likes": 3}]
    assert [c[1].rsplit("/", 1)[-1] for c in calls] == ["trigger", "s_1", "s_1", "s_1"]


def test_scrape_inactive_account_explains_itself():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="Customer is not active")))
    with pytest.raises(ScrapeError, match="activate the Bright Data account"):
        scrape(client, "k", "https://www.linkedin.com/in/me/", 10, None, sleep=lambda s: None)


def test_scrape_failed_snapshot():
    def handler(req):
        if req.url.path.endswith("/trigger"):
            return httpx.Response(200, json={"snapshot_id": "s_2"})
        return httpx.Response(200, json={"status": "failed"})

    with pytest.raises(ScrapeError, match="failed"):
        scrape(httpx.Client(transport=httpx.MockTransport(handler)), "k", "u", 10, None, sleep=lambda s: None)


def test_only_the_profile_owners_posts_are_kept():
    from scrape_linkedin_posts import profile_slug

    assert profile_slug("https://www.linkedin.com/in/DeepanshuLathar/?trk=x") == "deepanshulathar"
    recs = [
        {"post_text": "mine", "user_id": "deepanshulathar", "num_likes": 3},
        {"post_text": "someone else's viral post", "user_id": "drheenakhanna", "num_likes": 155},
    ]
    assert [p["text"] for p in best_posts(recs, 10, author="deepanshulathar")] == ["mine"]


# ── LinkedIn data-export importer ────────────────────────────────────────────
import zipfile  # noqa: E402

from import_linkedin_export import (  # noqa: E402
    apply_enrichment,
    merge,
    read_shares,
    text_key,
)

SHARES = (
    "Date,ShareLink,ShareCommentary,SharedUrl,MediaUrl,Visibility\n"
    '2026-01-25 09:00:00,https://www.linkedin.com/feed/update/urn:li:share:1,"Day 1 of my challenge.\nLine two, with a comma.",,,MEMBER_NETWORK\n'
    "2026-01-26 09:00:00,https://www.linkedin.com/feed/update/urn:li:share:2,,,,MEMBER_NETWORK\n"
    '2026-02-23 09:00:00,https://www.linkedin.com/feed/update/urn:li:share:3,"For the last 30 days, I treated my LinkedIn like a startup MVP.",,,MEMBER_NETWORK\n'
)


def test_read_shares_csv_and_zip(tmp_path):
    csv_path = tmp_path / "Shares.csv"
    csv_path.write_text(SHARES, encoding="utf-8")
    posts = read_shares(csv_path)
    assert [p["posted_at"] for p in posts] == ["2026-01-25", "2026-02-23"]  # pure reshare (empty text) skipped
    assert posts[0]["text"] == "Day 1 of my challenge.\nLine two, with a comma."
    zpath = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Shares.csv", SHARES)
    assert read_shares(zpath) == posts


def test_merge_reuses_scraped_text_so_reseeding_never_duplicates():
    scraped_text = "𝐅𝐨𝐫 𝐭𝐡𝐞 𝐥𝐚𝐬𝐭 𝟑𝟎 𝐝𝐚𝐲𝐬, 𝐈 𝐭𝐫𝐞𝐚𝐭𝐞𝐝 𝐦𝐲 𝐋𝐢𝐧𝐤𝐞𝐝𝐈𝐧​ like a startup MVP."
    known = {text_key(scraped_text): {"text": scraped_text, "posted_at": "2026-02-23", "likes": 10, "comments": 4}}
    export = [
        {"text": "For the last 30 days, I treated my LinkedIn like a startup MVP.", "posted_at": "2026-02-23", "link": "l3"},
        {"text": "Day 1 of my challenge.", "posted_at": "2026-01-25", "link": "l1"},
    ]
    known[text_key("KPMG internship announcement")] = {"text": "KPMG internship announcement", "posted_at": None, "likes": 387, "comments": 9}
    merged, missing = merge(export, known)
    assert merged[0] == {"text": scraped_text, "posted_at": "2026-02-23", "likes": 10, "comments": 4}
    assert [m["link"] for m in missing] == ["l1"] and missing[0]["likes"] == 0
    assert all("KPMG" not in m["text"] for m in merged)  # someone else's scraped post never enters your corpus


def test_enrichment_fills_engagement_by_text():
    missing = [{"text": "Day 1 of my challenge.", "posted_at": "2026-01-25", "likes": 0, "comments": 0, "link": "l1"}]
    records = [{"post_text": "Day 1 of my challenge.", "num_likes": 7, "num_comments": 2, "user_id": "me"}]
    assert apply_enrichment(missing, records) == 1
    assert (missing[0]["likes"], missing[0]["comments"]) == (7, 2)


def test_background_facts_join_the_voice_profile(tmp_path):
    from extract_voice import load_background

    f = tmp_path / "about_me.json"
    f.write_text('{"background": [{"fact": "Works at Picapool", "source": "LinkedIn"}, "Studies CSE at LPU", ""]}')
    facts = load_background(f)
    assert facts == ["Works at Picapool", "Studies CSE at LPU"]
    assert finalize_profile({"tone": "dry"}, "Founder's Office, Picapool", facts)["background"] == facts
    assert "background" not in finalize_profile({"tone": "dry"}, "x", [])
    assert load_background(tmp_path / "missing.json") == []


def test_style_rules_and_extra_samples(tmp_path):
    from extract_voice import load_extra_samples, load_style_rules

    about = tmp_path / "about.json"
    about.write_text('{"background": [], "style_rules": ["No bold-Unicode", ""]}')
    extra = tmp_path / "extra.json"
    extra.write_text('{"samples": ["I build systems that make growth traceable.", ""]}')
    assert load_style_rules(about) == ["No bold-Unicode"]
    assert load_extra_samples(extra) == ["[Long-form writing by the same author — not a LinkedIn post]\nI build systems that make growth traceable."]
    assert finalize_profile({}, "role", None, ["No bold-Unicode"])["style_rules"] == ["No bold-Unicode"]


def test_auth_hints_explain_app_setup_errors():
    from linkedin_auth import auth_hint

    assert "Sign In with LinkedIn using OpenID Connect" in auth_hint("unauthorized_scope_error")
    assert "localhost:8765/callback" in auth_hint("redirect_uri_mismatch")
    assert "Allow" in auth_hint("user_cancelled_authorize")
    assert auth_hint("something_else") == ""
