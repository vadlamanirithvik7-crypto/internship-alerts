"""Distinguish original employer applications from aggregator listings."""
from urllib.parse import urlsplit

AGGREGATORS = {"jobright.ai", "simplify.jobs", "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "adzuna.com", "arbeitnow.com", "remoteok.com"}


def is_aggregator(url):
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATORS)


def employer_url(urls):
    """Prefer a provided employer link; never guess a destination or requisition."""
    valid = []
    for url in urls:
        parsed = urlsplit(url or "")
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            continue
        valid.append(url)
    return next((url for url in valid if not is_aggregator(url)), valid[0] if valid else "")
