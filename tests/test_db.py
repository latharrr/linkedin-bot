"""Runs the REAL app.db.Database through supabase-py against a mocked PostgREST /
Storage transport, asserting the exact HTTP each helper produces."""

import json
from datetime import UTC, datetime
from urllib.parse import unquote

import httpx
import pytest
from supabase import AsyncClientOptions, acreate_client

from app.db import Database, parse_vector

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
POST_ROW = {"id": "p1", "chat_id": 42, "brief": "b", "status": "queued", "research": {"results": []}}


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}

    def on(self, method: str, path: str, **kw) -> None:
        self.responses[(method, path)] = httpx.Response(**{"status_code": 200, **kw})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.get((request.method, request.url.path), httpx.Response(200, json=[]))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self) -> str:
        return unquote(self.last.url.query.decode())

    def body(self):
        return json.loads(self.last.content) if self.last.content else None


@pytest.fixture
async def env():
    rec = Recorder()
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    client = await acreate_client("https://x.supabase.co", "k" * 40, options=AsyncClientOptions(httpx_client=http))
    return Database(client, "post-images"), rec


async def test_claim_is_conditional_on_queued(env):
    db, rec = env
    rec.on("PATCH", "/rest/v1/posts", json=[POST_ROW])
    assert await db.claim_post("p1", NOW) is True
    assert rec.query() == "id=eq.p1&status=eq.queued"
    assert rec.body() == {"status": "posting", "claimed_at": NOW.isoformat()}
    assert "return=representation" in rec.last.headers["prefer"]
    rec.on("PATCH", "/rest/v1/posts", json=[])
    assert await db.claim_post("p1", NOW) is False


async def test_update_post_guards(env):
    db, rec = env
    rec.on("PATCH", "/rest/v1/posts", json=[{**POST_ROW, "status": "queued", "chosen": "a"}])
    post = await db.update_post("p1", {"status": "queued", "scheduled_at": NOW}, status="awaiting_choice", require_chosen=True)
    assert post is not None and post.chosen == "a"
    assert rec.query() == "id=eq.p1&status=eq.awaiting_choice&chosen=not.is.null"
    assert rec.body()["scheduled_at"] == NOW.isoformat()
    rec.on("PATCH", "/rest/v1/posts", json=[])
    assert await db.update_post("p1", {"chosen": "a"}, status="awaiting_choice") is None


async def test_mark_alerted_only_once(env):
    db, rec = env
    await db.mark_alerted("p1", NOW)
    assert rec.query() == "id=eq.p1&status=eq.posting&alerted_at=is.null"


async def test_due_and_stuck_queries(env):
    db, rec = env
    rec.on("GET", "/rest/v1/posts", json=[POST_ROW])
    assert [p.id for p in await db.due_posts(NOW, 5)] == ["p1"]
    q = rec.query()
    assert "status=eq.queued" in q and f"scheduled_at=lte.{NOW.isoformat()}" in q
    assert "order=scheduled_at" in q and "limit=5" in q
    assert "topic_embedding" not in q  # never fetch vectors by default
    await db.stuck_posts(NOW)
    q = rec.query()
    assert "status=eq.posting" in q and f"claimed_at=lt.{NOW.isoformat()}" in q and "alerted_at=is.null" in q


async def test_last_posted_and_any_since(env):
    db, rec = env
    await db.last_posted(5, before=NOW)
    q = rec.query()
    assert "status=eq.posted" in q and f"posted_at=lte.{NOW.isoformat()}" in q and "order=posted_at.desc" in q
    rec.on("GET", "/rest/v1/posts", json=[{"id": "x"}])
    assert await db.any_post_since(NOW) is True
    assert f"created_at=gte.{NOW.isoformat()}" in rec.query()


async def test_chat_state_reads_filter_expiry(env):
    db, rec = env
    rec.on("GET", "/rest/v1/chat_state", json=[{"chat_id": 42, "pending_action": "awaiting_post_url", "post_id": "p1", "expires_at": NOW.isoformat()}])
    state = await db.get_chat_state(42, NOW)
    assert state.pending_action == "awaiting_post_url"
    assert f"expires_at=gt.{NOW.isoformat()}" in rec.query() and "chat_id=eq.42" in rec.query()
    await db.set_chat_state(42, "awaiting_edit_a", "p1", NOW)
    assert rec.last.method == "POST" and "on_conflict=chat_id" in rec.query()
    assert rec.body()["expires_at"] == "2026-09-22T12:30:00+00:00"
    await db.clear_chat_state(42)
    assert rec.last.method == "DELETE" and rec.query() == "chat_id=eq.42"


async def test_rpcs(env):
    db, rec = env
    rec.on("POST", "/rest/v1/rpc/find_similar_topic", json=[{"id": "x", "topic": "t", "similarity": 0.9}])
    sim = await db.find_similar_topic([0.1, 0.2])
    assert sim.similarity == 0.9 and rec.body() == {"q": [0.1, 0.2], "threshold": 0.85}
    rec.on("POST", "/rest/v1/rpc/match_past_posts", json=[{"id": "1", "text": "t", "engagement": 3, "source": "human", "embedding": "[0.1]"}])
    rows = await db.match_past_posts([0.1], 3)
    assert rows[0].text == "t" and rec.body() == {"q": [0.1], "k": 3}


async def test_past_posts_and_counts(env):
    db, rec = env
    rec.on("POST", "/rest/v1/past_posts", json=[{"id": "pp1"}])
    assert await db.insert_past_post("t", "ai", [0.5], engagement=0, posted_at="2026-09-22") == "pp1"
    assert rec.body() == {"text": "t", "source": "ai", "embedding": [0.5], "engagement": 0, "posted_at": "2026-09-22"}
    rec.on("GET", "/rest/v1/past_posts", json=[{"id": "1"}], headers={"content-range": "0-0/7"})
    assert await db.count_engaged_human() == 7
    assert "source=eq.human" in rec.query() and "engagement=gt.0" in rec.query()
    assert "count=exact" in rec.last.headers["prefer"]


async def test_settings_and_voice(env):
    db, rec = env
    rec.on("GET", "/rest/v1/settings", json=[{"value": "urn:li:person:1"}])
    assert await db.get_setting("linkedin_author_urn") == "urn:li:person:1"
    await db.set_setting("linkedin_access_token", "tok")
    assert "on_conflict=key" in rec.query() and rec.body()["value"] == "tok"
    rec.on("GET", "/rest/v1/voice_profile", json=[{"profile": {"current_role": "Founder"}}])
    assert (await db.get_voice_profile())["current_role"] == "Founder"


async def test_topic_embedding_parsed_from_pgvector_string(env):
    db, rec = env
    rec.on("GET", "/rest/v1/posts", json=[{"topic_embedding": "[0.25,0.5]"}])
    assert await db.get_topic_embedding("p1") == [0.25, 0.5]
    assert parse_vector(None) is None


async def test_upload_png_is_png_and_public(env):
    db, rec = env
    rec.on("POST", "/storage/v1/object/post-images/p1/a-x.png", json={"Key": "post-images/p1/a-x.png"})
    url = await db.upload_png("p1/a-x.png", b"\x89PNG\r\n\x1a\nrest")
    upload = next(r for r in rec.requests if r.method == "POST")
    assert upload.url.path == "/storage/v1/object/post-images/p1/a-x.png"
    assert b"Content-Type: image/png" in upload.content  # multipart file part
    assert url.startswith("https://x.supabase.co/storage/v1/object/public/post-images/p1/a-x.png")
