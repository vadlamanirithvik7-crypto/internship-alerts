"""Read-only pre-deployment dump, encrypted to the owner's offline public key."""
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, unquote
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import text
from shared.db import get_engine


def main():
    parts = urlsplit(os.environ["DATABASE_URL"])
    if "mckykzcaqstuhcwpdzkf" not in ((parts.username or "") + " " + (parts.hostname or "")):
        raise SystemExit("Unexpected database project; backup stopped.")
    engine = get_engine()
    with engine.connect() as conn:
        major = int(conn.scalar(text("SHOW server_version_num"))) // 10000
    if not 14 <= major <= 18:
        raise SystemExit("Database version needs review.")
    env = dict(os.environ, PGHOST=parts.hostname, PGPORT=str(parts.port or 5432),
               PGUSER=unquote(parts.username or ""), PGPASSWORD=unquote(parts.password or ""),
               PGDATABASE=parts.path.lstrip("/"), PGSSLMODE="require")
    cmd = ["docker", "run", "--rm"]
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"):
        cmd += ["-e", key]
    cmd += [f"postgres:{major}", "pg_dump", "--format=custom", "--no-owner", "--schema=public"]
    result = subprocess.run(cmd, env=env, capture_output=True)
    if result.returncode or not result.stdout.startswith(b"PGDMP"):
        raise SystemExit("Database backup failed. No database changes were made.")
    key = Fernet.generate_key()
    public = serialization.load_pem_public_key(Path("scripts/backup-public.pem").read_bytes())
    Path("before-applications.dump.encrypted").write_bytes(Fernet(key).encrypt(result.stdout))
    Path("backup-key.encrypted").write_bytes(public.encrypt(key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))
    print("Read-only encrypted backup verified; plaintext was never written to disk.")
    engine.dispose()


if __name__ == "__main__":
    main()
