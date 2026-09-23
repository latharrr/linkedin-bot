import pytest

from app.verify import extract_numbers, normalize, unverified_numbers, warning_line


def check(draft: str, research: str) -> list[str]:
    return unverified_numbers(draft, research)


@pytest.mark.parametrize(
    "draft,research",
    [
        ("Raised $2B last year", "The company raised 2 billion dollars"),
        ("Raised 2 billion", "raised $2B"),
        ("Raised $2B", "raised $2,000,000,000"),
        ("valued at $1.5 billion", "a $1.5B valuation"),
        ("47% of founders", "47 percent of founders"),
        ("47 percent of founders", "47% of founders"),
        ("47 per cent", "47%"),
        ("₹500 crore round", "a 500 crore rupee round"),
        ("₹5 crore", "Rs. 5 Cr"),
        ("₹5 crore", "₹50 million"),  # same magnitude, different unit words
        ("2 lakh students", "200,000 students"),
        ("12 lakh users", "₹12 lakhs"),
        ("3.2 million downloads", "3.2M downloads"),
        ("50K users", "50 thousand users"),
        ("1,20,000 applicants", "120000 applicants"),  # Indian digit grouping
        ("10x faster", "roughly 10X faster"),
        ("In 2025, 38% grew", "a 2025 survey: 38 percent grew"),
    ],
)
def test_equivalent_forms_match(draft, research):
    assert check(draft, research) == []


def test_unmatched_number_is_flagged():
    assert check("Revenue grew 63% in 2024", "Revenue grew 47% in 2024") == ["63%"]


def test_percent_and_plain_are_different_stats():
    assert check("47% said yes", "47 people said yes") == ["47%"]


def test_magnitude_mismatch_flagged():
    assert check("$2B raised", "$2M raised") == ["2B"]


@pytest.mark.parametrize("n", range(1, 11))
def test_list_count_integers_1_to_10_ignored(n):
    assert check(f"{n} lessons I learned", "no numbers here") == []


def test_eleven_and_zero_not_ignored():
    assert check("11 lessons", "nothing") == ["11"]
    assert check("0 customers", "nothing") == ["0"]


def test_small_numbers_with_units_are_checked():
    assert check("5% churn", "nothing") == ["5%"]
    assert check("3x growth", "nothing") == ["3x"]
    assert check("$5 per seat", "nothing") == ["5"]  # money is a stat, not a list count
    assert check("2 billion", "nothing") == ["2B"]
    assert check("2.5 hours", "nothing") == ["2.5"]


def test_duplicates_reported_once_in_order():
    assert check("63% then 71% then 63% again", "") == ["63%", "71%"]


def test_ordinals_and_words_with_digits_are_ignored():
    assert check("the 21st century, B2B and Web3 and H1B", "") == []


def test_unit_letter_not_swallowed_from_following_word():
    tokens = extract_numbers(normalize("5 MBA students and 3 kids"))
    assert [(str(t.value), t.kind) for t in tokens] == [("5", ""), ("3", "")]


def test_normalize_maps_words_and_strips_symbols():
    assert normalize("$2,000 and 47 percent and 3 crore and 2 lakhs") == "¤2000 and 47% and 3 Cr and 2 L"


def test_warning_line():
    assert warning_line([]) == ""
    assert warning_line(["63%", "2B"]) == "⚠ unverified numbers: 63%, 2B"
