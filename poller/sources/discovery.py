"""Company discovery: find companies in our target sectors, by name.

Originally scoped as "pull ETF holdings", but the fund providers (iShares, VanEck)
serve bot-block pages to non-browser clients, and an ETF only exposes its top ~30
holdings anyway. SEC EDGAR's SIC industry browse is free, keyless, reliable, and
returns every registered filer in an industry - hundreds per sector - which is a
strictly better fit for "as many companies as possible".

Discovered names are candidates only. resolver.py must validate each against a live
ATS API before we trust it; unresolved names still feed keyword search.
"""

import logging
import re
import time

from poller.net import session

log = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent identifying the requester.
SEC_UA = "internship-alert-bot (personal project; vadlamani.rithvik7@gmail.com)"
SEC_BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"

# Standard Industrial Classification codes mapped to our sector taxonomy.
SIC_SECTORS = {
    "semiconductor": {
        3674: "Semiconductors & related devices",
        3559: "Special industry machinery (semiconductor equipment)",
        3827: "Laboratory analytical instruments",
        3825: "Instruments for measuring electricity",
    },
    "computer_architecture": {
        3571: "Electronic computers",
        3572: "Computer storage devices",
        3576: "Computer communications equipment",
        3577: "Computer peripheral equipment",
        3661: "Telephone & telegraph apparatus",
        3663: "Radio & TV broadcasting & communications equipment",
        3669: "Communications equipment",
    },
    "power_electronics": {
        3612: "Power distribution & specialty transformers",
        3613: "Switchgear & switchboard apparatus",
        3621: "Motors & generators",
        3629: "Electrical industrial apparatus",
        3691: "Storage batteries",
        3690: "Electrical machinery, equipment & supplies",
        4911: "Electric services",
    },
    "robotics": {
        3812: "Search, detection, navigation & guidance systems",
        3823: "Industrial instruments for measurement & control",
        3711: "Motor vehicles & passenger car bodies",
        3728: "Aircraft parts & auxiliary equipment",
        3541: "Machine tools, metal cutting types",
        3550: "Special industry machinery",
    },
    "software_tech": {
        7372: "Prepackaged software",
        7370: "Computer programming & data processing services",
        7371: "Computer programming services",
        7373: "Computer integrated systems design",
        7374: "Data processing & preparation",
    },
}

# Suffixes stripped when turning a legal name into something board-slug shaped.
LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|lp|plc|"
    r"holdings?|group|technologies|technology|international|sa|nv|ag|gmbh|"
    r"kk|/de/|/ca/|/ny/|com)\b\.?",
    re.I,
)

_ROW_PATTERN = re.compile(r"CIK=\d+[^>]*>\s*(\d+)\s*</a></td>\s*<td[^>]*>(.*?)</td>", re.S)


def clean_company_name(raw: str) -> str:
    """'ADVANCED MICRO DEVICES INC' -> 'Advanced Micro Devices'"""
    name = re.sub(r"&amp;", "&", raw or "")
    name = re.sub(r"<[^>]+>", "", name).strip()
    name = re.sub(r"/[A-Z]{2}/?$", "", name).strip()
    name = LEGAL_SUFFIXES.sub("", name)
    name = re.sub(r"[,\.]+\s*$", "", name).strip()
    name = re.sub(r"\s{2,}", " ", name)
    # ALL-CAPS legal names read badly in a UI; title-case them, keep mixed case.
    if name.isupper():
        name = name.title()
    return name.strip()


def fetch_sic_companies(sic: int, max_pages: int = 4, page_size: int = 100):
    """Return company names registered under one SIC code."""
    names = []
    for page in range(max_pages):
        try:
            resp = session().get(
                SEC_BROWSE,
                params={
                    "action": "getcompany",
                    "SIC": sic,
                    "type": "10-K",
                    "owner": "include",
                    "count": page_size,
                    "start": page * page_size,
                },
                headers={"User-Agent": SEC_UA},
                timeout=30,
            )
            if resp.status_code != 200:
                break
            rows = _ROW_PATTERN.findall(resp.text)
        except Exception as exc:
            log.warning("discovery: SIC %s page %s failed: %s", sic, page, exc)
            break

        if not rows:
            break
        names.extend(clean_company_name(raw) for _, raw in rows)
        if len(rows) < page_size:
            break
        time.sleep(0.2)  # SEC asks for <10 req/s; stay well under

    return [n for n in names if n]


def discover(sectors=None, max_pages: int = 4):
    """Discover companies across the target sectors.

    Returns {company_name: sector_key}. Names are candidates pending resolution.
    """
    sectors = sectors or list(SIC_SECTORS)
    found = {}

    for sector in sectors:
        for sic, label in SIC_SECTORS.get(sector, {}).items():
            names = fetch_sic_companies(sic, max_pages=max_pages)
            for name in names:
                found.setdefault(name, sector)
            log.info("discovery: SIC %s (%s) -> %s companies", sic, label, len(names))

    log.info("discovery: %s unique companies total", len(found))
    return found
