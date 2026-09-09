"""Compare fixed retrieval methods against explicit graded relevance judgments.

Input: {profiles:[{id,name,resume_text,preferences}], jobs:[{id,title,description,
company_name,location,term,source,remote}], judgments:[{profile_id,job_id,relevance,
split}]}. Relevance: 0=irrelevant, 1=adjacent, 2=relevant, 3=strong. Missing labels
are excluded, never silently treated as negatives. --split selects dev or test.
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.db import init_db, get_engine, get_session_factory, utcnow
from shared.matching import rank


def metrics(order, labels, k=10):
    top = order[:k]
    precision = sum(labels[i] >= 2 for i in top) / max(1, len(top))
    dcg = sum((2 ** labels[i] - 1) / math.log2(pos + 2) for pos, i in enumerate(top))
    ideal = sum(
        (2**g - 1) / math.log2(pos + 2)
        for pos, g in enumerate(sorted(labels.values(), reverse=True)[:k])
    )
    return {
        "precision_at_10": round(precision, 4),
        "ndcg_at_10": round(dcg / ideal, 4) if ideal else 0,
    }


def evaluate(data, split="test"):
    engine = init_db(get_engine("sqlite://"))
    jobs = [
        SimpleNamespace(
            **{
                "location": "",
                "term": "",
                "source": "evaluation",
                "remote": False,
                "first_seen_at": utcnow(),
                **j,
            }
        )
        for j in data["jobs"]
    ]
    results = []
    with get_session_factory(engine)() as db:
        for raw in data["profiles"]:
            profile = SimpleNamespace(
                **{
                    "locations": "",
                    "term": "",
                    "exclusions": "",
                    "remote_only": False,
                    **raw,
                }
            )
            labels = {
                j["job_id"]: j["relevance"]
                for j in data["judgments"]
                if j["profile_id"] == profile.id and j["split"] == split
            }
            candidates = [j for j in jobs if j.id in labels]
            if not candidates:
                continue
            for method in ["keywords", "weighted", "hybrid"]:
                start = time.perf_counter()
                rows, scores, mode = rank(db, candidates, profile, method)
                elapsed = (time.perf_counter() - start) * 1000
                # Warm-cache latency is separate from model-load/embedding cost.
                start = time.perf_counter()
                rank(db, candidates, profile, method)
                warm = (time.perf_counter() - start) * 1000
                results.append(
                    {
                        "profile": profile.name,
                        "method": method,
                        "engine": mode,
                        "judged_jobs": len(candidates),
                        **metrics([p.id for p in rows], labels),
                        "first_run_ms": round(elapsed, 2),
                        "warm_ms": round(warm, 2),
                        "paid_api_calls": 0,
                    }
                )
    return {
        "dataset": data.get("description", "Unspecified"),
        "split": split,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.dataset.read_text()), args.split)
    out = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(out + "\n")
    print(out)
