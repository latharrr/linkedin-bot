from app.pipeline import need_question


def test_need_line_becomes_a_question():
    assert need_question("NEED: What actually broke the first time?") == "What actually broke the first time?"
    assert need_question("  need: how long did it take?\nextra") == "how long did it take?"


def test_normal_rewrite_is_not_a_question():
    assert need_question("Your dashboard lies.\n\nNEED to check it.") is None
    assert need_question("") is None
