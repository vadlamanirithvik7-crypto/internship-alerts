"""Idempotent additive migration for existing SQLite and PostgreSQL databases."""

from sqlalchemy import bindparam, inspect, text

ADDITIONS = {
    "companies": {
        "workday_host": "VARCHAR(20)",
        "priority": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "postings": {
        "description": "TEXT",
        "last_seen_at": "TIMESTAMP",
        "closed_at": "TIMESTAMP",
        "missing_sweeps": "INTEGER NOT NULL DEFAULT 0",
        "status": "VARCHAR(30) NOT NULL DEFAULT 'new'",
        "notes": "TEXT NOT NULL DEFAULT ''",
        "soft_key": "VARCHAR(64)",
        "alert_eligible": "BOOLEAN NOT NULL DEFAULT TRUE",
        "target_eligible": "BOOLEAN",
        "search_roles": "TEXT",
        "search_version": "INTEGER",
        "applied_at": "TIMESTAMP",
        "status_updated_at": "TIMESTAMP",
    },
}


def migrate(conn):
    tables = set(inspect(conn).get_table_names())
    if "application_tasks" in tables and conn.scalar(text(
        "SELECT 1 FROM application_tasks WHERE state IN ('queued','running','submitting','needs_info','needs_review','needs_action') LIMIT 1"
    )):
        conn.execute(text(
            "UPDATE application_tasks SET state=CASE WHEN submission_started_at IS NULL THEN 'cancelled' ELSE 'uncertain' END, "
            "detail='Automatic applications removed. Check employer confirmation if submission had already started.' "
            "WHERE state IN ('queued','running','submitting','needs_info','needs_review','needs_action')"
        ))
    for table, additions in ADDITIONS.items():
        if table not in tables:
            continue
        columns = {c["name"] for c in inspect(conn).get_columns(table)}
        for column, ddl in additions.items():
            if column not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        if table == "postings":
            if conn.scalar(text("SELECT 1 FROM postings WHERE last_seen_at IS NULL LIMIT 1")):
                conn.execute(text("UPDATE postings SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL"))
            indexes = {index["name"] for index in inspect(conn).get_indexes("postings")}
            if "ix_postings_soft_key" not in indexes:
                conn.execute(text("CREATE INDEX ix_postings_soft_key ON postings (soft_key)"))
            from shared.eligibility import eligible, SEARCH_VERSION
            from shared.role_search import role_tags

            while True:
                rows = (
                    conn.execute(
                        text(
                            f"SELECT id,title,location,term,description,url,raw_hash FROM postings WHERE target_eligible IS NULL OR search_version IS NULL OR search_version < {SEARCH_VERSION} LIMIT 500"
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                from poller.normalize import canonical_url
                from shared.db import raw_hash
                for r in rows:
                    canonical = canonical_url(r["url"])
                    if "?" not in canonical:
                        continue
                    identity = raw_hash(canonical)
                    if identity != r["raw_hash"] and not conn.scalar(text("SELECT id FROM postings WHERE raw_hash=:hash"), {"hash": identity}):
                        conn.execute(text("UPDATE postings SET raw_hash=:hash WHERE id=:id"), {"hash": identity, "id": r["id"]})
                groups = {}
                for r in rows:
                    key = (eligible(r["title"], r["location"], r["term"], r["description"]), "|" + "|".join(role_tags(r["title"])) + "|")
                    groups.setdefault(key, []).append(r["id"])
                # Two set-based writes per batch, not one network round trip per
                # historical posting on a remotely hosted PostgreSQL database.
                for (value, roles), ids in groups.items():
                    if ids:
                        conn.execute(text(
                            f"UPDATE postings SET target_eligible=:value,search_roles=:roles,search_version={SEARCH_VERSION} WHERE id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)), {"value": value, "roles": roles, "ids": ids})
            if "ix_postings_target_eligible" not in indexes:
                conn.execute(text("CREATE INDEX ix_postings_target_eligible ON postings (target_eligible)"))
