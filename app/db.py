"""Supabase access. Every query the app runs lives here (SPEC §4 tables + RPCs).

State transitions are conditional UPDATEs (`... WHERE id = ? AND status = ?`)
and report whether a row actually changed, so two workers / two taps can never
both win a transition.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from supabase import AsyncClient, AsyncClientOptions, acreate_client

from app.timeutil import utcnow

Status = Literal["draft", "awaiting_choice", "queued", "posting", "posted", "failed"]
PendingAction = Literal[
    "awaiting_custom_time", "awaiting_edit_a", "awaiting_edit_b", "awaiting_stats", "awaiting_post_url", "awaiting_brief", "awaiting_interview", "awaiting_comment"
]
CHAT_STATE_TTL = timedelta(minutes=30)
EMBED_DIM = 2048  # must match vector(2048) in scripts/schema.sql

# topic_embedding is deliberately excluded: 2048 floats we rarely need.
POST_COLUMNS = (
    "id,chat_id,brief,topic,template,research,draft_a,draft_b,edited_a,edited_b,"
    "image_a_url,image_b_url,image_urn,chosen,scheduled_at,status,claimed_at,alerted_at,"
    "retry_count,error,post_url,past_post_id,created_at,posted_at"
)


class Post(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    chat_id: int
    brief: str
    topic: str | None = None
    template: str | None = None
    research: dict[str, Any] | None = None
    draft_a: str | None = None
    draft_b: str | None = None
    edited_a: bool = False
    edited_b: bool = False
    image_a_url: str | None = None
    image_b_url: str | None = None
    image_urn: str | None = None
    chosen: Literal["a", "b"] | None = None
    scheduled_at: datetime | None = None
    status: Status = "draft"
    claimed_at: datetime | None = None
    alerted_at: datetime | None = None
    retry_count: int = 0
    error: str | None = None
    post_url: str | None = None
    past_post_id: str | None = None
    created_at: datetime | None = None
    posted_at: datetime | None = None

    def draft(self, which: str) -> str:
        return (self.draft_a if which == "a" else self.draft_b) or ""

    def image_url(self, which: str) -> str | None:
        return self.image_a_url if which == "a" else self.image_b_url


class PastPost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    engagement: int = 0
    source: Literal["human", "ai"]


class ChatState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chat_id: int
    pending_action: PendingAction
    post_id: str | None = None
    expires_at: datetime


class SimilarTopic(BaseModel):
    id: str
    topic: str | None
    similarity: float


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) if isinstance(v, datetime) else v for k, v in fields.items()}


def parse_vector(value: Any) -> list[float] | None:
    """pgvector columns come back from PostgREST as the string '[0.1,0.2,...]'."""
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else list(value)


class Database:
    def __init__(self, client: AsyncClient, bucket: str) -> None:
        self._c = client
        self._bucket = bucket

    @classmethod
    async def connect(cls, url: str, service_key: str, bucket: str, timeout: float) -> Database:
        options = AsyncClientOptions(postgrest_client_timeout=int(timeout), storage_client_timeout=int(timeout) * 2)
        client = await acreate_client(url, service_key, options=options)
        return cls(client, bucket)

    # ── RPCs ──────────────────────────────────────────────────────────────
    async def find_similar_topic(self, embedding: list[float], threshold: float = 0.85) -> SimilarTopic | None:
        res = await self._c.rpc("find_similar_topic", {"q": embedding, "threshold": threshold}).execute()
        return SimilarTopic(**res.data[0]) if res.data else None

    async def match_past_posts(self, embedding: list[float], k: int = 3) -> list[PastPost]:
        res = await self._c.rpc("match_past_posts", {"q": embedding, "k": k}).execute()
        return [PastPost(**row) for row in res.data or []]

    # ── voice_profile ─────────────────────────────────────────────────────
    async def get_voice_profile(self) -> dict[str, Any] | None:
        res = await self._c.table("voice_profile").select("profile").eq("id", 1).limit(1).execute()
        return res.data[0]["profile"] if res.data else None

    async def upsert_voice_profile(self, profile: dict[str, Any]) -> None:
        row = {"id": 1, "profile": profile, "updated_at": _iso(utcnow())}
        await self._c.table("voice_profile").upsert(row, on_conflict="id").execute()

    # ── posts ─────────────────────────────────────────────────────────────
    async def insert_post(self, fields: dict[str, Any]) -> Post:
        res = await self._c.table("posts").insert(_jsonable(fields)).execute()
        return Post(**res.data[0])

    async def get_post(self, post_id: str) -> Post | None:
        res = await self._c.table("posts").select(POST_COLUMNS).eq("id", post_id).limit(1).execute()
        return Post(**res.data[0]) if res.data else None

    async def get_topic_embedding(self, post_id: str) -> list[float] | None:
        res = await self._c.table("posts").select("topic_embedding").eq("id", post_id).limit(1).execute()
        return parse_vector(res.data[0]["topic_embedding"]) if res.data else None

    async def update_post(
        self,
        post_id: str,
        fields: dict[str, Any],
        *,
        status: Status | None = None,
        require_chosen: bool = False,
    ) -> Post | None:
        """Conditional update. Returns the updated row, or None if the guard didn't match."""
        q = self._c.table("posts").update(_jsonable(fields)).eq("id", post_id)
        if status is not None:
            q = q.eq("status", status)
        if require_chosen:
            q = q.not_.is_("chosen", "null")
        res = await q.execute()
        return Post(**res.data[0]) if res.data else None

    async def claim_post(self, post_id: str, now: datetime) -> bool:
        """SPEC §6 step 3: queued → posting. False if another worker got there first."""
        res = await (
            self._c.table("posts")
            .update({"status": "posting", "claimed_at": _iso(now)})
            .eq("id", post_id)
            .eq("status", "queued")
            .execute()
        )
        return bool(res.data)

    async def mark_alerted(self, post_id: str, now: datetime) -> bool:
        """Set alerted_at once; False if already alerted (so the alert fires once per row)."""
        res = await (
            self._c.table("posts")
            .update({"alerted_at": _iso(now)})
            .eq("id", post_id)
            .eq("status", "posting")
            .is_("alerted_at", "null")
            .execute()
        )
        return bool(res.data)

    async def due_posts(self, now: datetime, limit: int = 5) -> list[Post]:
        res = await (
            self._c.table("posts")
            .select(POST_COLUMNS)
            .eq("status", "queued")
            .lte("scheduled_at", _iso(now))
            .order("scheduled_at")
            .limit(limit)
            .execute()
        )
        return [Post(**r) for r in res.data or []]

    async def stuck_posts(self, cutoff: datetime) -> list[Post]:
        res = await (
            self._c.table("posts")
            .select(POST_COLUMNS)
            .eq("status", "posting")
            .lt("claimed_at", _iso(cutoff))
            .is_("alerted_at", "null")
            .execute()
        )
        return [Post(**r) for r in res.data or []]

    async def last_posted(self, limit: int = 5, before: datetime | None = None) -> list[Post]:
        q = self._c.table("posts").select(POST_COLUMNS).eq("status", "posted")
        if before is not None:
            q = q.lte("posted_at", _iso(before))
        res = await q.order("posted_at", desc=True).limit(limit).execute()
        return [Post(**r) for r in res.data or []]

    async def status_counts(self, chat_id: int) -> dict[str, int]:
        res = await self._c.table("posts").select("status").eq("chat_id", chat_id).execute()
        counts: dict[str, int] = {}
        for r in res.data or []:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return counts

    async def posts_with_status(self, chat_id: int, status: Status, limit: int = 5) -> list[Post]:
        """/drafts and /queue. Queued posts come soonest-first, everything else newest-first."""
        q = self._c.table("posts").select(POST_COLUMNS).eq("chat_id", chat_id).eq("status", status)
        q = q.order("scheduled_at") if status == "queued" else q.order("created_at", desc=True)
        res = await q.limit(limit).execute()
        return [Post(**r) for r in res.data or []]

    async def any_post_since(self, since: datetime) -> bool:
        res = await self._c.table("posts").select("id").gte("created_at", _iso(since)).limit(1).execute()
        return bool(res.data)

    # ── chat_state (every read filters expires_at > now) ──────────────────
    async def get_chat_state(self, chat_id: int, now: datetime) -> ChatState | None:
        res = await (
            self._c.table("chat_state")
            .select("*")
            .eq("chat_id", chat_id)
            .gt("expires_at", _iso(now))
            .limit(1)
            .execute()
        )
        return ChatState(**res.data[0]) if res.data else None

    async def set_chat_state(self, chat_id: int, action: PendingAction, post_id: str | None, now: datetime) -> None:
        row = {
            "chat_id": chat_id,
            "pending_action": action,
            "post_id": post_id,
            "expires_at": _iso(now + CHAT_STATE_TTL),
        }
        await self._c.table("chat_state").upsert(row, on_conflict="chat_id").execute()

    async def clear_chat_state(self, chat_id: int) -> None:
        await self._c.table("chat_state").delete().eq("chat_id", chat_id).execute()

    # ── settings ──────────────────────────────────────────────────────────
    async def get_setting(self, key: str) -> str | None:
        res = await self._c.table("settings").select("value").eq("key", key).limit(1).execute()
        return res.data[0]["value"] if res.data else None

    async def set_setting(self, key: str, value: str) -> None:
        row = {"key": key, "value": value, "updated_at": _iso(utcnow())}
        await self._c.table("settings").upsert(row, on_conflict="key").execute()

    # ── past_posts (style corpus) ─────────────────────────────────────────
    async def insert_past_post(
        self,
        text: str,
        source: Literal["human", "ai"],
        embedding: list[float],
        engagement: int = 0,
        posted_at: str | None = None,
    ) -> str:
        row = {"text": text, "source": source, "embedding": embedding, "engagement": engagement, "posted_at": posted_at}
        res = await self._c.table("past_posts").insert(row).execute()
        return str(res.data[0]["id"])

    async def update_past_post_engagement(self, past_post_id: str, engagement: int) -> None:
        await self._c.table("past_posts").update({"engagement": engagement}).eq("id", past_post_id).execute()

    async def count_engaged_human(self) -> int:
        res = await (
            self._c.table("past_posts")
            .select("id", count="exact")
            .eq("source", "human")
            .gt("engagement", 0)
            .execute()
        )
        return int(res.count or 0)

    async def engaged_human_engagements(self) -> list[int]:
        res = await self._c.table("past_posts").select("engagement").eq("source", "human").gt("engagement", 0).execute()
        return [int(r["engagement"]) for r in res.data or []]

    # ── usage ledger (scripts/usage.sql) ──────────────────────────────────
    async def record_api_call(self, row: dict[str, Any]) -> None:
        await self._c.table("bot_api_calls").insert(row).execute()

    async def api_calls_since(self, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        res = await (
            self._c.table("bot_api_calls").select("*").gte("at", _iso(since)).order("at", desc=True).limit(limit).execute()
        )
        return list(res.data or [])

    async def count_api_calls(self, provider: str) -> int:
        res = await self._c.table("bot_api_calls").select("id", count="exact").eq("provider", provider).execute()
        return int(res.count or 0)

    async def resource_usage(self) -> dict[str, Any]:
        res = await self._c.rpc("bot_resource_usage", {}).execute()
        return res.data if isinstance(res.data, dict) else {}

    # ── one-time re-embedding (scripts/reembed_corpus.py) ──────────────────
    async def past_posts_missing_embedding(self, limit: int = 200) -> list[tuple[str, str]]:
        res = await self._c.table("past_posts").select("id,text").is_("embedding", "null").limit(limit).execute()
        return [(str(r["id"]), r["text"]) for r in res.data or []]

    async def set_past_post_embedding(self, past_post_id: str, embedding: list[float]) -> None:
        await self._c.table("past_posts").update({"embedding": embedding}).eq("id", past_post_id).execute()

    async def posts_missing_topic_embedding(self, limit: int = 200) -> list[tuple[str, str]]:
        res = await self._c.table("posts").select("id,brief").is_("topic_embedding", "null").limit(limit).execute()
        return [(str(r["id"]), r["brief"]) for r in res.data or []]

    async def set_topic_embedding(self, post_id: str, embedding: list[float]) -> None:
        await self._c.table("posts").update({"topic_embedding": embedding}).eq("id", post_id).execute()

    async def past_post_texts(self, source: Literal["human", "ai"] | None = None) -> list[str]:
        q = self._c.table("past_posts").select("text")
        if source:
            q = q.eq("source", source)
        res = await q.execute()
        return [r["text"] for r in res.data or []]

    # ── storage ───────────────────────────────────────────────────────────
    async def upload_png(self, path: str, data: bytes) -> str:
        bucket = self._c.storage.from_(self._bucket)
        await bucket.upload(path, data, {"content-type": "image/png", "upsert": "false"})
        return await bucket.get_public_url(path)
