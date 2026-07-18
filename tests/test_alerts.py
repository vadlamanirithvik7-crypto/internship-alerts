"""Regression tests for alert delivery.

Run: python3 tests/test_alerts.py   (no network required - the sender is stubbed)
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poller import alerts  # noqa: E402


class FakePosting:
    def __init__(self, title, company_name="ACME", location="Austin, TX"):
        self.title = title
        self.company_name = company_name
        self.location = location
        self.url = "https://example.com/job"
        self.sector_tags = "|robotics|"


# Real titles seen in live feeds. Every one of these contains a character that is
# not latin-1 encodable, which is what broke the header-based ntfy call in CI.
NON_LATIN1_TITLES = [
    "Amazon Robotics – Applied Scientist Intern",   # en dash
    "Ingenieur Stagiaire — Puissance Électrique",   # em dash + accent
    'Chip Design Intern "Summer 2026"',             # curly quotes
    "机器人实习生",                                   # CJK
]


def test_unicode_titles_do_not_crash():
    """Titles must travel in the JSON body, never in an HTTP header."""
    failures = []

    with mock.patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}):
        with mock.patch.object(alerts.requests, "post") as post:
            post.return_value.raise_for_status.return_value = None

            for title in NON_LATIN1_TITLES:
                try:
                    alerts.send_ntfy([FakePosting(title)])
                except UnicodeEncodeError as exc:
                    failures.append(f"{title!r} raised UnicodeEncodeError: {exc}")

            # Any header value we send must be latin-1 safe.
            for call in post.call_args_list:
                for key, value in (call.kwargs.get("headers") or {}).items():
                    try:
                        str(value).encode("latin-1")
                    except UnicodeEncodeError:
                        failures.append(f"header {key}={value!r} is not latin-1 encodable")

            # And the title must actually be in the JSON payload.
            last = post.call_args_list[-1]
            payload = last.kwargs.get("json") or {}
            if "title" not in payload or "topic" not in payload:
                failures.append(f"expected title+topic in JSON body, got {sorted(payload)}")

    return failures


def test_skips_when_unconfigured():
    """No topic configured must be a clean skip, not an exception."""
    with mock.patch.dict(os.environ, {"NTFY_TOPIC": ""}, clear=False):
        if alerts.send_ntfy([FakePosting("Intern")]) is not False:
            return ["send_ntfy should return False when NTFY_TOPIC is unset"]
    return []


def run():
    failures = test_unicode_titles_do_not_crash() + test_skips_when_unconfigured()
    if failures:
        print(f"FAILED ({len(failures)} problems)")
        for failure in failures:
            print("  -", failure)
        return 1
    print(f"PASSED - {len(NON_LATIN1_TITLES) + 1} checks")
    return 0


if __name__ == "__main__":
    sys.exit(run())
