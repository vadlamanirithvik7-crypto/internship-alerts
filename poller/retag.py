"""Re-apply sector tagging to stored postings.

Tags are computed at insert time, so any change to the taxonomy in shared/sectors.py
only affects new rows. Run this after editing keywords to bring existing rows in line.

    python3 poller/retag.py --dry-run   # preview changes
    python3 poller/retag.py             # apply
"""

import argparse
import os
import sys

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.db import (  # noqa: E402
    Posting,
    get_engine,
    get_session_factory,
    pack_list,
    unpack_list,
)
from shared.sectors import tag_posting  # noqa: E402


def retag(dry_run=False, show=12):
    Session = get_session_factory(get_engine())
    added_total, removed_total, changed = 0, 0, 0

    with Session() as session:
        postings = session.execute(select(Posting)).scalars().all()
        examples = []

        for posting in postings:
            old = set(unpack_list(posting.sector_tags))
            new = set(tag_posting(posting.title, "", posting.category_hint or ""))
            if old == new:
                continue

            changed += 1
            added_total += len(new - old)
            removed_total += len(old - new)
            if len(examples) < show:
                examples.append((posting.title, sorted(old), sorted(new)))
            if not dry_run:
                posting.sector_tags = pack_list(sorted(new))

        if not dry_run:
            session.commit()

        print(f"{len(postings)} postings scanned, {changed} would change" if dry_run
              else f"{len(postings)} postings scanned, {changed} updated")
        print(f"  tags added: {added_total}, tags removed: {removed_total}")
        if examples:
            print("\nexamples:")
            for title, old, new in examples:
                print(f"  {title[:58]}")
                print(f"      {old} -> {new}")

    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-apply sector tags to stored postings")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args()
    retag(dry_run=args.dry_run)
