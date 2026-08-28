"""Regression tests for internship detection and sector tagging.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_sectors.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.sectors import is_internship, tag_posting


INTERNSHIP_CASES = [
    ("Software Engineer Intern", True),
    ("Summer 2026 Analyst Internship", True),
    ("Hardware Engineering Co-op", True),
    ("Electrical Engineering Co-Op - Fall 2026", True),
    # Google's research-internship title names neither "intern" nor "co-op".
    ("Student Researcher", True),
    # "intern" appears as a substring but the role is not an internship.
    ("Internal Audit Analyst", False),
    ("International Sales Manager", False),
    ("Senior Staff Software Engineer", False),
    ("Director of Internal Communications", False),
]

TAGGING_CASES = [
    # (title, must_include, must_exclude)
    ("Analog IC Design Intern (VLSI)", {"semiconductor"}, set()),
    ("RTL Design Engineer Intern - SoC", {"computer_architecture"}, set()),
    # ASIC design spans the front end, the back end, test and post-silicon.
    ("ASIC Design Engineer Intern", {"asic_design"}, set()),
    ("Design Verification Intern (UVM/SystemVerilog)", {"asic_design"}, set()),
    ("Physical Design Co-op - Timing Closure", {"asic_design"}, set()),
    ("DFT Intern - Scan Insertion and ATPG", {"asic_design"}, set()),
    ("Post-Silicon Validation Intern", {"asic_design"}, set()),
    ("Summer Intern, Place and Route / Floorplanning", {"asic_design"}, set()),
    # Acronyms with a common non-ASIC meaning are deliberately not tokens, so
    # these must stay clear of the ASIC bucket. See SECTORS["asic_design"].
    ("CDC Public Health Intern", set(), {"asic_design"}),
    ("Data Science Intern - EDA and Dashboards", set(), {"asic_design"}),
    ("Staff Accountant Intern", set(), {"asic_design"}),
    ("Power Electronics Co-op - Inverter Design", {"power_electronics"}, set()),
    ("Robotics Perception Intern (SLAM, ROS2)", {"robotics"}, set()),
    ("Software Development Engineer Internship", {"software_tech"}, set()),
    ("Full-Stack Engineer Intern", {"software_tech"}, set()),
    # "research" must not trigger computer_architecture via "arch intern":
    # keyword matching is strictly anchored at the start of a word.
    ("PhD Research Intern", set(), {"computer_architecture"}),
    ("Research Intern, Biology", set(), {"computer_architecture"}),
    # ...but inflected forms of a keyword must still match, since the trailing
    # edge is deliberately loose ("firmware engineer" -> "firmware engineering").
    ("Firmware Engineering Intern/Co-op", {"computer_architecture"}, set()),
    ("Robotics Controls Engineering Intern", {"robotics"}, set()),
    ("Semiconductors Process Intern", {"semiconductor"}, set()),
    # A bare hardware role must not claim a specific discipline it never named.
    ("Hardware Engineer Intern", {"hardware_general"}, {"power_electronics", "semiconductor"}),
    # Unrelated roles stay untagged.
    ("Marketing Intern", set(), {"software_tech", "robotics"}),
]

CATEGORY_HINT_CASES = [
    # A coarse "Hardware" hint falls back to the general bucket only.
    ("Chipsim Intern", "Hardware", {"hardware_general"}, {"power_electronics"}),
]


def run():
    failures = []

    for title, expected in INTERNSHIP_CASES:
        actual = is_internship(title)
        if actual != expected:
            failures.append(f"is_internship({title!r}) == {actual}, expected {expected}")

    for title, must_include, must_exclude in TAGGING_CASES:
        tags = set(tag_posting(title))
        missing = must_include - tags
        wrong = must_exclude & tags
        if missing:
            failures.append(f"tag_posting({title!r}) missing {sorted(missing)} (got {sorted(tags)})")
        if wrong:
            failures.append(f"tag_posting({title!r}) wrongly includes {sorted(wrong)}")

    for title, hint, must_include, must_exclude in CATEGORY_HINT_CASES:
        tags = set(tag_posting(title, "", hint))
        if must_include - tags:
            failures.append(f"tag_posting({title!r}, hint={hint!r}) missing {sorted(must_include - tags)}")
        if must_exclude & tags:
            failures.append(f"tag_posting({title!r}, hint={hint!r}) wrongly includes {sorted(must_exclude & tags)}")

    total = len(INTERNSHIP_CASES) + len(TAGGING_CASES) + len(CATEGORY_HINT_CASES)
    if failures:
        print(f"FAILED ({len(failures)} of {total} checks)")
        for failure in failures:
            print("  -", failure)
        return 1

    print(f"PASSED - {total} checks")
    return 0


def test_sectors():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
