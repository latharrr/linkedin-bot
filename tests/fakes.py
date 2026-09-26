"""In-memory fakes. NVIDIA NIM is faked at the HTTP level (httpx.MockTransport with
the real response shapes) so the real NvidiaClient / Writer / ImageMaker /
Embedder code runs in tests."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import statistics
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from PIL import Image

from app.db import CHAT_STATE_TTL, ChatState, PastPost, Post, SimilarTopic
from app.groq import GroqClient
from app.nvidia import NvidiaClient

OUTLINE_JSON = {
    "insight": "Most student founders quit before month 6",
    "hooks": ["47% of student startups die in year one", "I lost my first co-founder", "The 3am call"],
    "audience": "student founders",
    "sub_template": "story_failure_first",
    "angle_note": "fresh",
}


def png_bytes(size: tuple[int, int] = (1024, 1536), color: str = "navy") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(size: tuple[int, int] = (1024, 1024), color: str = "teal") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def fake_vector(text: str, dim: int = 2048) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    return ([b / 255 for b in seed] * (dim // 32 + 1))[:dim]


# ── NVIDIA NIM (served over httpx.MockTransport with the real response shapes) ─
def user_text(body: dict[str, Any]) -> str:
    return body["messages"][-1]["content"]


def system_text(body: dict[str, Any]) -> str:
    first = body["messages"][0]
    return first["content"] if first["role"] == "system" else ""


async def _no_sleep(_: float) -> None:
    return None


class FakeNvidia:
    """Handles chat/completions, embeddings and FLUX exactly like NIM responds."""

    def __init__(self) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.embed_calls: list[dict[str, Any]] = []
        self.image_calls: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        self.finish_reason = "stop"
        self.wrap_think = False
        self.fail_images = False
        self.image_finish = "SUCCESS"
        self.embed_dim = 2048
        self.draft_count = 0
        self.groq_calls: list[dict[str, Any]] = []
        self.browse_text = "[]"
        self.browse_tools: list[dict[str, Any]] = []
        self.browse_status = 200
        self.chat_reply: Any = {"reply": "Happy to help. What's on your mind?", "draft_topic": None}
        self._converse_calls = 0  # lets chat_reply be a list: one entry per converse() round trip
        self.chat_calls_history: list[list[dict[str, str]]] = []
        self.tune_reply: str | None = None
        self.unsupported: list[str] = []
        self.final_prompts: list[str] = []
        self.comment_prompts: list[str] = []
        self.audit_prompts: list[str] = []
        self.tune_prompts: list[str] = []

    def client(self) -> NvidiaClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return NvidiaClient("nvapi-test", http, chat_timeout=5, embed_timeout=5, image_timeout=5, sleep=_no_sleep)

    def groq_client(self) -> GroqClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return GroqClient("gsk-test", http, chat_timeout=5, browse_timeout=5, sleep=_no_sleep)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth_headers.append(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        route = (request.url.host, request.url.path)
        if route == ("api.groq.com", "/openai/v1/chat/completions"):
            self.groq_calls.append(body)
            return self._browse(body) if "tools" in body else self._chat(body)
        if route == ("integrate.api.nvidia.com", "/v1/chat/completions"):
            return self._chat(body)
        if route == ("integrate.api.nvidia.com", "/v1/embeddings"):
            return self._embed(body)
        if route == ("ai.api.nvidia.com", "/v1/genai/black-forest-labs/flux.1-dev"):
            return self._flux(body)
        return httpx.Response(404, json={"detail": "not found"})

    def _chat(self, body: dict[str, Any]) -> httpx.Response:
        self.chat_calls.append(body)
        user = user_text(body)
        if "Telegram assistant inside" in system_text(body):
            self.chat_calls_history.append(body["messages"][1:])
            reply = self.chat_reply
            if isinstance(reply, list):  # a scripted sequence: one entry per tool-loop round trip
                reply = reply[min(self._converse_calls, len(reply) - 1)]
            self._converse_calls += 1
            text = reply if isinstance(reply, str) else json.dumps(reply)
        elif user.startswith("You draft comments that Deepanshu Lathar will post"):
            self.comment_prompts.append(user)
            text = json.dumps({"comments": [
                {"template": "Missing piece", "text": "The attribution argument misses one piece: offline activations never show up in ad dashboards, so the channel that looks weakest is often the one filling the room. Which one did you under-count?"},
                {"template": "Sharper question", "text": "The harder version of this question is what you stop funding when the numbers finally show up. Most teams can measure a channel long before they can bring themselves to cut it."},
            ]})
        elif user.startswith("Write the headline for this LinkedIn post's cover image"):
            text = "Your AI agent needs a boss"
        elif user.startswith("You are the final editor"):
            self.final_prompts.append(user)
            text = "47% of student startups die in year one. [raw] final post\nWhat would you do?"
        elif user.startswith("You fact-check a LinkedIn post"):
            self.audit_prompts.append(user)
            text = json.dumps({"unsupported": self.unsupported})
        elif user.startswith("You are editing a LinkedIn post"):
            self.tune_prompts.append(user)
            post = user.split("POST:\n", 1)[1].rsplit("\n\nReturn ONLY", 1)[0]
            text = self.tune_reply if self.tune_reply is not None else "Sharper hook.\n\n" + post
        elif user.startswith("You turn a LinkedIn post into ONE image prompt"):
            text = ("SCENE: A student founder alone in a dim co-working space at 2am, laptop open, whiteboard of crossed-out plans behind.\n"
                    "SIMPLE: A laptop and a whiteboard of crossed-out plans on an office desk in daylight.")
        elif "Return ONLY JSON" in user:
            text = "Here you go:\n" + json.dumps(OUTLINE_JSON)
        elif user.startswith("Analyze these LinkedIn posts"):
            text = json.dumps({"current_role": "<ask the user, do not guess>", "tone": "direct"})
        elif user.startswith("Edit this LinkedIn post"):
            flagged = user.split("must go: ", 1)[1].split("\n", 1)[0].split(", ")
            text = user.split("POST:\n", 1)[1].rsplit("\nReturn ONLY", 1)[0]
            for n in flagged:
                text = text.replace(n + " ", "").replace(n, "")
        elif user.startswith("You are an editor"):
            draft = user.split("DRAFT:\n", 1)[1].rsplit("\nReturn ONLY", 1)[0]
            text = draft.replace("[raw]", "[edited]")
        else:
            role = "story" if "This draft's role: story" in system_text(body) else "contrarian"
            self.draft_count += 1
            text = f"47% of student startups die in year one. [raw] {role} v{self.draft_count}\nWhat would you do?"
        if self.wrap_think:
            text = "<think>let me reason about delve and tapestry</think>\n" + text
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": self.finish_reason}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 42, "total_tokens": 142},
            },
        )

    def _browse(self, body: dict[str, Any]) -> httpx.Response:
        if self.browse_status != 200:
            return httpx.Response(self.browse_status, json={"error": {"message": "boom"}})
        message = {"role": "assistant", "content": self.browse_text, "executed_tools": self.browse_tools}
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}], "usage": {"completion_tokens": 50}})

    def _embed(self, body: dict[str, Any]) -> httpx.Response:
        self.embed_calls.append(body)
        data = [{"index": i, "embedding": fake_vector(t, self.embed_dim), "object": "embedding"} for i, t in enumerate(body["input"])]
        return httpx.Response(
            200,
            json={"object": "list", "data": list(reversed(data)), "model": body["model"], "usage": {"prompt_tokens": 5, "total_tokens": 5}},
        )

    def _flux(self, body: dict[str, Any]) -> httpx.Response:
        self.image_calls.append(body)
        if self.fail_images:
            return httpx.Response(500, json={"detail": "upstream error"})
        artifact = {"base64": base64.b64encode(jpeg_bytes()).decode(), "finishReason": self.image_finish, "seed": 42}
        return httpx.Response(200, json={"artifacts": [artifact]})


# ── Tavily ───────────────────────────────────────────────────────────────────
RESEARCH_RESULTS = [
    {"title": f"Source {i}", "url": f"https://example.com/{i}", "content": f"Fact {i}: 47% of student startups die in year one.", "score": 1 - i / 20}
    for i in range(8)
]


class FakeTavily:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.results = RESEARCH_RESULTS if results is None else results

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append((query, kwargs))
        return {"results": copy.deepcopy(self.results)}


# ── Messenger ────────────────────────────────────────────────────────────────
class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any, Any]] = []  # (kind, chat_id/callback_id, payload)

    async def send_text(self, chat_id: int, text: str, keyboard: Any = None) -> None:
        self.sent.append(("text", chat_id, (text, keyboard)))

    async def send_photo(self, chat_id: int, photo_url: str) -> None:
        self.sent.append(("photo", chat_id, photo_url))

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: Any) -> None:
        self.sent.append(("edit_keyboard", chat_id, (message_id, keyboard)))

    async def send_menu(self, chat_id: int, text: str, rows: list[list[str]]) -> None:
        self.sent.append(("menu", chat_id, (text, rows)))

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.sent.append(("answer", callback_id, text))

    # helpers
    def texts(self) -> list[str]:
        return [p[0] for k, _, p in self.sent if k == "text"]

    def answers(self) -> list[str | None]:
        return [p for k, _, p in self.sent if k == "answer"]

    def keyboards(self) -> list[Any]:
        return [p[1] for k, _, p in self.sent if k == "text" and p[1]]

    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.sent]


# ── Database ─────────────────────────────────────────────────────────────────
class FakeDB:
    """Mirrors app.db.Database semantics, including conditional updates."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.past: dict[str, dict[str, Any]] = {}
        self.chat_states: dict[int, dict[str, Any]] = {}
        self.settings: dict[str, str] = {}
        self.voice: dict[str, Any] | None = {"current_role": "Founder, Example Labs", "tone": "direct"}
        self.similar: SimilarTopic | None = None
        self.uploads: dict[str, bytes] = {}

    # RPCs
    async def find_similar_topic(self, embedding: list[float], threshold: float = 0.85) -> SimilarTopic | None:
        return self.similar

    async def match_past_posts(self, embedding: list[float], k: int = 3) -> list[PastPost]:
        rows = sorted(self.past.values(), key=lambda r: -r["engagement"])[:k]
        return [PastPost(**r) for r in rows]

    # voice
    async def get_voice_profile(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.voice)

    async def upsert_voice_profile(self, profile: dict[str, Any]) -> None:
        self.voice = copy.deepcopy(profile)

    # posts
    def add_post(self, **fields: Any) -> Post:
        row = {
            "id": str(uuid.uuid4()),
            "chat_id": 42,
            "brief": "brief",
            "status": "awaiting_choice",
            "draft_a": "draft A text",
            "draft_b": "draft B text",
            "edited_a": False,
            "edited_b": False,
            "retry_count": 0,
            "research": {"results": [], "outline": dict(OUTLINE_JSON)},
            "image_a_url": "https://storage.example/a.png",
            "image_b_url": "https://storage.example/b.png",
            "topic_embedding": fake_vector("brief"),
        }
        row.update(fields)
        self.posts[row["id"]] = row
        return Post(**row)

    async def insert_post(self, fields: dict[str, Any]) -> Post:
        row = {"edited_a": False, "edited_b": False, "retry_count": 0, **copy.deepcopy(fields)}
        row.setdefault("id", str(uuid.uuid4()))
        self.posts[row["id"]] = row
        return Post(**row)

    async def get_post(self, post_id: str) -> Post | None:
        row = self.posts.get(post_id)
        return Post(**row) if row else None

    async def get_topic_embedding(self, post_id: str) -> list[float] | None:
        return self.posts.get(post_id, {}).get("topic_embedding")

    async def update_post(
        self, post_id: str, fields: dict[str, Any], *, status: str | None = None, require_chosen: bool = False
    ) -> Post | None:
        row = self.posts.get(post_id)
        if row is None or (status is not None and row["status"] != status):
            return None
        if require_chosen and row.get("chosen") is None:
            return None
        row.update(copy.deepcopy(fields))
        return Post(**row)

    async def claim_post(self, post_id: str, now: datetime) -> bool:
        row = self.posts.get(post_id)
        if row is None or row["status"] != "queued":
            return False
        row.update(status="posting", claimed_at=now)
        return True

    async def mark_alerted(self, post_id: str, now: datetime) -> bool:
        row = self.posts.get(post_id)
        if row is None or row["status"] != "posting" or row.get("alerted_at") is not None:
            return False
        row["alerted_at"] = now
        return True

    async def due_posts(self, now: datetime, limit: int = 5) -> list[Post]:
        rows = [r for r in self.posts.values() if r["status"] == "queued" and r.get("scheduled_at") and r["scheduled_at"] <= now]
        result = [Post(**r) for r in sorted(rows, key=lambda r: r["scheduled_at"])[:limit]]
        await asyncio.sleep(0)  # a real SELECT yields: lets two workers both see the row as queued
        return result

    async def stuck_posts(self, cutoff: datetime) -> list[Post]:
        rows = [
            r for r in self.posts.values()
            if r["status"] == "posting" and r.get("claimed_at") and r["claimed_at"] < cutoff and r.get("alerted_at") is None
        ]
        return [Post(**r) for r in rows]

    async def last_posted(self, limit: int = 5, before: datetime | None = None) -> list[Post]:
        rows = [r for r in self.posts.values() if r["status"] == "posted" and r.get("posted_at")]
        if before is not None:
            rows = [r for r in rows if r["posted_at"] <= before]
        return [Post(**r) for r in sorted(rows, key=lambda r: r["posted_at"], reverse=True)[:limit]]

    async def status_counts(self, chat_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.posts.values():
            if r["chat_id"] == chat_id:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
        return counts

    async def posts_with_status(self, chat_id: int, status: str, limit: int = 5) -> list[Post]:
        rows = [r for r in self.posts.values() if r["chat_id"] == chat_id and r["status"] == status]
        if status == "queued":
            rows.sort(key=lambda r: r.get("scheduled_at") or datetime.max.replace(tzinfo=UTC))
        else:
            rows.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True)
        return [Post(**r) for r in rows[:limit]]

    async def any_post_since(self, since: datetime) -> bool:
        return any(r.get("created_at") and r["created_at"] >= since for r in self.posts.values())

    # chat_state
    async def get_chat_state(self, chat_id: int, now: datetime) -> ChatState | None:
        row = self.chat_states.get(chat_id)
        return ChatState(**row) if row and row["expires_at"] > now else None

    async def set_chat_state(self, chat_id: int, action: str, post_id: str | None, now: datetime) -> None:
        self.chat_states[chat_id] = {
            "chat_id": chat_id, "pending_action": action, "post_id": post_id, "expires_at": now + CHAT_STATE_TTL,
        }

    async def clear_chat_state(self, chat_id: int) -> None:
        self.chat_states.pop(chat_id, None)

    # settings
    async def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    async def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    # past_posts
    async def insert_past_post(self, text, source, embedding, engagement=0, posted_at=None) -> str:
        pid = str(uuid.uuid4())
        self.past[pid] = {"id": pid, "text": text, "source": source, "engagement": engagement, "embedding": embedding, "posted_at": posted_at}
        return pid

    async def update_past_post_engagement(self, past_post_id: str, engagement: int) -> None:
        self.past[past_post_id]["engagement"] = engagement

    async def count_engaged_human(self) -> int:
        return sum(1 for r in self.past.values() if r["source"] == "human" and r["engagement"] > 0)

    async def engaged_human_engagements(self) -> list[int]:
        return [r["engagement"] for r in self.past.values() if r["source"] == "human" and r["engagement"] > 0]

    async def past_post_texts(self, source: str | None = None) -> list[str]:
        return [r["text"] for r in self.past.values() if source is None or r["source"] == source]

    # storage
    async def upload_png(self, path: str, data: bytes) -> str:
        self.uploads[path] = data
        return f"https://storage.example/{path}"

    # helpers
    def human_median(self) -> float:
        return statistics.median([r["engagement"] for r in self.past.values() if r["source"] == "human" and r["engagement"] > 0])
