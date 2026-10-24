"""Publisher layer (phase 1): LinkedIn behind the interface, same calls and outcomes."""

from datetime import timedelta

import pytest

from app.linkedin import ImageUploadError, LinkedInError, PostMaybeLive, PostRejected
from app.publishers import Draft, MaybeLive, NotPublished, Unsupported
from app.publishers.linkedin import LinkedInPublisher
from tests.fakes import FakeDB, png_bytes
from tests.test_dispatcher import AUTHOR, NOW, FakeLinkedIn, make, queued

PNG = png_bytes((1080, 1350))


@pytest.fixture
def db():
    d = FakeDB()
    d.settings["linkedin_author_urn"] = AUTHOR
    return d


async def test_linkedin_publisher_makes_the_same_three_calls():
    li = FakeLinkedIn()
    result = await LinkedInPublisher(li, AUTHOR).publish(Draft("Hello #AI world", PNG, "image/png", "Hello"))
    assert li.puts == [("https://upload.example/u1", len(PNG), "image/png")]
    payload = li.creates[0]
    assert payload["author"] == AUTHOR and payload["content"]["media"] == {"id": "urn:li:image:img1", "title": "Hello"}
    assert payload["commentary"] == "Hello {hashtag|\\#|AI} world"
    assert result.platform_post_id == "urn:li:share:777"
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:777"
    assert result.data == {"image_urn": "urn:li:image:img1"}


@pytest.mark.parametrize(
    ("exc", "kind"),
    [(ImageUploadError("x"), NotPublished), (PostRejected("x"), NotPublished), (PostMaybeLive("x"), MaybeLive)],
)
def test_linkedin_errors_carry_the_safety_class(exc, kind):
    assert isinstance(exc, kind) and isinstance(exc, LinkedInError)


async def test_unparseable_restli_id_is_maybe_live():
    with pytest.raises(MaybeLive, match="posted but"):
        await LinkedInPublisher(FakeLinkedIn(restli="garbage"), AUTHOR).publish(Draft("t", PNG, "image/png"))


def test_capability_check():
    li = LinkedInPublisher(None, AUTHOR)
    assert li.check(Draft("short", PNG, "image/png")) is None
    assert "3000 characters" in li.check(Draft("x" * 3001))
    assert LinkedInPublisher.capabilities.native_scheduling is False


async def test_linkedin_has_no_native_scheduling():
    with pytest.raises(Unsupported):
        await LinkedInPublisher(None, AUTHOR).schedule(Draft("t"), NOW)


async def test_dispatcher_stores_the_platform_result(db):
    post = queued(db)
    await make(db, FakeLinkedIn()).run(NOW)
    row = db.posts[post.id]
    assert row["status"] == "posted" and row["post_url"] == "https://www.linkedin.com/feed/update/urn:li:share:777"
    assert row["platform_post_id"] == "urn:li:share:777"
    assert row["platform_data"] == {"linkedin": {"post_id": "urn:li:share:777", "url": row["post_url"], "image_urn": "urn:li:image:img1"}}


async def test_a_platform_without_a_publisher_is_never_sent(db):
    post = queued(db, platform="instagram")
    li = FakeLinkedIn()
    await make(db, li).run(NOW)
    assert li.creates == [] and db.posts[post.id]["status"] == "queued"
    assert "no publisher for 'instagram'" in db.posts[post.id]["error"]


def test_existing_rows_read_as_linkedin(db):
    post = queued(db, scheduled_at=NOW - timedelta(minutes=1))
    assert post.platform == "linkedin" and post.targets == ["linkedin"] and post.platform_data is None
