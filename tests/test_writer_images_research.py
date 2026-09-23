import asyncio
import io

import pytest
from PIL import Image

from app.images import (
    ImageMaker,
    crop_to_4x5_png,
    hook_of,
    sniff_image_type,
)
from app.nvidia import FLUX_URL, NvidiaError
from app.research import Researcher, ResearchError, format_research, research_corpus
from app.writer import (
    TEMPLATES,
    Writer,
    WriterError,
    load_prompt,
    parse_json_object,
    render,
    role_for,
    validate_outline,
)
from tests.fakes import (
    OUTLINE_JSON,
    FakeNvidia,
    FakeTavily,
    jpeg_bytes,
    png_bytes,
    system_text,
    user_text,
)

VOICE = {"current_role": "Founder, Example Labs", "tone": "direct"}


# ── prompts ──────────────────────────────────────────────────────────────────
def test_render_fills_known_placeholders_and_keeps_json_braces():
    out = render(load_prompt("outline"), brief="B", research="R", recent_topics="T", few_shot_posts="P", voice_profile="V")
    assert "Brief: B" in out and "Voice: V" in out
    assert '{"insight": "<the one idea, one sentence>",' in out
    assert "{brief}" not in out


def test_draft_system_prompt_has_spec_text():
    text = load_prompt("draft_system")
    for needle in ("Deepanshu Lathar: {current_role}", "{post_1}", "{post_3}", "{copy_playbook}"):
        assert needle in text
    playbook = load_prompt("copy_playbook")
    for needle in ("at most 140 characters", "ONE IDEA per post", "900-1,400 characters", "0-2 niche hashtags", "at most 2 statistics", "A to C without B", "Dated moment"):
        assert needle in playbook


def test_all_prompts_present():
    for name in ("voice_extraction", "outline", "draft_system", "draft_user", "deai", "image"):
        assert load_prompt(name)


# ── parsing / validation ─────────────────────────────────────────────────────
def test_parse_json_object_tolerates_prose():
    assert parse_json_object('Sure!\n{"a": 1}\nThanks') == {"a": 1}


@pytest.mark.parametrize("bad", ["no json", "{not json}", "[1,2]"])
def test_parse_json_object_rejects(bad):
    with pytest.raises(WriterError):
        parse_json_object(bad)


def test_validate_outline_defaults_unknown_template():
    out = validate_outline({"insight": "x", "hooks": ["h1", "h2", "h3", "h4"], "sub_template": "listicle"})
    assert out["sub_template"] == TEMPLATES[0]
    assert out["hooks"] == ["h1", "h2", "h3"]


def test_validate_outline_requires_hooks():
    with pytest.raises(WriterError):
        validate_outline({"insight": "x", "hooks": []})


def test_roles_are_fixed_and_template_refines_matching_role():
    assert role_for("a", "story_dialogue") == "story / founder-POV (structure: story_dialogue)"
    assert role_for("b", "story_dialogue") == "contrarian / insight-list"
    assert role_for("b", "insight_list") == "contrarian / insight-list (structure: insight_list)"
    assert role_for("a", "contrarian_teardown") == "story / founder-POV"


# ── Writer against the NVIDIA chat endpoint (fake NIM responses) ─────────────
MODEL = "deepseek-ai/deepseek-v4.1-flash"


def writer(nv: FakeNvidia, max_tokens: int = 4096) -> Writer:
    return Writer(nv.client(), MODEL, 0.85, max_tokens)


async def test_draft_sends_temperature_085_and_openai_style_messages():
    nv = FakeNvidia()
    await writer(nv).draft("b", "b", OUTLINE_JSON, "R", VOICE, [])
    body = nv.chat_calls[0]
    assert body["model"] == MODEL and body["temperature"] == 0.85 and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "Founder, Example Labs" in system_text(body) and system_text(body).count("(none)") == 3
    assert user_text(body).startswith("Brief: b")
    assert nv.auth_headers[0] == "Bearer nvapi-test"


async def test_editing_calls_run_cool_and_candidates_run_warm():
    from app.writer import EDIT_TEMPERATURE

    nv = FakeNvidia()
    w = writer(nv)
    out = await w.outline("b", "R", "none", [], VOICE)
    await w.deai("d [raw]", "R", VOICE)
    assert out["sub_template"] == "story_failure_first"
    assert all(c["temperature"] == EDIT_TEMPERATURE == 0.2 for c in nv.chat_calls)  # model default (1.0) invented details
    assert all([m["role"] for m in c["messages"]] == ["user"] for c in nv.chat_calls)
    await w.draft("a", "b", {"sub_template": "story_cold_open"}, "R", VOICE, [])
    assert nv.chat_calls[-1]["temperature"] == w._temperature  # candidates keep their variety


async def test_max_tokens_passed_through():
    nv = FakeNvidia()
    await writer(nv, max_tokens=1234).deai("d", "R", VOICE)
    assert nv.chat_calls[0]["max_tokens"] == 1234


async def test_truncated_output_raises():
    nv = FakeNvidia()
    nv.finish_reason = "length"
    with pytest.raises(WriterError, match="truncated"):
        await writer(nv).deai("d", "R", VOICE)


async def test_reasoning_think_block_is_stripped():
    nv = FakeNvidia()
    nv.wrap_think = True
    out = await writer(nv).deai("line [raw]", "R", VOICE)
    assert out == "line [edited]"  # the think text (with 'delve', 'tapestry') never reaches the draft


async def test_deai_returns_rewritten_text():
    out = await writer(FakeNvidia()).deai("line [raw]", "R", VOICE)
    assert out == "line [edited]"


async def test_chat_http_error_is_nvidia_error():
    import httpx

    from app.nvidia import NvidiaClient

    async def no_sleep(_):
        return None

    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404, json={"detail": "Function not found for account"})

    client = NvidiaClient("k", httpx.AsyncClient(transport=httpx.MockTransport(handler)), chat_timeout=1, embed_timeout=1, image_timeout=1, sleep=no_sleep)
    with pytest.raises(NvidiaError, match="HTTP 404"):
        await Writer(client, "nope/model", 0.85).deai("d", "R", VOICE)
    assert len(calls) == 1  # 4xx is not retried


# ── images (FLUX.1-dev on NVIDIA) ────────────────────────────────────────────
def test_crop_is_1080x1350_png():
    out = crop_to_4x5_png(png_bytes((1024, 1536)))
    assert sniff_image_type(out) == "image/png"
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1350) and img.format == "PNG"


def test_square_flux_jpeg_becomes_1080x1350_png():
    out = crop_to_4x5_png(jpeg_bytes((1024, 1024)))
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1350) and img.format == "PNG"


def test_crop_is_centered():
    # 1024x1536 → 4:5 keeps the middle 1280 rows (128 trimmed top & bottom).
    img = Image.new("RGB", (1024, 1536), "red")
    img.paste((0, 0, 255), (0, 128, 1024, 1408))  # blue band = what should survive
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with Image.open(io.BytesIO(crop_to_4x5_png(buf.getvalue()))) as out:
        assert out.getpixel((540, 2))[2] > 200 and out.getpixel((540, 1347))[2] > 200
        assert out.getpixel((540, 2))[0] < 50


def test_square_crop_trims_sides_evenly():
    # 1024x1024 → 4:5 keeps the middle 819 columns (~102 trimmed left & right).
    img = Image.new("RGB", (1024, 1024), "red")
    img.paste((0, 0, 255), (103, 0, 921, 1024))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with Image.open(io.BytesIO(crop_to_4x5_png(buf.getvalue()))) as out:
        assert out.getpixel((3, 675))[2] > 200 and out.getpixel((1076, 675))[2] > 200


def test_crop_accepts_wide_and_webp_input_but_outputs_png():
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), "green").save(buf, format="WEBP")
    out = crop_to_4x5_png(buf.getvalue())
    assert sniff_image_type(out) == "image/png"


def test_sniff():
    assert sniff_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_type(b"RIFF....WEBP") is None


def test_hook_of():
    assert hook_of("\n\n  First line  \nsecond") == "First line"


async def test_image_maker_calls_flux_with_scene_and_quality_settings():
    nv = FakeNvidia()
    data = await ImageMaker(nv.client()).generate('A founder takes "the 3am call" in a dark hallway')
    body = nv.image_calls[0]
    assert set(body) == {"prompt", "mode", "width", "height", "steps", "cfg_scale"}
    assert (body["width"], body["height"], body["steps"], body["mode"]) == (1088, 1344, 50, "base")
    assert body["prompt"].startswith("A founder takes 'the 3am call' in a dark hallway")
    assert "No text" in body["prompt"]
    assert nv.auth_headers == ["Bearer nvapi-test"]
    assert sniff_image_type(data) == "image/png"
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (1080, 1350)


def test_flux_url_is_the_verified_endpoint():
    assert FLUX_URL == "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"


async def test_flux_content_filtered_falls_back_to_neutral_prompt():
    class FilterHookOnly:
        def __init__(self):
            self.prompts = []

        async def flux(self, prompt):
            self.prompts.append(prompt)
            if "neutral" not in prompt and "abstract" not in prompt:
                raise NvidiaError("flux: finishReason='CONTENT_FILTERED'")
            return jpeg_bytes()

    c = FilterHookOnly()
    data = await ImageMaker(c).generate("𝐈 𝐛𝐮𝐢𝐥𝐭 𝐚 𝐛𝐨𝐭")
    assert sniff_image_type(data) == "image/png" and len(c.prompts) == 2
    assert "I built a bot" in c.prompts[0]  # bold-Unicode never reaches the image model


async def test_flux_down_falls_back_to_local_text_card():
    nv = FakeNvidia()
    nv.fail_images = True
    data = await ImageMaker(nv.client()).generate("Hook line")
    with Image.open(io.BytesIO(data)) as img:
        assert img.size == (1080, 1350) and img.format == "PNG"
    assert len(nv.image_calls) == 6  # hook ×3 retries, neutral ×3 retries, then the card


async def test_flux_garbage_bytes_fall_back_to_card():
    class Garbage:
        async def flux(self, prompt):
            return b"not an image"

    assert sniff_image_type(await ImageMaker(Garbage()).generate("hook")) == "image/png"


def test_text_card_is_valid_png():
    from app.images import text_card_png

    with Image.open(io.BytesIO(text_card_png("𝐒𝐭𝐫𝐚𝐭𝐞𝐠𝐲 𝐢𝐬 𝐬𝐞𝐱𝐲. " * 20))) as img:
        assert img.size == (1080, 1350)


async def test_draft_prompt_uses_plain_examples():
    nv = FakeNvidia()
    await Writer(nv.client(), "m", 0.85).draft("a", "b", {"sub_template": "story_cold_open"}, "R", VOICE, ["𝐒𝐭𝐫𝐚𝐭𝐞𝐠𝐲 𝐢𝐬 𝐬𝐞𝐱𝐲."])
    system = system_text(nv.chat_calls[0])
    assert "Strategy is sexy." in system and "𝐒𝐭𝐫𝐚𝐭𝐞𝐠𝐲" not in system


# ── research ─────────────────────────────────────────────────────────────────
async def test_research_query_and_top_results():
    tav = FakeTavily()
    res = await Researcher(tav, 10).research("student founders", 2026)
    query, kwargs = tav.queries[0]
    assert query == "student founders statistics 2026"
    assert kwargs["max_results"] == 8 and kwargs["timeout"] == 10
    assert len(res["results"]) == 8
    assert all(r["url"].startswith("https://") for r in res["results"])
    assert res["results"][0]["title"] == "Source 0"  # highest score first


async def test_research_drops_results_without_url():
    tav = FakeTavily([{"title": "t", "content": "c", "url": ""}, {"title": "ok", "content": "c", "url": "https://u"}])
    res = await Researcher(tav, 10).research("x", 2026)
    assert [r["title"] for r in res["results"]] == ["ok"]


async def test_research_timeout_is_enforced():
    class Slow:
        async def search(self, query, **kw):
            await asyncio.sleep(10)

    r = Researcher(Slow(), 0.01)
    r.GRACE = 0
    with pytest.raises(ResearchError):
        await asyncio.wait_for(r.research("x", 2026), 7)


async def test_news_topic_rotates_keywords():
    tav = FakeTavily()
    r = Researcher(tav, 10)
    await r.news_topic(["AI agents", "startups"], 0)
    await r.news_topic(["AI agents", "startups"], 1)
    assert [q for q, _ in tav.queries] == ["AI agents latest news", "startups latest news"]
    assert tav.queries[0][1]["topic"] == "news"


async def test_news_topic_none_without_keywords_or_results():
    assert await Researcher(FakeTavily(), 10).news_topic([], 0) is None
    assert await Researcher(FakeTavily([]), 10).news_topic(["x"], 0) is None


def test_format_and_corpus():
    research = {"results": [{"title": "T", "url": "https://u", "content": "47% grew"}]}
    assert format_research(research) == "[1] T — 47% grew (source: https://u)"
    assert format_research(None) == "(no research results)"
    assert "47% grew" in research_corpus(research, "brief 2B")
    assert "brief 2B" in research_corpus(research, "brief 2B")


def test_unbold_folds_math_alphanumerics_only():
    from app.writer import clean_post, unbold

    assert unbold("𝐅𝐨𝐫 𝐭𝐡𝐞 𝐥𝐚𝐬𝐭 𝟑𝟎 𝐝𝐚𝐲𝐬 🚀 — “quotes” …") == "For the last 30 days 🚀 — “quotes” …"
    assert unbold("𝗧𝗵𝗲 𝘪𝘵𝘢𝘭𝘪𝘤") == "The italic"
    assert clean_post('"𝐇𝐨𝐨𝐤 line"', fold_bold=True) == "Hook line"
    assert clean_post('"𝐇𝐨𝐨𝐤 line"') == "𝐇𝐨𝐨𝐤 line"  # default: bold-Unicode is allowed on LinkedIn (master profile §10.3)


def test_card_text_has_no_undrawable_glyphs():
    from app.images import card_text

    out = card_text("𝐒𝐡𝐢𝐩𝐩𝐞𝐝 — “two taps” … 🚀 done")
    assert out == 'Shipped - "two taps" ... done'


# ── designed cover ──────────────────────────────────────────────────────────
def test_covers_are_4x5_png():
    from app.cover import photo_cover, type_cover

    for data in (type_cover("Your AI agent needs a boss"), photo_cover(png_bytes((1088, 1344)), "Your AI agent needs a boss")):
        with Image.open(io.BytesIO(data)) as img:
            assert img.size == (1080, 1350) and img.format == "PNG"


def test_headline_is_cleaned_but_never_silently_cut():
    from app.cover import clean_headline

    assert clean_headline("“Ship ugly — then fix it.” 🚀 #growth") == "Ship ugly, then fix it"
    long = " ".join(["word"] * 20)
    assert clean_headline(long).endswith("…")


def test_private_places_are_moved_before_the_image_model():
    from app.images import safe_scene

    out = safe_scene("A desk in a shared apartment; later a hostel room, then a bedroom, and a dorm.")
    assert not any(w in out.lower() for w in ("apartment", "hostel", "bedroom", "dorm"))


async def test_photo_tries_every_scene_then_neutral_then_type_cover():
    class Filter:
        def __init__(self, ok_on):
            self.prompts, self.ok_on = [], ok_on

        async def flux(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == self.ok_on:
                return jpeg_bytes()
            raise NvidiaError("flux: finishReason='CONTENT_FILTERED'")

    second = Filter(ok_on=2)
    assert await ImageMaker(second).photo(["first scene is refused", "simpler scene passes"]) is not None
    assert "simpler scene passes" in second.prompts[1]
    never = Filter(ok_on=99)
    data = await ImageMaker(never).generate(["a", "b"], headline="Your AI agent needs a boss")
    assert len(never.prompts) == 3 and sniff_image_type(data) == "image/png"  # scene1, scene2, neutral, then type cover


async def test_scene_writer_returns_both_versions():
    nv = FakeNvidia()
    w = Writer(nv.client(), "m", 0.8)
    scenes = await w.image_scene("post", "insight")
    assert len(scenes) == 2 and scenes[1].startswith("A laptop and a whiteboard")
