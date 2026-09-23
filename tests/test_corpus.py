import itertools

import pytest

from app import corpus
from app.db import Post
from app.pipeline import regenerate
from tests.fakes import FakeDB


class Emb:
    async def embed_passage(self, text):
        return [0.0] * 2048


def post(**kw) -> Post:
    return Post(id="00000000-0000-0000-0000-000000000001", chat_id=1, brief="b", **kw)


# ── admission on post: chosen × edited matrix ────────────────────────────────
@pytest.mark.parametrize("chosen,edited_a,edited_b", list(itertools.product(["a", "b"], [False, True], [False, True])))
def test_admission_matrix(chosen, edited_a, edited_b):
    expected = edited_a if chosen == "a" else edited_b
    assert corpus.should_admit_on_post(post(chosen=chosen, edited_a=edited_a, edited_b=edited_b)) is expected


def test_no_choice_never_admitted():
    assert not corpus.should_admit_on_post(post(edited_a=True, edited_b=True))


async def test_admit_inserts_ai_row_with_chosen_text_and_links_it():
    db = FakeDB()
    p = db.add_post(status="posted", chosen="b", edited_b=True, draft_b="my edited B")
    past_id = await corpus.admit_on_post(db, Emb(), p)
    assert db.past[past_id]["source"] == "ai" and db.past[past_id]["text"] == "my edited B"
    assert db.posts[p.id]["past_post_id"] == past_id


async def test_admit_skips_unedited_and_already_admitted():
    db = FakeDB()
    assert await corpus.admit_on_post(db, Emb(), db.add_post(chosen="a", edited_b=True)) is None
    assert await corpus.admit_on_post(db, Emb(), db.add_post(chosen="a", edited_a=True, past_post_id="x")) is None
    assert db.past == {}


async def test_regen_resets_flags_so_regenerated_text_is_not_admitted(svc, fakes):
    db = fakes["db"]
    p = db.add_post(edited_a=True, edited_b=True)
    await regenerate(svc, p)
    db.posts[p.id].update(status="posted", chosen="a")
    after = await db.get_post(p.id)
    assert after.edited_a is False and after.edited_b is False
    assert await corpus.admit_on_post(db, Emb(), after) is None


# ── promotion gate ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("n,open_", [(0, False), (4, False), (5, True), (20, True)])
def test_gate_threshold(n, open_):
    assert corpus.promotion_gate_open(n) is open_


def test_beats_median_is_strict():
    assert corpus.beats_median(31, [10, 20, 30, 40, 50])
    assert not corpus.beats_median(30, [10, 20, 30, 40, 50])
    assert corpus.beats_median(26, [10, 20, 30, 40])  # median 25
    assert not corpus.beats_median(100, [])


async def _humans(db, engagements):
    for e in engagements:
        await db.insert_past_post("h", "human", [0.0], engagement=e)


async def test_gate_counts_only_engaged_human_rows():
    db = FakeDB()
    await _humans(db, [0, 0, 0, 10, 20, 30, 40])  # 4 engaged, 3 unknown
    await db.insert_past_post("ai", "ai", [0.0], engagement=999)  # AI rows never count
    p = db.add_post(status="posted", chosen="a")
    assert await corpus.record_stats(db, Emb(), p, 500, 0) == "gate_closed"
    assert db.posts[p.id].get("past_post_id") is None


async def test_promotion_when_gate_open_and_beats_median():
    db = FakeDB()
    await _humans(db, [0, 10, 20, 30, 40, 50])  # engaged median = 30 (zero row excluded)
    winner = db.add_post(status="posted", chosen="a", draft_a="winner text")
    loser = db.add_post(status="posted", chosen="a")
    assert await corpus.record_stats(db, Emb(), winner, 25, 6) == "promoted"
    assert await corpus.record_stats(db, Emb(), loser, 20, 10) == "below_median"
    row = db.past[db.posts[winner.id]["past_post_id"]]
    assert (row["source"], row["engagement"], row["text"]) == ("ai", 31, "winner text")


async def test_stats_update_existing_corpus_row():
    db = FakeDB()
    past_id = await db.insert_past_post("t", "ai", [0.0])
    p = db.add_post(status="posted", chosen="a", past_post_id=past_id)
    assert await corpus.record_stats(db, Emb(), p, 70, 7) == "updated"
    assert db.past[past_id]["engagement"] == 77
