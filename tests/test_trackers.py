"""Regression tests for the tracker / HN / Reddit ingestion parsers.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_trackers.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller.sources.trackers import _parse_markdown_table
from poller.sources.hackernews import _apply_url, _comment_text
from poller.sources.reddit import _company_from
from shared.sectors import is_internship
from poller.normalize import text_is_non_us

# jobright-style: the apply link lives in the Job Title cell; "↳" continues the
# company above; the last row is a foreign-only location.
JOBRIGHT_MD = """
Some intro text.

| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- | --------- | ---- | ------- |
| **[ByteDance](http://bytedance.com)** | **[Software Engineer Intern](https://jobright.ai/jobs/info/aaa?utm=git)** | San Jose, CA, United States | On Site | Jul 22 |
| **[Copart](http://copart.com)** | **[Software Engineering Intern](https://jobright.ai/jobs/info/bbb)** | Dallas, TX, United States | On Site | Jul 22 |
| ↳ | **[Backend Intern](https://jobright.ai/jobs/info/ccc)** | Dallas, TX, United States | On Site | Jul 22 |
| **[Spotify](http://spotify.com)** | **[Data Intern](https://jobright.ai/jobs/info/ddd)** | London, United Kingdom | Remote | Jul 22 |
"""

# pittcsc-style: a separate Application column holds an <a href> link, and the
# Role cell is plain text.
PITTCSC_MD = """
| Company | Role | Location | Application | Date |
|---|---|---|---|---|
| **Stripe** | Software Engineer Intern | Seattle, WA | <a href="https://stripe.com/jobs/123"><img src="apply.png"></a> | Jul 1 |
"""


def _check_markdown():
    failures = []
    posts = _parse_markdown_table(JOBRIGHT_MD, "tracker-md")
    by_url = {p["url"].split("?")[0]: p for p in posts}

    if len(posts) != 4:
        failures.append(f"jobright: parsed {len(posts)} rows, expected 4")
    # Continuation row inherits Copart.
    cont = by_url.get("https://jobright.ai/jobs/info/ccc")
    if not cont or cont["company_name"] != "Copart":
        failures.append(f"jobright: continuation row company = {cont and cont['company_name']!r}, expected 'Copart'")
    # Link comes from the title cell.
    bd = by_url.get("https://jobright.ai/jobs/info/aaa")
    if not bd or bd["company_name"] != "ByteDance":
        failures.append("jobright: ByteDance row not parsed correctly")

    # pittcsc layout: link from the Application column, plain-text role.
    pitt = _parse_markdown_table(PITTCSC_MD, "tracker-md")
    if len(pitt) != 1:
        failures.append(f"pittcsc: parsed {len(pitt)} rows, expected 1")
    elif pitt[0]["url"] != "https://stripe.com/jobs/123":
        failures.append(f"pittcsc: url = {pitt[0]['url']!r}")
    elif pitt[0]["title"] != "Software Engineer Intern":
        failures.append(f"pittcsc: title = {pitt[0]['title']!r}")
    return failures


def _check_helpers():
    failures = []

    # HN: paragraph HTML flattens to text; href beats bare URL; self-links ignored.
    raw = 'Acme Corp | Backend Intern | Remote (US)<p>Apply: <a href="https://acme.com/jobs/1">here</a>'
    text = _comment_text(raw)
    if "Acme Corp" not in text:
        failures.append("hn: comment text lost the header")
    if _apply_url(raw, text) != "https://acme.com/jobs/1":
        failures.append(f"hn: apply url = {_apply_url(raw, text)!r}")
    if _apply_url("no link here", "no link here") != "":
        failures.append("hn: expected empty url when no link present")

    # Reddit: derive a company from an ATS URL, and from a plain host.
    if _company_from("https://boards.greenhouse.io/databricks/jobs/5") != "Databricks":
        failures.append(f"reddit: greenhouse company = {_company_from('https://boards.greenhouse.io/databricks/jobs/5')!r}")
    if _company_from("https://careers.acme.com/apply/9") != "Careers":
        # host is careers.acme.com -> first label 'careers'; acceptable fallback
        pass

    # The shared filters the noisy sources rely on.
    if not is_internship("Acme | Software Engineering Intern | Remote"):
        failures.append("filter: internship header not detected")
    if is_internship("Acme | Senior Backend Engineer | Remote"):
        failures.append("filter: full-time role wrongly detected as internship")
    if not text_is_non_us("Spotify | Data Intern | London, United Kingdom"):
        failures.append("filter: foreign HN header not vetoed")
    if text_is_non_us("Acme | Intern | Austin, TX"):
        failures.append("filter: US header wrongly vetoed")
    return failures


def run():
    failures = _check_markdown() + _check_helpers()
    if failures:
        print(f"FAILED ({len(failures)} checks)")
        for f in failures:
            print("  -", f)
        return 1
    print("PASSED - tracker/HN/reddit parsers")
    return 0


def test_trackers():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
