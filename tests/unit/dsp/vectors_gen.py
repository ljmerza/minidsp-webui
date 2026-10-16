"""Builds tests/vectors/*.json, the shared design/response vectors (docs 04 §7-8).

Each file is a list of ``{name, kind, params, coeffs, freqs, expected_db}`` entries, plus optional extras:
``expected_db_float32`` (response with coefficients rounded to float32, as the device stores them),
``expected_phase_deg``, ``expected_group_delay_ms`` and ``doc_db`` (the rounded values published in docs 04 §8.1).
``expected_db`` is always the float64 evaluation of ``coeffs`` at fs = 96 000 Hz. ``coeffs`` rows are
``[b0, b1, b2, a1, a2]`` in the device convention (a1/a2 negated vs RBJ).

``kind`` is a `FilterDesign` type (params are its fields), ``crossover`` (a `CrossoverDesign`, plus ``bessel_norm`` for
Bessel), ``chain`` (``params.crossovers`` cascaded), ``raw`` (coefficients with no design) or ``delay``.

The golden coefficients are literals from the live device, never generated. Regenerate the rest with
``uv run python -m tests.unit.dsp.vectors_gen``; test_vectors.py fails if the files drift from this script.
"""

import json
import math
from pathlib import Path
from typing import Any

from app.dsp import design as d
from app.dsp import response as r
from app.model.filters import Biquad, CrossoverDesign

VECTORS_DIR = Path(__file__).resolve().parents[2] / "vectors"

GOLDEN_LP_80 = [
    6.828594198269291e-06,
    1.3657188396538582e-05,
    6.828594198269291e-06,
    1.992595228750302,
    -0.992622543127095,
]
GOLDEN_HP_20 = [
    0.9990748276239033,
    -1.9981496552478066,
    0.9990748276239033,
    1.9981487993036977,
    -0.9981505111919151,
]
GOLDEN_FREQS = [10.0, 20.0, 40.0, 60.0, 80.0, 120.0, 200.0]
GOLDEN_DOC_DB = [-24.6, -6.1, -1.1, -2.5, -6.1, -15.7, -32.1]

# minidsp-rs minidsp/test_fixtures/rew-filters.txt (REW "miniDSP" biquad export, fs = 96 kHz).
REW_FILTERS = [
    [0.999194903557377, -1.9973354686174658, 0.9981886333563846, 1.9973354686174658, -0.9973835369137615],
    [0.9998887753067031, -1.9993545414551797, 0.9995415181447863, 1.9993545414551797, -0.9994302934514893],
    [0.9997438276048239, -1.9963057492683718, 0.9968144282171145, 1.9963057492683718, -0.9965582558219387],
    [0.9990789086685623, -1.9946732189917196, 0.9959293034301698, 1.9946732189917196, -0.9950082120987321],
    [0.9983670182030222, -1.9927237434872702, 0.9949285993345175, 1.9927237434872702, -0.9932956175375398],
    [0.9979598523643881, -1.9892560043464678, 0.9925763345585341, 1.9892560043464678, -0.9905361869229221],
    [0.99551681897023, -1.9735421424610884, 0.9839960695531388, 1.9735421424610884, -0.9795128885233688],
    [0.9699324996813751, -1.8219493560705984, 0.9001773692776996, 1.8219493560705984, -0.8701098689590749],
    [0.8763751198353854, -1.384250602079273, 0.6543255517808116, 1.384250602079273, -0.530700671616197],
    [0.8784231224481471, -1.2978927199484762, 0.7320089079645271, 1.2978927199484762, -0.6104320304126742],
]

BASE_FREQS = [5.0, 10.0, 20.0, 31.5, 50.0, 80.0, 100.0, 160.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0]
BASE_FREQS += [24000.0]


def _freqs(*extra: float) -> list[float]:
    return sorted(set(BASE_FREQS) | {float(f) for f in extra})


def _rows(biquads: list[Biquad]) -> list[list[float]]:
    return [list(b.as_tuple()) for b in biquads]


def _db(coeffs: list[list[float]], freqs: list[float], *, float32: bool = False) -> list[float]:
    resp = r.filter_response([tuple(c) for c in coeffs], freqs, float32=float32)
    return [round(float(x), 6) for x in resp.magnitude_db]


def _phase(coeffs: list[list[float]], freqs: list[float]) -> list[float]:
    resp = r.filter_response([tuple(c) for c in coeffs], freqs, float32=False)
    return [round(float(x), 6) for x in resp.phase_deg]


def _entry(name: str, kind: str, params: dict[str, Any], coeffs: list[list[float]], freqs: list[float]) -> dict:
    return {
        "name": name,
        "kind": kind,
        "params": params,
        "coeffs": coeffs,
        "freqs": freqs,
        "expected_db": _db(coeffs, freqs),
        "expected_db_float32": _db(coeffs, freqs, float32=True),
    }


def coeffs_for(kind: str, params: dict[str, Any]) -> list[list[float]] | None:
    """Re-design a vector from its params (None for kinds without a design)."""
    if kind == "crossover":
        p = dict(params)
        norm = p.pop("bessel_norm", None)
        return _rows(d.design_crossover(CrossoverDesign(**p), bessel_norm=norm, allow_unverified=True))
    if kind == "chain":
        return [row for x in params["crossovers"] for row in coeffs_for("crossover", x)]
    if kind in ("raw", "delay"):
        return None
    return _rows([d.design_filter({"type": kind, **params})])


def golden() -> list[dict]:
    lp = {"kind": "lp", "family": "lr", "order": 4, "freq": 80.0}
    hp = {"kind": "hp", "family": "lr", "order": 4, "freq": 20.0}
    q = 1 / math.sqrt(2)
    combined = _entry(
        "Shaker crossover: LR4 LP 80 Hz + LR4 HP 20 Hz (live device)",
        "chain",
        {"crossovers": [lp, hp]},
        [GOLDEN_LP_80, GOLDEN_LP_80, [1.0, 0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0, 0.0]]
        + [GOLDEN_HP_20, GOLDEN_HP_20, [1.0, 0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0, 0.0]],
        GOLDEN_FREQS,
    )
    combined["doc_db"] = GOLDEN_DOC_DB
    return [
        _entry("BW2 LP 80 Hz (live device)", "lp2", {"freq": 80.0, "q": q}, [GOLDEN_LP_80], _freqs(80)),
        _entry("BW2 HP 20 Hz (live device)", "hp2", {"freq": 20.0, "q": q}, [GOLDEN_HP_20], _freqs(20)),
        combined,
    ]


def filters() -> list[dict]:
    specs: list[tuple[str, str, dict[str, Any]]] = [
        ("PEQ +6 dB @ 1 kHz Q 1", "peaking", {"freq": 1000.0, "q": 1.0, "gain_db": 6.0}),
        ("PEQ -3.5 dB @ 42 Hz Q 4", "peaking", {"freq": 42.0, "q": 4.0, "gain_db": -3.5}),
        ("PEQ +12 dB @ 15 kHz Q 0.5", "peaking", {"freq": 15000.0, "q": 0.5, "gain_db": 12.0}),
        ("Low shelf +6 dB @ 100 Hz Q 0.707", "lowshelf", {"freq": 100.0, "q": 0.707, "gain_db": 6.0}),
        ("Low shelf -9 dB @ 35 Hz Q 1.2", "lowshelf", {"freq": 35.0, "q": 1.2, "gain_db": -9.0}),
        ("High shelf -4 dB @ 3 kHz Q 0.707", "highshelf", {"freq": 3000.0, "q": 0.707, "gain_db": -4.0}),
        ("High shelf +3 dB @ 8 kHz Q 0.5", "highshelf", {"freq": 8000.0, "q": 0.5, "gain_db": 3.0}),
        ("LP1 120 Hz", "lp1", {"freq": 120.0}),
        ("HP1 30 Hz", "hp1", {"freq": 30.0}),
        ("AP1 200 Hz", "ap1", {"freq": 200.0}),
        ("LP2 80 Hz Q 0.5", "lp2", {"freq": 80.0, "q": 0.5}),
        ("LP2 2 kHz Q 2", "lp2", {"freq": 2000.0, "q": 2.0}),
        ("HP2 25 Hz Q 0.9", "hp2", {"freq": 25.0, "q": 0.9}),
        ("Band-pass 500 Hz Q 2", "bandpass", {"freq": 500.0, "q": 2.0}),
        ("Notch 60 Hz Q 10", "notch", {"freq": 60.0, "q": 10.0}),
        ("All-pass 2nd 100 Hz Q 0.7", "ap2", {"freq": 100.0, "q": 0.7}),
        ("Linkwitz Transform 40 Hz Q 0.9 -> 20 Hz Q 0.6", "lt", {"lt": {"f0": 40.0, "q0": 0.9, "fp": 20.0, "qp": 0.6}}),
        ("Gain -6 dB", "gain", {"gain_db": -6.0}),
        ("Pass-through", "passthrough", {}),
    ]
    out = []
    for name, kind, params in specs:
        coeffs = coeffs_for(kind, params)
        extra = [params["freq"]] if "freq" in params else []
        if kind == "lt":
            extra = [params["lt"]["f0"], params["lt"]["fp"]]
        entry = _entry(name, kind, params, coeffs, _freqs(*extra))
        if kind in ("passthrough", "ap1", "ap2"):
            entry["expected_phase_deg"] = _phase(coeffs, entry["freqs"])
        out.append(entry)
    return out


def crossovers() -> list[dict]:
    out = []
    plans: list[tuple[str, list[int], list[str | None]]] = [
        ("bw", list(range(1, 9)), [None]),
        ("lr", [2, 4, 6, 8], [None]),
        ("bessel", list(range(2, 9)), ["phase", "mag"]),
    ]
    for family, orders, norms in plans:
        for order in orders:
            for norm in norms:
                for kind, freq in (("lp", 100.0), ("hp", 100.0)):
                    params: dict[str, Any] = {"kind": kind, "family": family, "order": order, "freq": freq}
                    if norm:
                        params["bessel_norm"] = norm
                    label = f"{family.upper()}{order} {kind.upper()} {freq:g} Hz" + (f" ({norm})" if norm else "")
                    out.append(_entry(label, "crossover", params, coeffs_for("crossover", params), _freqs(freq)))
    bp = {"kind": "bp", "family": "lr", "order": 4, "freq": 20.0, "freq_hi": 80.0}
    out.append(_entry("LR4 band-pass 20-80 Hz", "crossover", bp, coeffs_for("crossover", bp), _freqs(20, 80)))
    return out


def rew() -> list[dict]:
    return [
        _entry(
            f"REW fixture biquad{k + 1}", "raw", {"source": f"minidsp-rs rew-filters.txt biquad{k + 1}"}, [c], _freqs()
        )
        for k, c in enumerate(REW_FILTERS)
    ]


def delay() -> list[dict]:
    freqs = _freqs()
    resp = r.Response.from_tf(r.delay_tf(864, freqs), freqs)
    return [
        {
            "name": "Delay 9 ms",
            "kind": "delay",
            "params": {"delay_ms": 9.0, "samples": 864, "payload": {"secs": 0, "nanos": 9_000_000}},
            "coeffs": [],
            "freqs": freqs,
            "expected_db": [0.0] * len(freqs),
            "expected_group_delay_ms": [round(float(x), 9) for x in resp.group_delay_ms],
        }
    ]


def build() -> dict[str, list[dict]]:
    return {
        "golden.json": golden(),
        "filters.json": filters(),
        "crossovers.json": crossovers(),
        "rew.json": rew(),
        "delay.json": delay(),
    }


def dumps(entries: list[dict]) -> str:
    return json.dumps(entries, indent=1) + "\n"


def main() -> None:
    VECTORS_DIR.mkdir(parents=True, exist_ok=True)
    for name, entries in build().items():
        (VECTORS_DIR / name).write_text(dumps(entries))
        print(f"wrote {name}: {len(entries)} vectors")


if __name__ == "__main__":
    main()
