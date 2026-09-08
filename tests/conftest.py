import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from shared.db import get_engine, init_db, get_session_factory


@pytest.fixture
def db():
    engine = init_db(get_engine("sqlite://"))
    with get_session_factory(engine)() as session:
        yield session
    engine.dispose()
