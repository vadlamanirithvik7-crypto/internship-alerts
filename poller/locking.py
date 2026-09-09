"""Prevent overlapping local/remote pollers, independent of CI concurrency."""

from contextlib import contextmanager
import hashlib
import tempfile
from pathlib import Path
from sqlalchemy import text


@contextmanager
def poller_lock(engine):
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            acquired = conn.scalar(text("SELECT pg_try_advisory_lock(71429032)"))
            if not acquired:
                raise RuntimeError("Another poller is already running")
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(71429032)"))
    else:
        import fcntl

        key = hashlib.sha256(str(engine.url).encode()).hexdigest()[:20]
        with (Path(tempfile.gettempdir()) / f"internship-poller-{key}.lock").open(
            "w"
        ) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
