"""Regression tests for posting dedupe identity.

Every case here is a real collision observed in the live database, where one job
was stored two to four times because the sources disagreed on incidental details.

Run: python3 tests/test_dedupe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller.normalize import canonical_url, make_posting  # noqa: E402
from poller.store import _identity  # noqa: E402


def ident(url, company, title, source="test"):
    return _identity(
        make_posting(company_name=company, title=title, url=url, source=source)
    )


# Groups that MUST collapse to a single identity.
SAME_JOB = [
    (
        "aggregator shortens the title",
        [
            ("https://job-boards.greenhouse.io/accuweather/jobs/7453001",
             "AccuWeather", "Product Analyst Intern"),
            ("https://job-boards.greenhouse.io/accuweather/jobs/7453001",
             "AccuWeather", "Product Analyst Intern (Spring/Summer 2026)"),
        ],
    ),
    (
        "company name recorded differently",
        [
            ("https://job-boards.greenhouse.io/aquaticcapitalmanagement/jobs/8489233002",
             "Aquatic Capital Management", "Software Engineer Intern"),
            ("https://job-boards.greenhouse.io/aquaticcapitalmanagement/jobs/8489233002",
             "Aquatic", "Software Engineer, Intern (Summer 2027)"),
        ],
    ),
    (
        "URL casing and /application suffix differ",
        [
            ("https://jobs.ashbyhq.com/Etched/157ed4f4-6e3b-4ec9-b93f-3e363e92041e/application",
             "Etched", "RTL Intern"),
            ("https://jobs.ashbyhq.com/etched/157ed4f4-6e3b-4ec9-b93f-3e363e92041e",
             "Etched.ai", "RTL Intern"),
            ("https://jobs.ashbyhq.com/Etched/157ed4f4-6e3b-4ec9-b93f-3e363e92041e",
             "Etched", "RTL Intern"),
        ],
    ),
    (
        "tracking params and trailing slash",
        [
            ("https://boards.greenhouse.io/acme/jobs/123?utm_source=x&gh_src=y",
             "Acme", "Hardware Intern"),
            ("https://boards.greenhouse.io/acme/jobs/123/", "Acme", "Hardware Intern"),
        ],
    ),
]

# Genuinely different jobs that must NOT collapse.
DIFFERENT_JOBS = [
    (
        "different job ids at the same company",
        ("https://job-boards.greenhouse.io/acme/jobs/111", "Acme", "SWE Intern"),
        ("https://job-boards.greenhouse.io/acme/jobs/222", "Acme", "SWE Intern"),
    ),
    (
        "same title at different companies",
        ("https://jobs.lever.co/alpha/abc", "Alpha", "Robotics Intern"),
        ("https://jobs.lever.co/beta/xyz", "Beta", "Robotics Intern"),
    ),
    (
        "no URL - falls back to company + title",
        ("", "Gamma", "Power Electronics Intern"),
        ("", "Gamma", "Semiconductor Intern"),
    ),
]


def run():
    failures = []

    for label, rows in SAME_JOB:
        identities = {ident(*row) for row in rows}
        if len(identities) != 1:
            failures.append(f"{label}: expected 1 identity, got {len(identities)}")

    for label, a, b in DIFFERENT_JOBS:
        if ident(*a) == ident(*b):
            failures.append(f"{label}: distinct jobs collapsed to the same identity")

    # canonical_url should be stable and lowercase.
    if canonical_url("https://Jobs.AshbyHQ.com/Etched/abc/application") != \
       canonical_url("https://jobs.ashbyhq.com/etched/abc"):
        failures.append("canonical_url not stable across casing/suffix")

    total = len(SAME_JOB) + len(DIFFERENT_JOBS) + 1
    if failures:
        print(f"FAILED ({len(failures)} of {total})")
        for failure in failures:
            print("  -", failure)
        return 1
    print(f"PASSED - {total} checks")
    return 0


if __name__ == "__main__":
    sys.exit(run())
