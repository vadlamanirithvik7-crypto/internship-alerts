"""Explicit evidence for the owner's United States / summer 2027 search."""

import re
from poller.normalize import clean_location, US_STATE_NAMES, US_STATE_ABBREVS
from shared.sectors import is_internship
from shared.role_search import role_tags

SEARCH_VERSION = 3

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
            if name.lower() != "georgia"
        ):
            return True
        # Georgia is also a country, including city-qualified locations such as
        # Tbilisi, Georgia. Require the US marker or GA code checked above.
    return False


def summer_2027(title, term="", description=""):
    return any(_SUMMER.search(value or "") for value in (term, title, description))


def is_coop(title, term=""):
    """Exclude roles explicitly labeled co-op, including internship/co-op hybrids."""
    return bool(re.search(r"\b(?:co[\s\-‐‑–—]*ops?|cooperative (?:education|program))\b", f"{title or ''} {term or ''}", re.I))


def eligible(title, location, term="", description=""):
    return bool(
        confirmed_us(location)
        and summer_2027(title, term, description)
        and is_internship(title or "", "", term or "")
        and not is_coop(title, term)
        and role_tags(title)
        and not restriction_reasons(title, description)
    )


def _normalized(text):
    text = (text or "").lower().replace("’", "'")
    for pattern, replacement in [(r"\bu\.?s\.?a?\b\.?", "us"),
                                 (r"\bph\.?\s?d\b\.?", "phd"),
                                 (r"\bm\.s\b\.?", "masters"),
                                 (r"\bb\.s\b\.?", "bachelors")]:
        text = re.sub(pattern, replacement, text)
    return text


def restriction_reasons(title, description=""):
    """Flag stated hard restrictions; preferences and bachelor's alternatives pass."""
    reasons = []
    title = _normalized(title)
    graduate = r"\b(?:master(?:'s|s)|master(?= (?:degree|of science|of engineering|program))|ms|msc|meng|mba|phd|doctoral|doctorate|postgraduate|graduate (?:students?|intern|research|degree|program))\b"
    bachelor = r"\b(?:bachelor'?s?|undergrad(?:uate)?s?|bs|bsc)\b"
    if (re.search(graduate, title) or re.search(r"\bgraduate\b", title)) and not re.search(bachelor, title):
        reasons.append("Graduate-degree role")
    # Normalize degree abbreviations before splitting to keep Ph.D./M.S. intact.
    clauses = re.split(r"[\n.;]+", _normalized(description))
    for clause in clauses:
        if re.search(graduate, clause) and not re.search(bachelor, clause):
            required = re.search(r"\b(?:must|required|requirement|minimum|pursuing|enrolled|working toward|currently|candidate|students? in|students? of|seeking|studying)\b", clause)
            optional = re.search(r"\b(?:preferred|preferably|optional|not required|not necessary)\b", clause)
            if required and not optional:
                reasons.append("Master's or PhD required")
        citizen = re.search(r"\b(?:us (?:citizen(?:ship)?s?|national(?:ity)?s?|persons?)|united states citizen(?:ship)?s?|citizenship|(?:lawful |legal )?permanent residents?|permanent residency|green[ -]card(?: holders?)?|usc\s*[/|]\s*gc)\b", clause)
        if not citizen:
            continue
        if re.search(r"(?:citizenship|residen(?:cy|t)|green[ -]card).{0,25}(?:not required|not necessary)|(?:regardless of|without regard to|do not require|does not require|no requirement for)", clause):
            continue
        if re.search(r"\b(?:must|required|requires?|only|restricted|limited|need to|have to|shall|condition of|to comply|itar|export control|eligible applicants|open to|applicants (?:are|should)|candidates (?:are|should))\b", clause):
            reasons.append("Citizenship / permanent-residency restriction")
    return list(dict.fromkeys(reasons))
