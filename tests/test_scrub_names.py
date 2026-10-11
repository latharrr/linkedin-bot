from app.pipeline import scrub_names


def test_names_are_replaced():
    out = scrub_names("At PicaPool I built it. PicaPool's team used ProofMart and UniLyf and Gapl.")
    for name in ("PicaPool", "ProofMart", "UniLyf", "Gapl"):
        assert name not in out
    assert out.startswith("At the startup I work at I built it.")


def test_text_without_names_unchanged():
    assert scrub_names("Count the signups in your database.") == "Count the signups in your database."
