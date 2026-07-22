"""Regression tests for US-location filtering.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_location.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller.normalize import is_us_location


# (location, expected_keep)
CASES = [
    # Clear US locations are kept.
    ("San Francisco, CA", True),
    ("New York, NY, United States", True),
    ("Austin, TX 78701", True),
    ("Seattle, Washington", True),
    ("Remote, US", True),
    ("Remote (USA)", True),
    ("Boston, MA; Chicago, IL", True),
    ("Cambridge, MA", True),
    ("San Jose, CA", True),
    ("Puerto Rico", True),
    # Ambiguous / empty locations get the benefit of the doubt.
    ("", True),
    (None, True),
    ("Remote", True),
    ("Multiple Locations", True),
    ("Anywhere", True),
    # Clearly foreign locations are dropped.
    ("London, United Kingdom", False),
    ("London", False),
    ("Berlin, Germany", False),
    ("Berlin", False),
    ("Toronto, Canada", False),
    ("Toronto, ON", False),
    ("Bengaluru, India", False),
    ("Bangalore", False),
    ("Munich", False),
    ("Paris, France", False),
    ("Tel Aviv, Israel", False),
    ("Singapore", False),
    ("Sydney, Australia", False),
    ("Remote - Europe", False),
    ("Remote (EMEA)", False),
    ("London; Berlin; Amsterdam", False),
    # Mixed lists keep the posting if any option is US-based.
    ("New York, NY; London, UK", True),
    ("Remote, US; Toronto", True),
    # US namesakes of foreign cities: the state tag wins.
    ("Berlin, CT", True),
    ("Dublin, OH", True),
    ("Paris, TX", True),
    ("London, KY", True),
    ("Vancouver, WA", True),
    ("Ontario, CA", True),
    # "New Mexico" must not be read as the country Mexico.
    ("Albuquerque, New Mexico", True),
    # A foreign-looking token embedded in a word must not trigger a state match.
    ("Berlin, Germany", False),
]


def run():
    failures = []
    for location, expected in CASES:
        actual = is_us_location(location)
        if actual != expected:
            failures.append(
                f"is_us_location({location!r}) == {actual}, expected {expected}"
            )

    if failures:
        print(f"FAILED ({len(failures)} of {len(CASES)} checks)")
        for failure in failures:
            print("  -", failure)
        return 1

    print(f"PASSED - {len(CASES)} checks")
    return 0


def test_location():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
