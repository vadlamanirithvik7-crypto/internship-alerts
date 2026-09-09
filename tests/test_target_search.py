import pytest
from shared.eligibility import eligible, confirmed_us


@pytest.mark.parametrize(
    "location",
    [
        "Austin, TX",
        "Seattle, Washington",
        "Remote, US",
        "Remote (USA)",
        "New York, NY; Toronto, Canada",
        "Paris, TX",
        "Atlanta, GA",
        "Atlanta, Georgia, USA",
    ],
)
def test_confirmed_us_options(location):
    assert confirmed_us(location)


@pytest.mark.parametrize(
    "location",
    [
        "",
        None,
        "Remote",
        "Anywhere",
        "North America",
        "Toronto, Canada",
        "London, UK",
        "Georgia",
        "Tbilisi, Georgia",
        "Remote - Europe",
    ],
)
def test_ambiguous_or_foreign_location_is_not_us(location):
    assert not confirmed_us(location)


@pytest.mark.parametrize(
    "title,term,description,wanted",
    [
        ("Software Engineering Intern", "Summer 2027", "", True),
        ("Summer 2027 Hardware Co-op", "", "", True),
        ("Software Engineering Intern - 2027 Summer", "", "", True),
        (
            "Software Engineering Intern",
            "",
            "Join our Summer 2027 internship program.",
            True,
        ),
        (
            "Software Engineering Intern",
            "Summer 2026",
            "Students graduating in 2027 welcome.",
            False,
        ),
        ("Software Engineering Intern", "Fall 2027", "", False),
        ("Software Engineering Intern", "2027", "", False),
        ("Senior Software Engineer", "", "We also hire summer 2027 interns.", False),
    ],
)
def test_term_and_role_need_positive_evidence(title, term, description, wanted):
    assert eligible(title, "Austin, TX", term, description) is wanted
