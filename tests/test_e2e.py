"""Bot + dispatcher over one shared DB: nothing posts until BOTH human gates pass."""

from datetime import UTC, datetime, timedelta

from app.telegram_ui import build_callback
from app.timeutil import IST
from bot import BotHandler, fallback
from dispatcher import Dispatcher
from tests.conftest import CHAT_ID
from tests.test_bot import drain
from tests.test_dispatcher import FakeEmbedder, FakeLinkedIn, fetch_png

T0 = datetime(2026, 9, 22, 10, 0, tzinfo=IST).astimezone(UTC)


def dispatcher(fakes, li):
    fakes["db"].settings["linkedin_author_urn"] = "urn:li:person:me"
    return Dispatcher(fakes["db"], FakeEmbedder(), fakes["messenger"], CHAT_ID, li, fetch_png)


async def test_full_flow_requires_pick_and_time(svc, fakes):
    db, li = fakes["db"], FakeLinkedIn()
    h = BotHandler(svc, now=lambda: T0)

    await h.on_text(CHAT_ID, "make a post on why student founders quit in year one")
    await drain(h)
    post_id = next(iter(db.posts))
    far_future = T0 + timedelta(days=30)

    await dispatcher(fakes, li).run(far_future)  # gate 0: drafts only
    assert li.creates == [] and db.posts[post_id]["status"] == "awaiting_choice"

    await h.on_callback(CHAT_ID, "c0", build_callback("pick", post_id, "b"))  # no draft B on a one-post row
    assert db.posts[post_id].get("chosen") is None
    await h.on_callback(CHAT_ID, "c1", build_callback("pick", post_id, "a"))  # gate 1: ✅ Post this
    await dispatcher(fakes, li).run(far_future)
    assert li.creates == [] and db.posts[post_id]["status"] == "awaiting_choice"

    await h.on_callback(CHAT_ID, "c2", build_callback("time", post_id, "6pm"))  # gate 2
    await dispatcher(fakes, li).run(T0 + timedelta(hours=1))  # not due yet
    assert li.creates == []

    await dispatcher(fakes, li).run(datetime(2026, 9, 22, 18, 0, tzinfo=IST))
    assert len(li.creates) == 1
    assert li.creates[0]["commentary"].startswith("47% of student startups")  # the one post, escaped
    assert "final post" in li.creates[0]["commentary"]
    assert db.posts[post_id]["status"] == "posted"
    assert fakes["messenger"].texts()[-1].startswith("Posted ✓ https://www.linkedin.com/feed/update/")


async def test_fallback_drafts_are_never_posted_without_gates(svc, fakes):
    li = FakeLinkedIn()
    assert await fallback(svc, T0) == "drafted"
    for days in (0, 1, 7):
        await dispatcher(fakes, li).run(T0 + timedelta(days=days))
    assert li.creates == []


async def test_stuck_alert_then_not_live_reposts_once(svc, fakes):
    from app.linkedin import PostMaybeLive

    db = fakes["db"]
    post = db.add_post(status="queued", chosen="a", scheduled_at=T0 - timedelta(minutes=1))
    h = BotHandler(svc, now=lambda: T0)
    await dispatcher(fakes, FakeLinkedIn(create_exc=PostMaybeLive("posts: ReadTimeout"))).run(T0)
    assert db.posts[post.id]["status"] == "posting"
    await h.on_callback(CHAT_ID, "c", build_callback("notlive", post.id))  # human says it isn't live
    li = FakeLinkedIn()
    await dispatcher(fakes, li).run(T0 + timedelta(minutes=5))
    assert len(li.creates) == 1 and db.posts[post.id]["status"] == "posted"
