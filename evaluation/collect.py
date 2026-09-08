"""Collect public internship descriptions for local human evaluation (no alerts)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from poller.sources import ats
from poller.normalize import is_us_location
from shared.sectors import is_internship
from scripts.demo import PROFILES

jobs = []
for fetcher, slug, name in [
    (ats.fetch_greenhouse, "cloudflare", "Cloudflare"),
    (ats.fetch_lever, "palantir", "Palantir"),
    (ats.fetch_ashby, "ramp", "Ramp"),
    (ats.fetch_ashby, "etched", "Etched"),
]:
    for p in fetcher(slug, name):
        if is_internship(p["title"], "", p["term"]) and is_us_location(p["location"]):
            jobs.append(
                {
                    **{
                        k: p[k]
                        for k in [
                            "title",
                            "description",
                            "company_name",
                            "location",
                            "term",
                            "source",
                            "remote",
                            "url",
                        ]
                    },
                    "id": len(jobs) + 1,
                }
            )
profiles = [
    {"id": i, "name": n, "preferences": prefs, "resume_text": resume}
    for i, (n, prefs, resume) in enumerate(PROFILES, 1)
]
path = Path("evaluation/local")
path.mkdir(parents=True, exist_ok=True)
(path / "unlabeled.json").write_text(
    json.dumps(
        {
            "description": "Public real postings; relevance requires human labels.",
            "profiles": profiles,
            "jobs": jobs,
            "judgments": [],
        },
        indent=2,
    )
)
print(
    f"Collected {len(jobs)} real postings. Add human judgments in evaluation/local/unlabeled.json before evaluating."
)
