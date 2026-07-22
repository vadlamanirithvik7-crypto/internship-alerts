"""Regression tests for the tracker-feed term gate.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_simplify.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller.sources.simplify import _wanted_term


# (terms, expected_keep)
CASES = [
    # Real season info is honored.
    (["Summer 2026"], True),
    (["Fall 2026"], True),
    (["Summer 2027"], True),
    (["Co-op"], True),
    # New-grad / full-time rows sharing the feed format are excluded.
    (["Full Time"], False),
    (["New Grad"], False),
    # Placeholder / missing terms defer to the internship keyword check (kept
    # here). Google's "Student Researcher" rows carry ['N/A'] and were dropped
    # outright before this fix.
    ([], True),
    ([""], True),
    (["N/A"], True),
    (["n/a"], True),
    (["TBD"], True),
    (["None"], True),
    # A placeholder mixed with a real season still honors the season.
    (["N/A", "Summer 2026"], True),
    # A placeholder mixed with a full-time term is still excluded.
    (["N/A", "Full Time"], False),
]


def run():
    failures = []
    for terms, expected in CASES:
        actual = _wanted_term(terms)
        if actual != expected:
            failures.append(f"_wanted_term({terms!r}) == {actual}, expected {expected}")

    if failures:
        print(f"FAILED ({len(failures)} of {len(CASES)} checks)")
        for failure in failures:
            print("  -", failure)
        return 1

    print(f"PASSED - {len(CASES)} checks")
    return 0


def test_simplify_terms():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
