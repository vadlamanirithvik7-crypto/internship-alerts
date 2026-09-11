"""Generalized ingestion of community job-tracker repos on GitHub.

The whole point of a tracker is that it lists a direct application URL for a job
*no matter which ATS the company uses* - so ingesting trackers is how we cover
Google, Meta, Apple and every other employer that isn't on one of the four ATS
APIs we poll directly. Rather than a scraper per company, we ingest as many of
these cross-company trackers as we can and let dedupe (on canonical URL) collapse
the overlap - whoever lists a job first is what triggers your alert.

Two shapes cover almost every tracker:
  * listings.json in the SimplifyJobs schema  -> simplify.postings_from_listings
  * a markdown table in the README            -> _parse_markdown_table (here)

discover_feeds() then keeps the set current on its own: it searches GitHub for
newly-active tracker repos and classifies each into one of those two shapes, so a
tracker that springs up mid-season gets picked up without a code change.
"""

import logging
import os
import re

from poller.net import get_json, get_text
from poller.normalize import make_posting
from poller.application_links import employer_url
from poller.sources import simplify

log = logging.getLogger(__name__)

# Curated markdown-table trackers (README, not listings.json). These are the
# high-signal ones we always ingest; discover_feeds() adds more at runtime.
MARKDOWN_TRACKERS = [
    ("jobright-swe", "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-Internship/master/README.md"),
    ("jobright-eng", "https://raw.githubusercontent.com/jobright-ai/2026-Engineer-Internship/master/README.md"),
    ("jobright-data", "https://raw.githubusercontent.com/jobright-ai/2026-Data-Analysis-Internship/master/README.md"),
]

# Continuation glyphs a row uses to mean "same company as the row above".
_CONTINUATION = {"↳", "⤷", "→", "»", "", "-", "—", "–"}

# [text](url)  and  <a href="url">text</a>
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_HTML_HREF = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean(cell: str) -> str:
    """Strip markdown/HTML decoration from a table cell, keeping its text."""
    text = _HTML_TAG.sub(" ", cell or "")
    text = text.replace("**", "").replace("`", "").strip()
    return re.sub(r"\s{2,}", " ", text)


def _cell_link(cell: str):
    """Return (text, url) for a cell, pulling the first markdown or HTML link."""
    md = _MD_LINK.search(cell or "")
    if md:
        return _clean(md.group(1)), md.group(2)
    href = _HTML_HREF.search(cell or "")
    return _clean(cell), (href.group(1) if href else "")


def _split_row(line: str):
    """Split a markdown table row into trimmed cells (drops the outer pipes)."""
    cells = line.split("|")
    # A leading/trailing "|" yields empty edge cells; drop exactly those.
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return cells


def _header_columns(cells):
    """Map a header row to {role: index} for the columns we care about."""
    cols = {}
    for i, cell in enumerate(cells):
        name = _clean(cell).lower()
        if "company" in name and "company" not in cols:
            cols["company"] = i
        elif any(k in name for k in ("role", "title", "position", "job")) and "title" not in cols:
            cols["title"] = i
        elif "location" in name and "location" not in cols:
            cols["location"] = i
        elif any(k in name for k in ("application", "apply", "link")) and "apply" not in cols:
            cols["apply"] = i
    return cols


def _parse_markdown_table(text: str, source: str):
    """Extract postings from the first company/role/location table in a README."""
    if not text:
        return []

    rows = [l for l in text.splitlines() if l.lstrip().startswith("|")]
    cols = None
    last_company = ""
    postings = []

    for line in rows:
        cells = _split_row(line)
        if cols is None:
            candidate = _header_columns(cells)
            if "company" in candidate and "title" in candidate:
                cols = candidate
            continue

        # Skip the |---|---| separator row.
        joined = _clean("".join(cells))
        if not joined or set(joined) <= set("-: "):
            continue
        if len(cells) <= max(cols.values()):
            continue

        company_cell = cells[cols["company"]]
        company_text, _ = _cell_link(company_cell)
        stripped = _clean(company_cell)
        if not company_text or stripped in _CONTINUATION:
            company = last_company  # continuation row -> inherit the company above
        else:
            company = company_text
            last_company = company
        if not company:
            continue

        title_text, title_url = _cell_link(cells[cols["title"]])
        links = []
        if "apply" in cols and cols["apply"] < len(cells):
            cell = cells[cols["apply"]]
            links.extend(url for _, url in _MD_LINK.findall(cell))
            links.extend(_HTML_HREF.findall(cell))
        links.append(title_url)
        apply_url = employer_url(links)
        if not title_text or not apply_url:
            continue

        location = _clean(cells[cols["location"]]) if "location" in cols and cols["location"] < len(cells) else ""
        postings.append(
            make_posting(
                company_name=company,
                title=title_text,
                url=apply_url,
                location=location,
                source=source,
            )
        )

    return postings


# --------------------------------------------------------------------------
# Auto-discovery of new tracker repos
# --------------------------------------------------------------------------
GITHUB_SEARCH = "https://api.github.com/search/repositories"
# Queries aimed at the naming conventions these trackers actually use.
DISCOVERY_QUERIES = [
    "internships in:name,description 2027",
    "hardware firmware internships in:name,description 2027",
    "internship list in:name,description",
]
# Cut the long tail of student portfolios/coursework named "*internship*".
MIN_STARS = 10
MAX_REPOS_PROBED = 30


def _github_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")  # lifts the 60/hr unauth rate limit
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _classify_repo(full_name: str, branch: str):
    """Return ('json'|'markdown', url) if the repo is an ingestible tracker."""
    base = f"https://raw.githubusercontent.com/{full_name}/{branch}"
    listings = f"{base}/.github/scripts/listings.json"
    data = get_json(listings, timeout=20)
    if isinstance(data, list) and data:
        return "json", listings

    readme = get_text(f"{base}/README.md", timeout=20)
    if readme:
        table_rows = [l for l in readme.splitlines() if l.lstrip().startswith("|")]
        for line in table_rows[:5]:
            header = _header_columns(_split_row(line))
            if "company" in header and ("title" in header or "location" in header):
                return "markdown", f"{base}/README.md"
    return None, None


def discover_feeds(max_repos=MAX_REPOS_PROBED):
    """Search GitHub for active tracker repos and classify them into feeds.

    Returns a list of (kind, source, url). Best-effort: a rate-limited or failing
    search just yields fewer feeds, never an exception.
    """
    curated = {url for _, url in MARKDOWN_TRACKERS} | set(simplify.FEEDS)
    found = {}
    seen_repos = set()
    probed = 0

    for query in DISCOVERY_QUERIES:
        result = get_json(
            GITHUB_SEARCH,
            params={"q": query, "sort": "updated", "order": "desc", "per_page": 20},
            headers=_github_headers(),
            timeout=25,
        )
        for repo in (result or {}).get("items") or []:
            full = repo.get("full_name")
            if not full or full in seen_repos:
                continue
            seen_repos.add(full)
            if (repo.get("stargazers_count") or 0) < MIN_STARS:
                continue
            if probed >= max_repos:
                break
            probed += 1
            kind, url = _classify_repo(full, repo.get("default_branch") or "main")
            if url and url not in curated and url not in found:
                found[url] = (kind, full.split("/")[0][:40], url)

    log.info("trackers: discovery probed %s repos -> %s new feeds", probed, len(found))
    return list(found.values())


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def fetch(discover=False, feeds=()):
    """Ingest all curated markdown trackers plus any auto-discovered feeds."""
    postings = []
    seen_ids = set()

    for source, url in MARKDOWN_TRACKERS:
        try:
            kept = _parse_markdown_table(get_text(url, timeout=45), "tracker-md")
            postings.extend(kept)
            log.info("trackers: %s -> %s postings", source, len(kept))
        except Exception as exc:
            log.warning("trackers: %s failed: %s", source, exc)

    feeds = list(feeds)
    if discover:
        try:
            feeds.extend(discover_feeds())
        except Exception as exc:
            log.warning("trackers: discovery failed: %s", exc)
            pass
    for kind, source, url in feeds:
        try:
            if kind == "json":
                data = get_json(url, timeout=45)
                kept = simplify.postings_from_listings(
                    data, source="tracker-json", seen_ids=seen_ids
                )
            else:
                kept = _parse_markdown_table(get_text(url, timeout=45), "tracker-md")
            postings.extend(kept)
            log.info("trackers: discovered %s (%s) -> %s postings", source, kind, len(kept))
        except Exception as exc:
            log.warning("trackers: discovered %s failed: %s", url, exc)

    return postings
