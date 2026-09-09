"""Shared HTTP helpers for all harvesters.

Several of these free endpoints reject requests without a browser-like User-Agent
(GitHub raw and most ATS APIs among them), so every request goes through here.
"""

import logging
import time
import threading

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30

_local = threading.local()


def session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
    return _local.session


def reset_observation():
    _local.failures = 0


def failed_requests():
    return getattr(_local, "failures", 0)


def failed():
    _local.failures = failed_requests() + 1


def get_json(url, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=2):
    """GET and parse JSON, returning None instead of raising on failure.

    Harvesters run unattended on a schedule, so one dead endpoint must never take
    down the whole poll - callers treat None as "no results from this source".
    """
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                failed()
                return None  # company/board doesn't exist - expected during resolution
            if resp.status_code == 429:
                wait = 2**attempt
                log.warning("rate limited by %s, sleeping %ss", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries:
                failed()
                log.warning("giving up on %s: %s", url, exc)
                return None
            time.sleep(1 + attempt)
    failed()
    return None


def get_text(url, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=2):
    """GET and return response text, or None on failure. Same retry policy as get_json.

    Used for sources that serve markdown/HTML (tracker READMEs) rather than JSON.
    """
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                failed()
                return None
            if resp.status_code == 429:
                wait = 2**attempt
                log.warning("rate limited by %s, sleeping %ss", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries:
                failed()
                log.warning("giving up on %s: %s", url, exc)
                return None
            time.sleep(1 + attempt)
    return None


def post(url, *, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    try:
        resp = session().post(url, data=data, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.warning("POST %s failed: %s", url, exc)
        return False
