"""Filter coefficients and design parameters.

`Biquad` uses the device convention: y[n] = b0·x[n] + b1·x[n-1] + b2·x[n-2] + a1·y[n-1] + a2·y[n-2],
i.e. a1/a2 are negated relative to the usual RBJ denominator.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Biquad(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    b0: float = 1.0
    b1: float = 0.0
    b2: float = 0.0
    a1: float = 0.0
    a2: float = 0.0

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        return (self.b0, self.b1, self.b2, self.a1, self.a2)

    @property
    def is_passthrough(self) -> bool:
        return self.as_tuple() == (1.0, 0.0, 0.0, 0.0, 0.0)

    @property
    def is_stable(self) -> bool:
        """Poles of ``1 - a1·z⁻¹ - a2·z⁻²`` strictly inside the unit circle (and every coefficient finite)."""
        if not all(math.isfinite(c) for c in self.as_tuple()):
            return False
        return abs(self.a2) < 1.0 and abs(self.a1) < 1.0 - self.a2


PASSTHROUGH = Biquad()

FilterType = Literal[
    "peaking",
    "lowshelf",
    "highshelf",
    "lp1",
    "hp1",
    "lp2",
    "hp2",
    "bandpass",
    "notch",
    "ap1",
    "ap2",
    "lt",
    "gain",
    "passthrough",
    "raw",
]

# Parameters each design type needs (beyond `type`).
_REQUIRED: dict[str, tuple[str, ...]] = {
    "peaking": ("freq", "q", "gain_db"),
    "lowshelf": ("freq", "q", "gain_db"),
    "highshelf": ("freq", "q", "gain_db"),
    "lp1": ("freq",),
    "hp1": ("freq",),
    "lp2": ("freq", "q"),
    "hp2": ("freq", "q"),
    "bandpass": ("freq", "q"),
    "notch": ("freq", "q"),
    "ap1": ("freq",),
    "ap2": ("freq", "q"),
    "lt": ("lt",),
    "gain": ("gain_db",),
    "passthrough": (),
    "raw": ("raw",),
}


class LinkwitzTransformParams(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    f0: float = Field(gt=0)
    q0: float = Field(gt=0)
    fp: float = Field(gt=0)
    qp: float = Field(gt=0)


class FilterDesign(BaseModel):
    """Design intent for one biquad slot (PEQ band)."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    type: FilterType = "passthrough"
    freq: float | None = Field(default=None, gt=0)
    q: float | None = Field(default=None, gt=0)
    gain_db: float | None = None
    lt: LinkwitzTransformParams | None = None
    raw: Biquad | None = None

    @model_validator(mode="after")
    def _check_required(self) -> "FilterDesign":
        missing = [name for name in _REQUIRED[self.type] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"filter type {self.type!r} requires {', '.join(missing)}")
        return self


class CrossoverDesign(BaseModel):
    """Design intent for one crossover group (up to 4 cascaded biquads)."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    kind: Literal["lp", "hp", "bp"]
    family: Literal["bw", "lr", "bessel"]
    order: int = Field(ge=1, le=8)
    freq: float = Field(gt=0)
    freq_hi: float | None = Field(default=None, gt=0)
    bessel_norm: Literal["phase", "mag"] = "phase"  # Bessel only: phase-matched or -3 dB at freq

    @model_validator(mode="after")
    def _check(self) -> "CrossoverDesign":
        if self.family == "lr" and self.order % 2:
            raise ValueError("Linkwitz-Riley crossovers need an even order")
        if self.family == "bessel" and self.order < 2:
            raise ValueError("Bessel crossovers need order 2..8")
        if self.kind == "bp":
            if self.freq_hi is None:
                raise ValueError("band-pass needs freq_hi")
            if self.freq_hi <= self.freq:
                raise ValueError("freq_hi must be above freq")
        return self
