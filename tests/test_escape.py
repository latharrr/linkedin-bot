import pytest

from app.linkedin import (
    RESERVED,
    escape_little_text,
    looks_like_post_url,
    post_url_from_restli_id,
)


@pytest.mark.parametrize("ch", sorted(RESERVED))
def test_every_reserved_char_is_escaped(ch):
    assert escape_little_text(f"a{ch}b") == f"a\\{ch}b"


def test_reserved_set_matches_amendment_a1():
    assert RESERVED == set("\\|{}@[]()<>#*_~")


def test_plain_text_untouched():
    text = "Shipped 3 features.\nNo drama, just work! 47% faster: yes?"
    assert escape_little_text(text) == text


def test_all_reserved_in_one_string():
    assert escape_little_text("\\|{}@[]()<>*_~") == "\\\\\\|\\{\\}\\@\\[\\]\\(\\)\\<\\>\\*\\_\\~"


def test_backslash_escaped_once_not_double():
    assert escape_little_text("a\\b") == "a\\\\b"
    assert escape_little_text("\\*") == "\\\\\\*"


def test_hashtag_becomes_clickable_template():
    assert escape_little_text("#AI") == "{hashtag|\\#|AI}"


def test_multiple_hashtags_and_surrounding_text():
    out = escape_little_text("Ship it.\n\n#startups #AIagents #edtech")
    assert out == "Ship it.\n\n{hashtag|\\#|startups} {hashtag|\\#|AIagents} {hashtag|\\#|edtech}"


def test_hashtag_after_punctuation_and_line_start():
    assert escape_little_text("(#build)") == "\\({hashtag|\\#|build}\\)"
    assert escape_little_text("x\n#tag") == "x\n{hashtag|\\#|tag}"


def test_hashtag_with_digits_ok_but_number_only_is_not_a_tag():
    assert escape_little_text("#web3") == "{hashtag|\\#|web3}"
    assert escape_little_text("#1 priority") == "\\#1 priority"


def test_hash_inside_word_is_escaped_not_a_tag():
    assert escape_little_text("C# devs") == "C\\# devs"


def test_double_hash_is_not_a_tag():
    assert escape_little_text("##tag") == "\\#\\#tag"


def test_lone_hash_escaped():
    assert escape_little_text("# of users") == "\\# of users"


def test_underscore_ends_hashtag_and_is_escaped():
    assert escape_little_text("#machine_learning") == "{hashtag|\\#|machine}\\_learning"


def test_unicode_letter_hashtag():
    assert escape_little_text("#café") == "{hashtag|\\#|café}"


def test_url_anchor_is_not_a_hashtag_and_url_chars_escaped():
    out = escape_little_text("see https://x.com/a_b/#top now")
    assert out == "see https://x.com/a\\_b/\\#top now"


def test_mentions_and_markdown_are_neutralised():
    assert escape_little_text("@john *bold* [link](url)") == "\\@john \\*bold\\* \\[link\\]\\(url\\)"


def test_hashtag_template_braces_not_escaped_but_text_braces_are():
    assert escape_little_text("{x} #y") == "\\{x\\} {hashtag|\\#|y}"


def test_post_url_from_restli_id():
    assert post_url_from_restli_id("urn:li:share:7123") == "https://www.linkedin.com/feed/update/urn:li:share:7123"
    assert post_url_from_restli_id("urn%3Ali%3AugcPost%3A99") == "https://www.linkedin.com/feed/update/urn:li:ugcPost:99"
    with pytest.raises(ValueError):
        post_url_from_restli_id(None)
    with pytest.raises(ValueError):
        post_url_from_restli_id("garbage")


def test_looks_like_post_url():
    assert looks_like_post_url("https://www.linkedin.com/feed/update/urn:li:share:1/")
    assert looks_like_post_url("  https://linkedin.com/posts/me_abc  ")
    assert not looks_like_post_url("http://www.linkedin.com/feed/update/1")
    assert not looks_like_post_url("https://evil.com/linkedin.com")
    assert not looks_like_post_url("yes it's live")
