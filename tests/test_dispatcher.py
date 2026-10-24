import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.linkedin import ImageUploadError, PostMaybeLive, PostRejected
from app.telegram_ui import stuck_keyboard
from dispatcher import MAYBE_LIVE, Dispatcher
from tests.conftest import CHAT_ID
from tests.fakes import FakeDB, FakeMessenger, png_bytes

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
AUTHOR = "urn:li:person:abc123"


class FakeLinkedIn:
    def __init__(self, create_exc: Exception | None = None, init_exc: Exception | None = None, restli: str = "urn:li:share:777"):
        self.create_exc, self.init_exc, self.restli = create_exc, init_exc, restli
        self.creates: list[dict] = []
        self.puts: list[tuple[str, int, str]] = []
        self.userinfo_calls = 0

    async def userinfo_sub(self) -> str:
        self.userinfo_calls += 1
        return "abc123"

    async def init_upload(self, owner: str):
        await asyncio.sleep(0)  # yield so concurrent workers interleave
        if self.init_exc:
            raise self.init_exc
        return "https://upload.example/u1", "urn:li:image:img1"

    async def put_image(self, url: str, data: bytes, content_type: str) -> None:
        self.puts.append((url, len(data), content_type))

    async def create_post(self, payload: dict) -> str:
        await asyncio.sleep(0)
        self.creates.append(payload)
        if self.create_exc:
            raise self.create_exc
        return self.restli


class FakeEmbedder:
    async def embed_passage(self, text: str) -> list[float]:
        return [0.1] * 2048


async def fetch_png(url: str) -> bytes:
    return png_bytes((1080, 1350))


def make(db, li, messenger=None, dry_run=False, fetch=fetch_png):
    return Dispatcher(db, FakeEmbedder(), messenger or FakeMessenger(), CHAT_ID, li, fetch, dry_run)


@pytest.fixture
def db():
    d = FakeDB()
    d.settings["linkedin_author_urn"] = AUTHOR
    return d


def queued(db, **kw):
    fields = dict(status="queued", chosen="a", scheduled_at=NOW - timedelta(minutes=1), draft_a="Hello #AI world (v1)")
    fields.update(kw)
    return db.add_post(**fields)


# ── claim-before-post ────────────────────────────────────────────────────────
async def test_second_claim_fails(db):
    post = queued(db)
    assert await db.claim_post(post.id, NOW) is True
    assert await db.claim_post(post.id, NOW) is False
    assert db.posts[post.id]["status"] == "posting"


async def test_two_concurrent_dispatchers_post_once(db):
    post = queued(db)
    li = FakeLinkedIn()
    seen_queued = []
    real_due = db.due_posts

    async def spy(now, limit=5):
        rows = await real_due(now, limit)
        seen_queued.append(len(rows))
        return rows

    db.due_posts = spy
    r1, r2 = await asyncio.gather(make(db, li).run(NOW), make(db, li).run(NOW))
    assert seen_queued == [1, 1]  # both workers selected the row before either claimed it
    assert len(li.creates) == 1
    assert r1["posted"] + r2["posted"] == 1 and r1["lost_claim"] + r2["lost_claim"] == 1
    assert db.posts[post.id]["status"] == "posted"


# ── success ──────────────────────────────────────────────────────────────────
async def test_success_posts_escaped_payload_and_records_url(db):
    post = queued(db)
    li, m = FakeLinkedIn(), FakeMessenger()
    report = await make(db, li, m).run(NOW)
    row = db.posts[post.id]
    assert report["posted"] == 1
    assert row["status"] == "posted" and row["posted_at"] == NOW
    assert row["post_url"] == "https://www.linkedin.com/feed/update/urn:li:share:777"
    assert row["image_urn"] == "urn:li:image:img1"
    payload = li.creates[0]
    assert payload["author"] == AUTHOR
    assert payload["commentary"] == "Hello {hashtag|\\#|AI} world \\(v1\\)"
    assert payload["content"]["media"]["id"] == "urn:li:image:img1"
    assert (payload["lifecycleState"], payload["visibility"]) == ("PUBLISHED", "PUBLIC")
    assert li.puts == [("https://upload.example/u1", li.puts[0][1], "image/png")]
    assert li.userinfo_calls == 0  # URN read from settings
    assert m.texts()[0].startswith("Posted ✓ https://www.linkedin.com/feed/update/urn:li:share:777\n\nThe next 60-90 minutes")


async def test_posts_the_chosen_draft_and_image(db):
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return png_bytes((10, 10))

    queued(db, chosen="b", draft_b="The B draft", image_b_url="https://storage.example/b.png")
    li = FakeLinkedIn()
    await make(db, li, fetch=fetch).run(NOW)
    assert li.creates[0]["commentary"] == "The B draft" and fetched == ["https://storage.example/b.png"]


async def test_edited_chosen_draft_admitted_to_corpus(db):
    post = queued(db, chosen="a", edited_a=True)
    await make(db, FakeLinkedIn()).run(NOW)
    past_id = db.posts[post.id]["past_post_id"]
    assert db.past[past_id]["source"] == "ai" and db.past[past_id]["text"] == "Hello #AI world (v1)"


async def test_unedited_chosen_draft_not_admitted(db):
    post = queued(db, chosen="a", edited_b=True)  # edited the OTHER draft
    await make(db, FakeLinkedIn()).run(NOW)
    assert db.past == {} and db.posts[post.id].get("past_post_id") is None


async def test_author_urn_fetched_once_when_missing(db):
    db.settings.pop("linkedin_author_urn")
    li = FakeLinkedIn()
    queued(db)
    await make(db, li).run(NOW)
    assert li.userinfo_calls == 1 and db.settings["linkedin_author_urn"] == AUTHOR
    await make(db, li).run(NOW)
    assert li.userinfo_calls == 1


async def test_not_due_and_not_queued_rows_untouched(db):
    future = queued(db, scheduled_at=NOW + timedelta(minutes=5))
    waiting = db.add_post(status="awaiting_choice", chosen="a", scheduled_at=NOW - timedelta(hours=1))
    li = FakeLinkedIn()
    await make(db, li).run(NOW)
    assert li.creates == []
    assert db.posts[future.id]["status"] == "queued" and db.posts[waiting.id]["status"] == "awaiting_choice"


async def test_batch_limit_and_order(db):
    ids = [queued(db, scheduled_at=NOW - timedelta(minutes=10 - i), draft_a=f"post {i}").id for i in range(7)]
    li = FakeLinkedIn()
    report = await make(db, li).run(NOW)
    assert report["posted"] == 5
    assert [c["commentary"] for c in li.creates] == [f"post {i}" for i in range(5)]
    assert db.posts[ids[6]]["status"] == "queued"


# ── failure split ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "li",
    [
        FakeLinkedIn(create_exc=PostRejected("posts: HTTP 422: bad")),
        FakeLinkedIn(init_exc=ImageUploadError("initializeUpload: HTTP 403")),
    ],
    ids=["4xx", "upload"],
)
async def test_definite_failure_requeues_with_retry_count(db, li):
    post = queued(db)
    m = FakeMessenger()
    report = await make(db, li, m).run(NOW)
    row = db.posts[post.id]
    assert report["retry"] == 1
    assert (row["status"], row["retry_count"], row["claimed_at"]) == ("queued", 1, None)
    assert row["error"]
    assert "attempt 1/2" in m.texts()[0] and "Retrying" in m.texts()[0]


async def test_second_failure_marks_failed(db):
    post = queued(db, retry_count=1)
    m = FakeMessenger()
    report = await make(db, FakeLinkedIn(create_exc=PostRejected("posts: HTTP 400")), m).run(NOW)
    assert db.posts[post.id]["status"] == "failed" and db.posts[post.id]["retry_count"] == 2
    assert report["failed"] == 1
    assert "attempt 2/2" in m.texts()[0] and "Marked failed" in m.texts()[0]


async def test_retry_then_fail_across_two_runs(db):
    post = queued(db)
    li = FakeLinkedIn(create_exc=PostRejected("posts: HTTP 400"))
    await make(db, li).run(NOW)
    assert db.posts[post.id]["status"] == "queued"
    await make(db, li).run(NOW + timedelta(minutes=5))
    assert db.posts[post.id]["status"] == "failed"
    assert len(li.creates) == 2


@pytest.mark.parametrize("exc", [PostMaybeLive("posts: ReadTimeout"), PostMaybeLive("posts: HTTP 503")], ids=["timeout", "5xx"])
async def test_maybe_live_stays_posting_alerts_once_never_retries(db, exc):
    post = queued(db)
    li, m = FakeLinkedIn(create_exc=exc), FakeMessenger()
    report = await make(db, li, m).run(NOW)
    row = db.posts[post.id]
    assert report["maybe_live"] == 1
    assert row["status"] == "posting" and row["alerted_at"] == NOW and row["retry_count"] == 0
    assert m.texts()[0].startswith(MAYBE_LIVE.format(brief=post.brief))
    assert m.keyboards() == [stuck_keyboard(post.id)]
    # later runs: not re-posted, not re-alerted
    m2 = FakeMessenger()
    await make(db, li, m2).run(NOW + timedelta(hours=1))
    assert len(li.creates) == 1 and m2.sent == []


async def test_non_png_image_is_retryable_upload_failure(db):
    async def webp(url):
        return b"RIFF\x00\x00\x00\x00WEBPVP8 "

    post = queued(db)
    li = FakeLinkedIn()
    await make(db, li, fetch=webp).run(NOW)
    assert db.posts[post.id]["status"] == "queued" and "not PNG/JPEG" in db.posts[post.id]["error"]
    assert li.creates == []


async def test_image_download_error_is_retryable(db):
    async def broken(url):
        raise ConnectionError("storage down")

    post = queued(db)
    await make(db, FakeLinkedIn(), fetch=broken).run(NOW)
    assert db.posts[post.id]["status"] == "queued" and db.posts[post.id]["retry_count"] == 1


async def test_bad_restli_id_goes_to_maybe_live(db):
    post = queued(db)
    await make(db, FakeLinkedIn(restli="garbage")).run(NOW)
    assert db.posts[post.id]["status"] == "posting" and db.posts[post.id]["alerted_at"] == NOW


async def test_missing_token_fails_retryably_without_calling_linkedin(db):
    post = queued(db)
    m = FakeMessenger()
    await make(db, None, m).run(NOW)
    assert db.posts[post.id]["status"] == "queued" and "linkedin_auth.py" in db.posts[post.id]["error"]


async def test_unexpected_crash_leaves_row_posting_for_stuck_check(db):
    class Boom(FakeLinkedIn):
        async def put_image(self, *a):
            raise KeyError("bug")

    post = queued(db)
    report = await make(db, Boom()).run(NOW)
    assert report["crashed"] == 1 and db.posts[post.id]["status"] == "posting"


# ── stuck rows (A2 buttons) ──────────────────────────────────────────────────
async def test_stuck_row_alerted_once_with_live_buttons(db):
    stuck = db.add_post(status="posting", claimed_at=NOW - timedelta(minutes=16), brief="my brief")
    fresh = db.add_post(status="posting", claimed_at=NOW - timedelta(minutes=10))
    m = FakeMessenger()
    li = FakeLinkedIn()
    report = await make(db, li, m).run(NOW)
    assert report["stuck_alerted"] == 1
    assert m.texts() == [MAYBE_LIVE.format(brief="my brief")]
    assert m.keyboards() == [stuck_keyboard(stuck.id)]
    assert db.posts[stuck.id]["alerted_at"] == NOW and db.posts[stuck.id]["status"] == "posting"
    assert db.posts[fresh.id].get("alerted_at") is None
    assert li.creates == []  # never auto-retried
    m2 = FakeMessenger()
    await make(db, li, m2).run(NOW + timedelta(minutes=5))
    assert m2.sent == []


# ── dry run ──────────────────────────────────────────────────────────────────
async def test_dry_run_logs_payload_and_changes_nothing(db):
    post = queued(db)
    stuck = db.add_post(status="posting", claimed_at=NOW - timedelta(hours=1))
    before = {k: dict(v) for k, v in db.posts.items()}
    li, m = FakeLinkedIn(), FakeMessenger()
    d = make(db, li, m, dry_run=True)
    report = await d.run(NOW)
    assert report["dry_run"] == 1
    assert d.payloads[0]["commentary"] == "Hello {hashtag|\\#|AI} world \\(v1\\)"
    assert d.payloads[0]["author"] == AUTHOR
    assert li.creates == [] and li.puts == [] and li.userinfo_calls == 0
    assert db.posts == before and m.sent == []
    assert db.posts[post.id]["status"] == "queued"  # never claimed
    assert db.posts[stuck.id].get("alerted_at") is None  # stuck row only logged


async def test_dry_run_without_urn_never_calls_userinfo(db):
    db.settings.clear()
    queued(db)
    li = FakeLinkedIn()
    d = make(db, li, dry_run=True)
    await d.run(NOW)
    assert li.userinfo_calls == 0 and d.payloads[0]["author"] == "urn:li:person:DRY_RUN"


async def test_empty_chosen_draft_is_never_published(db):
    post = queued(db, chosen="b", draft_b="")
    li = FakeLinkedIn()
    await make(db, li).run(NOW)
    assert li.creates == [] and li.puts == []  # no LinkedIn call at all
    assert "is empty" in db.posts[post.id]["error"]
