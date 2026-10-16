import itertools

import pytest

from app.state.db import Database
from app.state.repo import Repo


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "test.sqlite3"


@pytest.fixture
async def db(db_path):
    database = await Database.open(db_path)
    yield database
    await database.close()


@pytest.fixture
def clock():
    """Deterministic, strictly increasing timestamps."""
    counter = itertools.count()
    return lambda: f"2026-01-01T00:00:{next(counter):06.3f}+00:00"


@pytest.fixture
def repo(db, clock):
    return Repo(db, clock=clock)
