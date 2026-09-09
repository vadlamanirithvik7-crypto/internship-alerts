"""Explicitly synthetic evaluation fixture, not evidence of real-world accuracy."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo import JOBS, PROFILES

# Fixed illustrative judgments, established independently of ranking output.
grades = {
    1: {1: 3, 2: 3, 3: 3, 4: 2, 9: 3, 13: 2, 14: 2, 18: 2, 8: 1, 6: 1},
    2: {5: 3, 6: 3, 11: 3, 17: 3, 8: 1, 7: 1, 14: 1},
    3: {7: 3, 8: 3, 10: 3, 5: 1, 6: 1, 17: 1},
}
data = {
    "description": "Synthetic demo fixture with author-supplied judgments; not a held-out real-job benchmark.",
    "profiles": [
        {"id": i, "name": n, "preferences": p, "resume_text": r}
        for i, (n, p, r) in enumerate(PROFILES, 1)
    ],
    "jobs": [
        {
            "id": i,
            "company_name": j[0],
            "title": j[1],
            "location": j[2],
            "source": j[4],
            "description": j[5],
            "term": "Summer 2027",
            "remote": "Remote" in j[2],
        }
        for i, j in enumerate(JOBS, 1)
    ],
    "judgments": [
        {
            "profile_id": p,
            "job_id": j,
            "relevance": grades[p].get(j, 0),
            "split": "test",
        }
        for p in grades
        for j in range(1, len(JOBS) + 1)
    ],
}
Path("evaluation/demo_dataset.json").write_text(json.dumps(data, indent=2) + "\n")
