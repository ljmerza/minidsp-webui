import pytest
from pydantic import ValidationError

from app.config import Settings


def test_default_gain_range():
    s = Settings(_env_file=None)
    assert (s.gain_min_db, s.gain_max_db) == (-72.0, 12.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"gain_min_db": -100.25},  # reads back as +6.3 dB on a 2x4HD
        {"gain_min_db": -127.0},
        {"gain_max_db": 24.0},  # +24 dB and up read back as 0
        {"gain_max_db": 30.0},
        {"gain_min_db": 6.0, "gain_max_db": 6.0},
        {"gain_min_db": 10.0, "gain_max_db": 0.0},
    ],
)
def test_gain_range_is_bounded_by_the_hardware(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_gain_range_edges_are_allowed():
    s = Settings(_env_file=None, gain_min_db=-100.0, gain_max_db=23.9)
    assert (s.gain_min_db, s.gain_max_db) == (-100.0, 23.9)


def test_gain_range_from_env(monkeypatch):
    monkeypatch.setenv("GAIN_MIN_DB", "-127")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
