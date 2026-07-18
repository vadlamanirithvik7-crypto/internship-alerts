"""Shared HTTP helpers for all harvesters.

Several of these free endpoints reject requests without a browser-like User-Agent
(GitHub raw and most ATS APIs among them), so every request goes through here.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30

_session = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return _session


def get_json(url, *, params=None, headers=None, timeout=DEFAULT_TIMEOUT, retries=2):
    """GET and parse JSON, returning None instead of raising on failure.

    Harvesters run unattended on a schedule, so one dead endpoint must never take
    down the whole poll - callers treat None as "no results from this source".
    """
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                return None  # company/board doesn't exist - expected during resolution
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("rate limited by %s, sleeping %ss", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries:
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
