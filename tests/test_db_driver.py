"""Catch runtime driver mismatches without needing a database connection."""

import pytest

from shared.db import get_engine


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2"])
def test_postgres_urls_load_installed_driver(monkeypatch, scheme):
    monkeypatch.setenv(
        "DATABASE_URL", f"{scheme}://user:p%40ss@localhost/radar_test?sslmode=require"
    )
    engine = get_engine()
    try:
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
        assert engine.url.password == "p@ss"
        assert engine.url.query["sslmode"] == "require"
    finally:
        engine.dispose()


def test_sqlite_still_connects():
    engine = get_engine("sqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        engine.dispose()
