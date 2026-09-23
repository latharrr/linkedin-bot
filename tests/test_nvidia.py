import ast
import sys
from pathlib import Path

import httpx
import pytest

from app.corpus import admit_on_post
from app.nvidia import CHAT_URL, EMBED_URL, NvidiaClient, NvidiaError, strip_think
from app.services import Embedder, EmbeddingError
from app.style_check import check_draft
from tests.fakes import FakeDB, FakeNvidia

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reembed_corpus import reembed  # noqa: E402
from validate_writer import CASES, SAMPLE_VOICE, render_markdown, run_case  # noqa: E402


def client(handler, sleeps=None, attempts=3) -> NvidiaClient:
    async def sleep(s):
        if sleeps is not None:
            sleeps.append(s)

    return NvidiaClient(
        "nvapi-secret", httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        chat_timeout=1, embed_timeout=1, image_timeout=1, max_attempts=attempts, sleep=sleep,
    )


def chat_ok(text="hi", finish="stop"):
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}]})


# ── transport behaviour ──────────────────────────────────────────────────────
async def test_endpoints_and_auth():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["authorization"]))
        return chat_ok()

    await client(handler).chat("m", [{"role": "user", "content": "x"}])
    assert seen == [(CHAT_URL, "Bearer nvapi-secret")]
    assert CHAT_URL == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert EMBED_URL == "https://integrate.api.nvidia.com/v1/embeddings"


async def test_429_retried_honouring_retry_after():
    responses = iter([httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(503), chat_ok("done")])
    sleeps: list[float] = []
    result = await client(lambda r: next(responses), sleeps).chat("m", [])
    assert result.text == "done" and sleeps == [7.0, 4.0]


async def test_retry_after_is_capped_for_5xx():
    # (a 429 with a long retry-after is a quota and fails fast — see test_quota_429_…)
    responses = iter([httpx.Response(503, headers={"retry-after": "999"}), chat_ok()])
    sleeps: list[float] = []
    await client(lambda r: next(responses), sleeps).chat("m", [])
    assert sleeps == [30.0]


async def test_retries_exhausted_raises_with_status():
    with pytest.raises(NvidiaError) as exc:
        await client(lambda r: httpx.Response(429, text="Too Many Requests")).chat("m", [])
    assert exc.value.status == 429


async def test_connect_error_retried_read_timeout_not():
    calls = []

    def flaky(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=req)
        return chat_ok()

    assert (await client(flaky).chat("m", [])).text == "hi" and len(calls) == 2

    calls.clear()

    def slow(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(NvidiaError, match="ReadTimeout"):
        await client(slow).chat("m", [])
    assert len(calls) == 1  # a slow generation is not doubled


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_4xx_not_retried(status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, text="nope")

    with pytest.raises(NvidiaError, match=f"HTTP {status}"):
        await client(handler).chat("m", [])
    assert len(calls) == 1


async def test_api_key_never_in_error_messages():
    with pytest.raises(NvidiaError) as exc:
        await client(lambda r: httpx.Response(401, text="bad auth")).chat("m", [])
    assert "nvapi-secret" not in str(exc.value)


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"nomessage": 1}]}])
async def test_malformed_chat_response(payload):
    with pytest.raises(NvidiaError, match="unexpected response shape"):
        await client(lambda r: httpx.Response(200, json=payload)).chat("m", [])


async def test_null_content_becomes_empty_text():
    r = await client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "stop"}]})).chat("m", [])
    assert r.text == ""


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("<think>hmm</think>\nPost", "Post"),
        ("<THINK>a\nb</THINK>Post", "Post"),
        ("reasoning without opener</think>\n\nPost", "Post"),
        ("Plain post", "Plain post"),
    ],
)
def test_strip_think(raw, clean):
    assert strip_think(raw) == clean


# ── FLUX ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload", [{}, {"artifacts": []}, {"artifacts": [{"finishReason": "SUCCESS", "base64": "!!!notbase64"}]}])
async def test_flux_bad_payloads(payload):
    with pytest.raises(NvidiaError):
        await client(lambda r: httpx.Response(200, json=payload)).flux("p")


# ── embeddings ───────────────────────────────────────────────────────────────
async def test_embed_request_shape_and_order():
    nv = FakeNvidia()
    vecs = await Embedder(nv.client(), "nvidia/nemotron-3-embed-1b").embed_passages(["one", "two"])
    body = nv.embed_calls[0]
    assert body == {
        "model": "nvidia/nemotron-3-embed-1b", "input": ["one", "two"], "input_type": "passage",
        "encoding_format": "float", "truncate": "END",
    }
    assert len(vecs) == 2 and all(len(v) == 2048 for v in vecs)
    assert vecs[0] != vecs[1]  # fake returns data reversed; client re-sorts by index


async def test_query_vs_passage():
    nv = FakeNvidia()
    e = Embedder(nv.client(), "m")
    await e.embed_query("brief")
    await e.embed_passage("post")
    assert [c["input_type"] for c in nv.embed_calls] == ["query", "passage"]


async def test_batches_of_16():
    nv = FakeNvidia()
    await Embedder(nv.client(), "m").embed_passages([f"t{i}" for i in range(40)])
    assert [len(c["input"]) for c in nv.embed_calls] == [16, 16, 8]


async def test_wrong_dimension_fails_loudly():
    nv = FakeNvidia()
    nv.embed_dim = 4096
    with pytest.raises(EmbeddingError, match="4096-dim.*vector\\(2048\\)"):
        await Embedder(nv.client(), "some/4096-dim-model").embed_query("x")


async def test_count_mismatch_detected():
    resp = {"data": [{"index": 0, "embedding": [0.1]}]}
    with pytest.raises(NvidiaError, match="got 1 vectors"):
        await client(lambda r: httpx.Response(200, json=resp)).embed("m", ["a", "b"], "passage")


async def test_corpus_admission_embeds_as_passage():
    nv = FakeNvidia()
    db = FakeDB()
    p = db.add_post(status="posted", chosen="a", edited_a=True, draft_a="my post")
    await admit_on_post(db, Embedder(nv.client(), "m"), p)
    assert nv.embed_calls[0]["input_type"] == "passage" and nv.embed_calls[0]["input"] == ["my post"]


# ── one-time re-embedding ────────────────────────────────────────────────────
class MigrDB(FakeDB):
    async def past_posts_missing_embedding(self, limit=200):
        return [(k, r["text"]) for k, r in self.past.items() if r["embedding"] is None][:limit]

    async def set_past_post_embedding(self, pid, emb):
        self.past[pid]["embedding"] = emb

    async def posts_missing_topic_embedding(self, limit=200):
        return [(k, r["brief"]) for k, r in self.posts.items() if r.get("topic_embedding") is None][:limit]

    async def set_topic_embedding(self, pid, emb):
        self.posts[pid]["topic_embedding"] = emb


async def test_reembed_fills_nulls_with_right_input_types_and_paces():
    db = MigrDB()
    for i in range(20):
        await db.insert_past_post(f"post {i}", "human", None, engagement=i)
    for i in range(3):
        db.add_post(brief=f"brief {i}", topic_embedding=None)
    nv, sleeps = FakeNvidia(), []

    async def sleep(s):
        sleeps.append(s)

    counts = await reembed(db, Embedder(nv.client(), "m"), sleep)
    assert counts == {"past_posts": 20, "posts": 3}
    assert all(len(r["embedding"]) == 2048 for r in db.past.values())
    assert all(len(r["topic_embedding"]) == 2048 for r in db.posts.values())
    assert [c["input_type"] for c in nv.embed_calls] == ["passage", "passage", "query"]
    assert sleeps and all(s >= 1.5 for s in sleeps)  # ≤40 req/min
    assert await reembed(db, Embedder(nv.client(), "m"), sleep) == {"past_posts": 0, "posts": 0}  # idempotent


# ── validation script (logic only; the real run needs NVIDIA_API_KEY) ─────────
async def test_validation_run_case_and_report():
    from app.writer import Writer

    nv = FakeNvidia()
    rows = await run_case(Writer(nv.client(), "m", 0.85), CASES[0], SAMPLE_VOICE)
    assert [r["which"] for r in rows] == ["A", "B"]
    assert len(nv.chat_calls) == 5 and nv.chat_calls[1]["temperature"] == 0.85
    assert rows[0]["unverified"] == []  # "47%" is in the fixture research
    md = render_markdown("m", [(CASES[0], rows)])
    assert "## Brief: Why most student founders quit" in md and "Before de-AI" in md


def test_validation_cases_have_three_briefs_with_numbers():
    assert len(CASES) == 3
    for case in CASES:
        assert any(ch.isdigit() for r in case["research"] for ch in r["content"])


# ── style checker ────────────────────────────────────────────────────────────
CLEAN = (
    "I lost my first co-founder in month four.\n\n"
    + "We shipped fast and talked too little about who owned what.\n\n" * 14
    + "\nWhat would you have done differently?\n\n#startups #founders"
)


def test_clean_draft_passes():
    r = check_draft(CLEAN)
    assert r.passed and r.soft == [], (r.hard, r.soft)


@pytest.mark.parametrize(
    "phrase,label",
    [
        ("Let's delve into it.", "delve"), ("This was a game-changer.", "game-changer"), ("It unlocked growth.", "unlock"),
        ("We supercharged sales.", "supercharge"), ("Elevate your team.", "elevate"), ("We embarked on it.", "embark"),
        ("This is crucial.", "crucial"), ("A rich tapestry.", "tapestry"), ("Here’s the thing.", "here's the thing"),
        ("Let that sink in.", "let that sink in"), ("In today's fast-paced world, x.", "in today's fast-paced world"),
        ("It's not about speed, it's about focus.", "it's not X, it's Y"),
        ("Hiring isn't a numbers game. It's a trust game.", "it's not X, it's Y"),
    ],
)
def test_each_cliche_is_a_hard_failure(phrase, label):
    assert f"cliché: {label}" in check_draft(phrase + "\n" + CLEAN).hard


def test_hashtag_and_emoji_bullet_limits():
    assert "4 hashtags (max 2)" in check_draft(CLEAN + " #a1 #b2").hard
    assert "emoji used as a bullet" in check_draft("🚀 Point one\n✅ Point two\n" + CLEAN).hard
    assert "emoji used as a bullet" not in check_draft("🚀 One emoji-led opener is fine\n\n" + CLEAN).hard


def test_soft_warnings():
    r = check_draft("Why do founders quit?\nBecause of the landscape.\nThe end.")
    assert r.passed
    assert any("words" in w for w in r.soft)
    assert "does not end with a CTA question" in r.soft and "opens with a question" in r.soft
    assert any("landscape" in w for w in r.soft)


# ── the old providers are gone ───────────────────────────────────────────────
def test_no_openai_or_anthropic_imports_left():
    offenders = []
    for path in [*ROOT.glob("app/*.py"), *ROOT.glob("scripts/*.py"), ROOT / "bot.py", ROOT / "dispatcher.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders += [f"{path.name}:{n}" for n in names if n.split(".")[0] in ("openai", "anthropic")]
    assert offenders == []
    reqs = (ROOT / "requirements.txt").read_text()
    assert "openai" not in reqs and "anthropic" not in reqs


# ── reasoning off ────────────────────────────────────────────────────────────
async def test_thinking_disabled_via_chat_template_kwargs():
    from app.nvidia import THINKING_OFF

    bodies = []

    def handler(req):
        import json as _j

        bodies.append(_j.loads(req.content))
        return chat_ok()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    on = NvidiaClient("k", http, chat_timeout=1, embed_timeout=1, image_timeout=1, chat_template_kwargs=THINKING_OFF)
    off = NvidiaClient("k", http, chat_timeout=1, embed_timeout=1, image_timeout=1)
    await on.chat("m", [])
    await off.chat("m", [])
    assert bodies[0]["chat_template_kwargs"] == {"thinking": False, "enable_thinking": False}
    assert "chat_template_kwargs" not in bodies[1]


def test_build_nvidia_disables_thinking_by_default(settings):
    from app.services import build_nvidia

    assert build_nvidia(settings, httpx.AsyncClient())._template_kwargs == {"thinking": False, "enable_thinking": False}
    assert build_nvidia(settings.model_copy(update={"nvidia_disable_thinking": False}), httpx.AsyncClient())._template_kwargs is None


async def test_quota_429_with_long_retry_after_fails_fast():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "207"}, text="tokens per day (TPD): Limit 200000")

    with pytest.raises(NvidiaError, match="HTTP 429"):
        await client(handler).chat("m", [])
    assert len(calls) == 1  # no pointless 30 s waits; the fallback chain takes over


async def test_embedder_folds_unicode_bold_for_embedding_only():
    nv = FakeNvidia()
    await Embedder(nv.client(), "m").embed_passage("𝐅𝐨𝐫 𝐭𝐡𝐞 𝐥𝐚𝐬𝐭 𝟑𝟎 𝐝𝐚𝐲𝐬")
    assert nv.embed_calls[0]["input"] == ["For the last 30 days"]


async def test_per_minute_429_is_waited_out_not_failed_over():
    responses = iter([
        httpx.Response(429, headers={"retry-after": "42"}, text="Rate limit reached on tokens per minute (TPM): Limit 8000"),
        chat_ok("after the wait"),
    ])
    sleeps: list[float] = []
    assert (await client(lambda r: next(responses), sleeps).chat("m", [])).text == "after the wait"
    assert sleeps == [42.0]


async def test_per_day_429_still_fails_fast():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "207"}, text="Rate limit reached on tokens per day (TPD)")

    with pytest.raises(NvidiaError):
        await client(handler).chat("m", [])
    assert len(calls) == 1
