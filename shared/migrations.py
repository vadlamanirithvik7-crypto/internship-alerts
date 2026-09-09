"""Idempotent additive migration for existing SQLite and PostgreSQL databases."""

from sqlalchemy import inspect, text

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
        "applied_at": "TIMESTAMP",
        "status_updated_at": "TIMESTAMP",
    },
}


def migrate(conn):
    tables = set(inspect(conn).get_table_names())
    for table, additions in ADDITIONS.items():
        if table not in tables:
            continue
        columns = {c["name"] for c in inspect(conn).get_columns(table)}
        for column, ddl in additions.items():
            if column not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        if table == "postings":
            conn.execute(
                text(
                    "UPDATE postings SET last_seen_at = first_seen_at WHERE last_seen_at IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_postings_soft_key ON postings (soft_key)"
                )
            )
            from shared.eligibility import eligible

            while True:
                rows = (
                    conn.execute(
                        text(
                            "SELECT id,title,location,term,description FROM postings WHERE target_eligible IS NULL LIMIT 500"
                        )
                    )
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                conn.execute(
                    text("UPDATE postings SET target_eligible=:value WHERE id=:id"),
                    [
                        {
                            "id": r["id"],
                            "value": eligible(
                                r["title"], r["location"], r["term"], r["description"]
                            ),
                        }
                        for r in rows
                    ],
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_postings_target_eligible ON postings (target_eligible)"
                )
            )
