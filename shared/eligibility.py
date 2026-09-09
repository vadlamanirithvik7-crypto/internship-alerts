"""Explicit evidence for the owner's United States / summer 2027 search."""

import re
from poller.normalize import clean_location, US_STATE_NAMES, US_STATE_ABBREVS
from shared.sectors import is_internship

_COUNTRY = re.compile(
    r"\b(?:united states(?: of america)?|usa|u\.s\.a\.?|u\.s\.?|us)\b", re.I
)
_STATE = re.compile(r",\s*([A-Z]{2})(?:[\s,\d]|$)")
_SUMMER = re.compile(
    r"\bsummer(?:[\s/-]+(?:internships?|interns?|co-?ops?|program|term|of|for)){0,3}[\s,:/-]*2027\b|\b2027[\s,:/-]*(?:summer)\b",
    re.I,
)


def confirmed_us(location):
    """Unknown remote locations are not evidence of a United States work option."""
    for part in re.split(r"[;|\n/]+", clean_location(location)):
        if _COUNTRY.search(part):
            return True
        # State abbreviations after a city disambiguate US namesake cities.
        match = _STATE.search(part)
        if match and match.group(1).lower() in US_STATE_ABBREVS:
            return True
        if any(
            re.search(r"\b" + re.escape(name) + r"\b", part, re.I)
            for name in US_STATE_NAMES
        ):
            # Georgia is also a country; without a US marker/state code it is ambiguous.
            if not re.fullmatch(r"\s*(?:remote[, -]*)?georgia\s*", part, re.I):
                return True
    return False


def summer_2027(title, term="", description=""):
    return any(_SUMMER.search(value or "") for value in (term, title, description))


def eligible(title, location, term="", description=""):
    return bool(
        confirmed_us(location)
        and summer_2027(title, term, description)
        and is_internship(title or "", "", term or "")
    )
