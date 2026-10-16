"""Jinja environment for the HTML pages: template directory, formatting filters and per-request globals."""

import math
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

MINUS = "−"  # typographic minus, so negative numbers line up with positive ones


def fmt_num(value: float | None, decimals: int = 1, *, signed: bool = False) -> str:
    """Number with a real minus sign; an em dash when unknown."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    text = f"{abs(value):.{decimals}f}"
    if value < 0 and float(text) != 0:
        return MINUS + text
    if signed and value > 0 and float(text) != 0:
        return "+" + text
    return text


def fmt_db(value: float | None, decimals: int = 1, *, signed: bool = False, unit: str = "dB") -> str:
    text = fmt_num(value, decimals, signed=signed)
    return text if text == "—" else f"{text} {unit}"


# Meter scale (mirrored in static/js/meters.js): -120..-40 dBFS fills the bottom quarter, -40..0 the rest.
METER_FLOOR_DB = -120.0
METER_KNEE_DB = -40.0
METER_KNEE_FRACTION = 0.25
METER_TICKS_DB = (0, -6, -12, -20, -30, -40, -80, -120)


def meter_fraction(db: float | None) -> float:
    if db is None or not math.isfinite(db):
        return 0.0
    db = min(0.0, max(METER_FLOOR_DB, db))
    if db >= METER_KNEE_DB:
        return METER_KNEE_FRACTION + (1 - METER_KNEE_FRACTION) * (db - METER_KNEE_DB) / -METER_KNEE_DB
    return METER_KNEE_FRACTION * (db - METER_FLOOR_DB) / (METER_KNEE_DB - METER_FLOOR_DB)


def _request_globals(request: Request) -> dict[str, Any]:
    return {
        "root": request.scope.get("root_path", "").rstrip("/"),
        "asset_version": __version__,
        "meter_ticks": METER_TICKS_DB,
    }


templates = Jinja2Templates(directory=TEMPLATE_DIR, context_processors=[_request_globals])
templates.env.filters["num"] = fmt_num
templates.env.filters["db"] = fmt_db
templates.env.filters["meter_pct"] = lambda db: f"{meter_fraction(db) * 100:.2f}%"
