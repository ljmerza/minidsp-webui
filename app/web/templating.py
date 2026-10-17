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


def fmt_duration(seconds: float | None) -> str:
    """Coarse elapsed time for people: "45 s", "12 min", "3 h 12 min", "2 d 4 h"; an em dash when unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    total = int(seconds)
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


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


GRAPH_VIEWS = ("sub", "full")  # named frequency views, as in app/dsp/response.py and static/js/graph.js
DEFAULT_GRAPH_DB_RANGE = 24.0


def graph_defaults(request: Request) -> dict[str, Any]:
    """Settings' default graph range (docs 05 §9); base.html hands it to graph.js on ``<body>``.

    ``view`` is None when every page keeps its own default view.
    """
    settings = getattr(request.app.state, "settings", None)
    view = getattr(settings, "graph_view", "auto")
    db_range = getattr(settings, "graph_db_range", DEFAULT_GRAPH_DB_RANGE)
    return {"view": view if view in GRAPH_VIEWS else None, "db_range": db_range}


def _request_globals(request: Request) -> dict[str, Any]:
    return {
        "root": request.scope.get("root_path", "").rstrip("/"),
        "asset_version": __version__,
        "meter_ticks": METER_TICKS_DB,
        "graph_defaults": graph_defaults(request),
    }


templates = Jinja2Templates(directory=TEMPLATE_DIR, context_processors=[_request_globals])
templates.env.filters["num"] = fmt_num
templates.env.filters["db"] = fmt_db
templates.env.filters["duration"] = fmt_duration
templates.env.filters["meter_pct"] = lambda db: f"{meter_fraction(db) * 100:.2f}%"
