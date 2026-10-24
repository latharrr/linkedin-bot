import pytest

from app.db import SimilarTopic
from app.pipeline import PipelineError, generate, regenerate, run_brief, unverified_for
from app.telegram_ui import FOOTER, draft_keyboard, draft_tools, split_text
from tests.conftest import CHAT_ID
from tests.fakes import user_text


async def test_run_brief_inserts_row_and_sends_sequence_in_order(svc, fakes):
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    row = fakes["db"].posts[post.id]
    assert row["status"] == "awaiting_choice"
    assert row["template"] == "story_failure_first"
    assert len(row["topic_embedding"]) == 2048
    assert row["research"]["outline"]["insight"]
    assert len(row["research"]["results"]) == 8
    # ONE post: the final editor merged both candidates, then the de-AI pass ran on it
    assert "[edited] final post" in row["draft_a"] and row["draft_b"] == ""
    final = fakes["nvidia"].final_prompts[0]
    assert "story v" in final and "contrarian v" in final  # both candidates reached the editor
    assert len(fakes["db"].uploads) == 1 and all(p.startswith(post.id) for p in fakes["db"].uploads)

    m = fakes["messenger"]
    assert m.kinds() == ["photo", "text", "text"]
    assert m.sent[0][2] == row["image_a_url"]
    texts = m.texts()
    assert texts[0].startswith("YOUR POST") and texts[1] == FOOTER
    assert m.keyboards() == [draft_tools(post.id, "a"), draft_keyboard(post.id)]


async def test_run_brief_reports_stages_in_order(svc, fakes):
    stages: list[str] = []
    await run_brief(svc, CHAT_ID, "student founders quitting", on_stage=stages.append)
    assert stages == ["researching", "outlining", "writing", "making the image", "saving"]


async def test_pipeline_step_inputs(svc, fakes):
    fakes["db"].similar = SimilarTopic(id="x", topic="Why student startups die", similarity=0.91)
    await generate(svc, "student founders quitting")
    outline_call = next(c for c in fakes["nvidia"].chat_calls if "Return ONLY JSON" in user_text(c))
    assert '"Why student startups die"' in user_text(outline_call)
    assert "find a fresh angle" in user_text(outline_call)
    assert fakes["tavily"].queries[0][0].startswith("student founders quitting statistics 20")
    # 1 outline + 1 outline audit + 2 candidates + 1 final editor + 1 de-AI + 1 polish audit + 1 final audit
    # + 1 image scene + 1 cover headline
    assert len(fakes["nvidia"].chat_calls) == 10
    assert len(fakes["nvidia"].audit_prompts) == 3 and "student founders quitting" in fakes["nvidia"].audit_prompts[0]
    assert len(fakes["nvidia"].image_calls) == 1
    flux = fakes["nvidia"].image_calls[0]
    assert "co-working space at 2am" in flux["prompt"]  # the scene the post describes, not the hook
    assert (flux["width"], flux["height"], flux["steps"]) == (1088, 1344, 50)
    # the brief is embedded once, as a retrieval *query*
    assert [c["input_type"] for c in fakes["nvidia"].embed_calls] == ["query"]


async def test_missing_voice_profile_fails_before_paid_calls(svc, fakes):
    fakes["db"].voice = None
    with pytest.raises(PipelineError, match="extract_voice"):
        await run_brief(svc, CHAT_ID, "x")
    assert fakes["nvidia"].chat_calls == [] and fakes["tavily"].queries == [] and fakes["nvidia"].embed_calls == []


async def test_blank_current_role_fails(svc, fakes):
    fakes["db"].voice = {"current_role": "  "}
    with pytest.raises(PipelineError):
        await generate(svc, "x")


async def test_image_model_down_still_delivers_drafts_with_text_cards(svc, fakes):
    fakes["nvidia"].fail_images = True  # e.g. CONTENT_FILTERED or an outage
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"
    assert all(data.startswith(b"\x89PNG") for data in fakes["db"].uploads.values())  # local text cards
    assert fakes["messenger"].texts()[-1] == FOOTER


async def test_unverified_numbers_shown_per_draft(svc, fakes):
    post = fakes["db"].add_post(
        draft_a="63% of founders quit", draft_b="47% of founders quit", brief="b",
        research={"results": [{"title": "t", "url": "https://u", "content": "47 percent"}], "outline": {}},
    )
    from app.pipeline import preview

    await preview(svc, post)
    texts = fakes["messenger"].texts()
    assert texts[0].endswith("⚠ unverified numbers: 63%")
    assert "⚠" not in texts[1]


def test_brief_numbers_count_as_sources():
    assert unverified_for({"a": "we hit 12K users"}, {"results": []}, "we have 12,000 users") == {"a": []}


async def test_regen_updates_same_row_and_resets_flags(svc, fakes):
    db = fakes["db"]
    post = db.add_post(edited_a=True, edited_b=True, chosen="a", draft_a="old A", draft_b="old B")
    before = len(db.posts)
    updated = await regenerate(svc, post)
    assert len(db.posts) == before  # no new row
    row = db.posts[post.id]
    assert row["edited_a"] is False and row["edited_b"] is False
    assert row["chosen"] is None
    assert row["draft_a"] != "old A" and row["draft_b"] == ""  # regen makes one new post
    assert row["image_a_url"] != "https://storage.example/a.png"
    assert updated is not None and updated.id == post.id
    # steps 6–11 only: no outline call, no new research
    assert not any("Recently covered (find a fresh angle" in user_text(c) for c in fakes["nvidia"].chat_calls)  # no new outline
    assert fakes["tavily"].queries == []
    assert fakes["messenger"].texts()[-1] == FOOTER


async def test_regen_discarded_if_post_locked_meanwhile(svc, fakes):
    db = fakes["db"]
    post = db.add_post()
    db.posts[post.id]["status"] = "queued"  # user picked a time while regen was running
    assert await regenerate(svc, post) is None
    assert db.posts[post.id]["draft_a"] == "draft A text"
    assert fakes["messenger"].sent == []


def test_split_text_long_messages():
    text = ("line\n" * 2000).strip()
    chunks = split_text(text, 4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(c + "\n" for c in chunks).count("line") == 2000


# ── copy guard ──────────────────────────────────────────────────────────────
ARTICLE_TEXT = (
    "Agents run in a loop: an LLM decides what to do, a tool executes, a model evaluates the results, "
    "and then continues in that loop until the task is complete. Agents and LLMs were initially difficult "
    "to integrate into software applications, which depend on structured data and predictable interfaces."
)


def test_copied_ratio_separates_copying_from_quoting():
    from app.verify import copied_ratio

    assert copied_ratio(ARTICLE_TEXT, [ARTICLE_TEXT]) == 1.0
    own = (
        "I built a bot that won't post without two taps from me. Here's why that matters more than the model: "
        'as one LangChain post puts it, agents "run in a loop" and the loop needs a human checkpoint somewhere.'
    )
    assert copied_ratio(own, [ARTICLE_TEXT]) == 0.0


async def test_copying_draft_is_rewritten_once(svc, fakes, monkeypatch):
    from app.pipeline import write_drafts

    calls = []
    original = svc.writer.draft

    async def draft(which, *args, rewrite_note=""):
        calls.append((which, bool(rewrite_note)))
        if which == "b" and not rewrite_note:
            return ARTICLE_TEXT  # a fallback model reproducing the linked article
        return await original(which, *args, rewrite_note=rewrite_note)

    monkeypatch.setattr(svc.writer, "draft", draft)
    research = {"results": [{"title": "LangChain", "url": "https://l", "content": ARTICLE_TEXT}]}
    drafts = await write_drafts(svc, "brief", {"sub_template": "insight_list"}, research, {"current_role": "x"}, [])
    assert sorted(calls) == [("a", False), ("b", False), ("b", True)]
    assert "Agents run in a loop" not in drafts["b"]


def test_preview_flags_a_draft_that_still_copies():
    from app.pipeline import copied_for
    from app.telegram_ui import draft_message

    flags = copied_for({"a": "my own words entirely here", "b": ARTICLE_TEXT}, {"results": [{"content": ARTICLE_TEXT}]})
    assert flags == {"a": False, "b": True}
    assert "⚠ copies sentences from a source" in draft_message("b", ARTICLE_TEXT, [], flags["b"])
    assert "⚠ copies" not in draft_message("a", "text", [], flags["a"])


async def test_enrichment_reads_retry_then_carry_on(svc, fakes, monkeypatch):
    calls = {"n": 0}

    async def flaky(embedding, k=3):
        calls["n"] += 1
        raise RuntimeError("StreamReset")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(fakes["db"], "match_past_posts", flaky)
    monkeypatch.setattr("app.pipeline.asyncio.sleep", no_sleep)
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    assert calls["n"] == 2 and fakes["db"].posts[post.id]["status"] == "awaiting_choice"


async def test_outline_with_unsupported_angle_is_regenerated_with_the_problems_named(svc, fakes):
    from app.pipeline import grounded_outline

    audits = iter([["LinkedIn flagged my post as AI slop"], []])
    notes = []
    real_outline = svc.writer.outline

    async def audit(post, brief, facts):
        return next(audits)

    async def outline(*args, note=""):
        notes.append(note)
        return await real_outline(*args, note=note)

    svc.writer.audit_claims = audit
    svc.writer.outline = outline
    await grounded_outline(svc, "my bot copied an article", "research", "none", [], {"current_role": "x", "background": ["fact"]})
    assert notes[0] == "" and "LinkedIn flagged my post as AI slop" in notes[1] and "outranks the research" in notes[1]


async def test_outline_hooks_still_flagged_after_retry_are_dropped(svc):
    from app.pipeline import grounded_outline

    async def outline(*args, note=""):
        return {"insight": "i", "hooks": ["LinkedIn flagged my post as AI slop today", "My copy check caught a copied opening"], "sub_template": "story_cold_open"}

    async def audit(post, brief, facts):
        return ["LinkedIn flagged my post as AI slop today"]

    svc.writer.outline, svc.writer.audit_claims = outline, audit
    out = await grounded_outline(svc, "b", "r", "none", [], {"current_role": "x"})
    assert out["hooks"] == ["My copy check caught a copied opening"]


async def test_polish_runs_a_second_round_for_what_the_first_missed():
    from app.pipeline import polish

    class TwoRounds:
        def __init__(self):
            self.audits = iter([["LinkedIn flagged my post."], ["A second pass landed at 0.01."], []])
            self.fixes = 0

        async def audit_claims(self, post, brief, facts):
            return next(self.audits)

        async def fix(self, text, issues):
            self.fixes += 1
            return text.replace("LinkedIn flagged my post.", "My copy check flagged the draft.") if self.fixes == 1 else text.replace(
                "A second pass landed at 0.01.", "My normal drafts score 0.01."
            )

    text = "LinkedIn flagged my post.\n\nA second pass landed at 0.01.\n\nWhat would you check?"
    w = TwoRounds()
    out = await polish(w, text, corpus="0.01", brief="normal drafts 0.01", facts=[])
    assert w.fixes == 2 and "LinkedIn flagged" not in out and "second pass" not in out


async def test_post_gutted_by_the_audit_is_rewritten_whole(svc, fakes, monkeypatch):
    from app import pipeline

    notes = []
    real_final = svc.writer.final

    async def final(*args, note=""):
        notes.append(note)
        return await real_final(*args, note=note)

    async def gutting_polish(writer, text, corpus, brief="", facts=None, rounds=2, seen=None, sources=""):
        if seen is not None:  # the first (full) polish: flags claims and leaves a fragment
            seen.append("Our audit log meets legal compliance standards.")
            return text[:10]
        return text

    svc.writer.final = final
    monkeypatch.setattr(pipeline, "polish", gutting_polish)
    post = await run_brief(svc, CHAT_ID, "why agents need approval gates")
    assert len(notes) == 2 and "Our audit log meets legal compliance standards." in notes[1]
    assert len(fakes["db"].posts[post.id]["draft_a"]) > 10  # the whole rewrite, not the fragment


async def test_image_upload_retries_with_a_fresh_name(svc, fakes, monkeypatch):
    from app import pipeline

    paths = []
    real = fakes["db"].upload_png

    async def flaky(path, data):
        paths.append(path)
        if len(paths) == 1:
            raise RuntimeError("ReadTimeout")
        return await real(path, data)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(fakes["db"], "upload_png", flaky)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_sleep)
    post = await run_brief(svc, CHAT_ID, "student founders quitting")
    assert len(paths) == 2 and paths[0] != paths[1]
    assert fakes["db"].posts[post.id]["status"] == "awaiting_choice"
