from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.formats.cli_script import (
    DEFAULT_HEADER,
    ScriptOp,
    apply_ops,
    export_script,
    import_script,
    parse_script,
)
from app.model.config_doc import (
    ConfigDoc,
    PathError,
    default_config_doc,
    flatten,
    get_path,
    is_device_path,
    set_path,
    validate_doc,
)
from app.model.device_profile import M2X4HD
from app.model.filters import PASSTHROUGH, Biquad, FilterDesign

FIXTURE = Path(__file__).parents[2] / "fixtures" / "pi_notes_commands.txt"

# docs/04 §8.1: BW2 sections at fs = 96 kHz, Q = 1/sqrt(2), each applied twice (LR4).
LP80 = Biquad(
    b0=6.828594198269291e-06,
    b1=1.3657188396538582e-05,
    b2=6.828594198269291e-06,
    a1=1.992595228750302,
    a2=-0.992622543127095,
)
HP20 = Biquad(
    b0=0.9990748276239033,
    b1=-1.9981496552478066,
    b2=0.9990748276239033,
    a1=1.9981487993036977,
    a2=-0.9981505111919151,
)


@pytest.fixture
def base() -> ConfigDoc:
    return default_config_doc(M2X4HD)


def ops_of(text: str) -> list[tuple[str, object]]:
    parsed = parse_script(text)
    assert parsed.errors == [] and parsed.warnings == []
    return [(op.path, op.value) for op in parsed.ops]


def device_state(doc: ConfigDoc) -> dict:
    """Everything a script can express: device paths, minus FIR (taps can't be scripted)."""
    return {p: v for p, v in flatten(doc).items() if is_device_path(p) and ".fir." not in p}


# --- fixture ----------------------------------------------------------------------------------------------------


def test_fixture_parses_cleanly(base):
    doc, parsed = import_script(FIXTURE.read_text(), base)
    assert parsed.ok and parsed.warnings == []
    assert validate_doc(doc, M2X4HD) == []

    shakers, sub, out3, out4 = doc.outputs
    assert shakers.gain_db == 12.0
    assert M2X4HD.delay_samples(shakers.delay_ms) == 864 and shakers.delay_ms == 9.0
    assert not shakers.mute and not shakers.invert
    assert [g.bypass for g in shakers.crossover] == [False, False]
    assert shakers.crossover[0].compiled == [LP80, LP80, PASSTHROUGH, PASSTHROUGH]
    assert shakers.crossover[1].compiled == [HP20, HP20, PASSTHROUGH, PASSTHROUGH]
    assert all(g.design is None for g in shakers.crossover)

    assert sub.gain_db == 0.0 and sub.delay_ms == 0.0 and not sub.mute
    assert [g.bypass for g in sub.crossover] == [True, True]
    assert all(bq == PASSTHROUGH for g in sub.crossover for bq in g.compiled)

    assert out3.mute and out4.mute
    assert [cell.enabled for cell in doc.inputs[0].routing] == [True, True, True, True]
    assert all(band.bypass for ch in doc.inputs + doc.outputs for band in ch.peq)


def test_fixture_unbypasses_only_after_coefficients():
    ops = parse_script(FIXTURE.read_text()).ops
    order = [op.path for op in ops]
    for g in (0, 1):
        bypass_at = order.index(f"outputs.0.crossover.{g}.bypass")
        assert order.index(f"outputs.0.crossover.{g}.compiled.0") < bypass_at
        assert order.index(f"outputs.0.crossover.{g}.compiled.1") < bypass_at


def test_fixture_device_paths():
    paths = parse_script(FIXTURE.read_text()).device_paths
    assert "outputs.0.crossover.0.design" not in paths
    assert paths[:4] == [f"inputs.0.routing.{m}.enabled" for m in range(4)]
    assert "outputs.0.gain_db" in paths and "outputs.3.mute" in paths
    assert len(paths) == len(set(paths)) == 4 + 2 + 4 + 2 + 8 + 2 + 2


def test_fixture_response_matches_docs(base):
    """Combined LR4 LP 80 + LR4 HP 20 magnitude, device convention (a1/a2 negated), docs/04 §8.1 to ±0.1 dB."""
    doc, _ = import_script(FIXTURE.read_text(), base)
    freqs = np.array([10, 20, 40, 60, 80, 120, 200], dtype=float)
    z1 = np.exp(-2j * np.pi * freqs / 96_000)
    h = np.ones_like(z1)
    for group in doc.outputs[0].crossover:
        for bq in group.compiled:
            h *= (bq.b0 + bq.b1 * z1 + bq.b2 * z1**2) / (1 - bq.a1 * z1 - bq.a2 * z1**2)
    expected = [-24.6, -6.1, -1.1, -2.5, -6.1, -15.7, -32.1]
    assert 20 * np.log10(np.abs(h)) == pytest.approx(expected, abs=0.06)


def test_fixture_export_roundtrip(base):
    doc, _ = import_script(FIXTURE.read_text(), base)
    script = export_script(doc)
    again, parsed = import_script(script, default_config_doc(M2X4HD))
    assert parsed.ok and parsed.warnings == []
    assert device_state(again) == device_state(doc)
    assert "output 0 crossover 0 0 set -- 6.828594198269291e-06 1.3657188396538582e-05" in script
    assert "output 0 crossover 0 2 set -- 1 0 0 0 0" in script
    assert "output 1 crossover all all set -- 1 0 0 0 0" in script
    assert "output 1 crossover all all bypass on" in script
    assert "output 0 crossover all all bypass off" in script
    assert "output 0 gain -- 12" in script and "output 0 delay 9" in script


# --- grammar ----------------------------------------------------------------------------------------------------


def test_comments_blank_lines_and_prefix():
    text = (
        "\n# comment\n   \nminidsp output 0 gain 3\n"
        "  output 1 gain -- -4.5   # trailing comment\n/usr/bin/minidsp output 2 gain 1"
    )
    parsed = parse_script(text)
    assert parsed.ok and parsed.warnings == []
    assert [(op.path, op.value, op.line) for op in parsed.ops] == [
        ("outputs.0.gain_db", 3.0, 4),
        ("outputs.1.gain_db", -4.5, 5),
        ("outputs.2.gain_db", 1.0, 6),
    ]


def test_dashdash_is_optional():
    assert ops_of("output 0 gain -6") == ops_of("output 0 gain -- -6") == [("outputs.0.gain_db", -6.0)]
    with_dd = ops_of("output 0 peq 1 set -- 1 -0.5 0.25 0.1 -0.2")
    assert with_dd == ops_of("output 0 peq 1 set 1 -0.5 0.25 0.1 -0.2")


def test_bom_crlf_and_line_continuation():
    parsed = parse_script("﻿output 0 crossover 0 1 set -- \\\r\n 1 0 0 \\\r\n 0 0\r\noutput 0 mute on\r\n")
    assert parsed.ok
    assert [(op.path, op.line) for op in parsed.ops] == [
        ("outputs.0.crossover.0.compiled.1", 1),
        ("outputs.0.crossover.0.design", 1),
        ("outputs.0.mute", 4),
    ]


def test_output_scalars():
    assert ops_of("output 3 mute on\noutput 3 mute false\noutput 2 invert true\noutput 1 delay 0") == [
        ("outputs.3.mute", True),
        ("outputs.3.mute", False),
        ("outputs.2.invert", True),
        ("outputs.1.delay_ms", 0.0),
    ]


def test_delay_rounded_to_whole_samples():
    ((path, value),) = ops_of("output 0 delay 10.41")
    assert path == "outputs.0.delay_ms"
    assert M2X4HD.delay_samples(value) == 999 and value == 999 / 96
    ((_, value),) = ops_of("output 0 delay 1.5")
    assert value == 1.5


def test_peq_set_single_band_and_design():
    bq = Biquad(b0=1.1, b1=-1.9, b2=0.8, a1=1.9, a2=-0.9)
    assert ops_of("input 1 peq 7 set -- 1.1 -1.9 0.8 1.9 -0.9") == [
        ("inputs.1.peq.7.compiled", bq),
        ("inputs.1.peq.7.design", FilterDesign(type="raw", raw=bq)),
    ]
    assert ops_of("output 0 peq 0 set 1 0 0 0 0")[1] == ("outputs.0.peq.0.design", FilterDesign())


def test_peq_all_expansion():
    ops = ops_of("output 2 peq all bypass off")
    assert ops == [(f"outputs.2.peq.{k}.bypass", False) for k in range(10)]
    ops = ops_of("input 0 peq ALL set -- 1 0 0 0 0")
    assert [p for p, _ in ops][::2] == [f"inputs.0.peq.{k}.compiled" for k in range(10)]


def test_peq_clear_resets_and_unbypasses(base):
    doc = apply_ops(base, parse_script("output 0 peq 3 set -- 0.5 0 0 0 0\noutput 0 peq 3 clear").ops)
    band = doc.outputs[0].peq[3]
    assert band.compiled == PASSTHROUGH and band.design == FilterDesign() and band.bypass is False


def test_crossover_set_all_all():
    ops = ops_of("output 1 crossover all all set -- 1 0 0 0 0")
    compiled = [p for p, _ in ops if ".compiled." in p]
    assert compiled == [f"outputs.1.crossover.{g}.compiled.{k}" for g in (0, 1) for k in range(4)]
    designs = [p for p, v in ops if p.endswith(".design")]
    assert designs == ["outputs.1.crossover.0.design", "outputs.1.crossover.1.design"]


def test_crossover_bypass_is_per_group():
    assert ops_of("output 0 crossover 1 3 bypass on") == [("outputs.0.crossover.1.bypass", True)]
    assert ops_of("output 0 crossover all 0 bypass off") == [
        ("outputs.0.crossover.0.bypass", False),
        ("outputs.0.crossover.1.bypass", False),
    ]


def test_crossover_clear_whole_group_keeps_bypass(base):
    script = (
        "output 0 crossover 0 all set -- 0.5 0 0 0 0\noutput 0 crossover 0 0 bypass off\noutput 0 crossover 0 2 clear"
    )
    doc = apply_ops(base, parse_script(script).ops)
    group = doc.outputs[0].crossover[0]
    assert group.compiled == [PASSTHROUGH] * 4 and group.bypass is False and group.design is None


def test_compressor_options():
    assert ops_of("output 0 compressor -b off -t -20 -k 4 -a 15 -r 30") == [
        ("outputs.0.compressor.bypass", False),
        ("outputs.0.compressor.threshold_db", -20.0),
        ("outputs.0.compressor.ratio", 4.0),
        ("outputs.0.compressor.attack_ms", 15.0),
        ("outputs.0.compressor.release_ms", 30.0),
    ]
    assert ops_of("output 1 compressor --threshold=-10 --bypass on -k2 -a=5") == [
        ("outputs.1.compressor.threshold_db", -10.0),
        ("outputs.1.compressor.bypass", True),
        ("outputs.1.compressor.ratio", 2.0),
        ("outputs.1.compressor.attack_ms", 5.0),
    ]


def test_input_commands():
    assert ops_of(
        "input 0 gain -- -3\ninput 1 mute on\ninput 0 routing 2 enable off\ninput 1 routing 3 gain -- -6"
    ) == [
        ("inputs.0.gain_db", -3.0),
        ("inputs.1.mute", True),
        ("inputs.0.routing.2.enabled", False),
        ("inputs.1.routing.3.gain_db", -6.0),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "minidsp gain -- -20",
        "mute on",
        "minidsp source toslink",
        "config 1",
        "dirac off",
        "status",
        "probe",
        "debug dump-float 1c 1d",
        "output 0 fir bypass off",
        "output 0 fir set -- 1 0",
        "output 0 peq all import filters.txt",
        "output 0 crossover 0 all import xo.txt",
        "output 0 compressor",
    ],
)
def test_ignored_lines_warn(line):
    parsed = parse_script(f"output 0 gain 1\n{line}\noutput 0 gain 2")
    assert parsed.ok and [op.value for op in parsed.ops] == [1.0, 2.0]
    assert [(w.line, w.text) for w in parsed.warnings] == [(2, line)]
    assert parsed.warnings[0].message


def test_fir_bypass_imported_when_supported():
    profile = M2X4HD.model_copy(update={"fir_supported": True})
    parsed = parse_script("output 2 fir bypass off", profile)
    assert [(op.path, op.value) for op in parsed.ops] == [("outputs.2.fir.bypass", False)]


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("for i in 0 1; do minidsp output $i mute on; done", "shell"),
        ("minidsp output $OUT gain 3", "shell"),
        ("do", "shell"),
        ("output 0 gain 3 && output 1 gain 3", "shell"),
        ("output 0 gain 'unterminated", "split"),
        ("output 4 gain 3", "out of range"),
        ("output -1 gain 3", "not an index"),
        ("output all gain 3", "not an index"),
        ("output 0 peq 10 bypass on", "out of range"),
        ("output 0 crossover 2 0 bypass on", "out of range"),
        ("output 0 crossover 0 4 set -- 1 0 0 0 0", "out of range"),
        ("input 2 gain 0", "out of range"),
        ("input 0 routing 4 enable on", "out of range"),
        ("output 0 peq 0 set -- 1 0 0 0", "5 coefficients"),
        ("output 0 peq 0 set -- 1 0 0 0 0 0", "5 coefficients"),
        ("output 0 peq 0 set -- 1 0 0 nan 0", "finite"),
        ("output 0 gain inf", "finite"),
        ("output 0 gain 1e999", "out of range"),
        ("output 0 gain 1_0", "finite"),
        ("output 0 gain", "missing gain"),
        ("output 0 gain 3 4", "extra"),
        ("output 0 mute toggle", "on or off"),
        ("output 0 delay -- -1", "negative"),
        ("output 0 volume 3", "unknown output command"),
        ("output 0 peq 0 frobnicate", "unknown peq action"),
        ("output 0 crossover 0 0 frobnicate", "unknown crossover action"),
        ("input 0 routing 0 mute on", "unknown routing action"),
        ("input 0 delay 3", "unknown input command"),
        ("output 0 compressor -x 3", "unknown compressor option"),
        ("output 0 compressor -t -3 -t -4", "twice"),
        ("output 0 compressor -t", "missing value"),
        ("minidsp", "missing command"),
        ("--", "missing command"),
        ("frobnicate 3", "unknown command"),
        ("-d 0 output 0 gain 3", "unknown command"),
    ],
)
def test_errors(line, fragment):
    parsed = parse_script(f"output 0 gain 1\n{line}")
    assert not parsed.ok
    assert [(e.line, e.text) for e in parsed.errors] == [(2, line)]
    assert fragment in parsed.errors[0].message
    assert [(op.path, op.value) for op in parsed.ops] == [("outputs.0.gain_db", 1.0)]


def test_apply_ops_rejects_bad_paths(base):
    for path in ("", "outputs.9.gain_db", "outputs.0.nope", "outputs.x.gain_db", "outputs.0.gain_db.x"):
        with pytest.raises(PathError):
            apply_ops(base, [ScriptOp(path, 1.0, 1)])
    assert base.outputs[0].gain_db == 0.0


# --- exporter ---------------------------------------------------------------------------------------------------


def test_export_defaults_shape(base):
    script = export_script(base)
    lines = script.splitlines()
    assert lines[0] == f"# {DEFAULT_HEADER.splitlines()[0]}"
    commands = [line for line in lines if line and not line.startswith("#")]
    assert commands[:4] == [f"output {n} mute on" for n in range(4)]
    assert commands[-4:] == [f"output {n} mute off" for n in range(4)]
    assert not any(line.startswith("minidsp") for line in commands)
    assert "input 0 peq all set -- 1 0 0 0 0" in commands
    assert "output 3 peq all bypass on" in commands
    assert "output 2 compressor -b on -t 0 -k 1 -a 10 -r 100" in commands
    assert not any("fir" in line for line in commands)
    assert script.endswith("\n")


def test_export_no_header_no_mute_wrap(base):
    doc = set_path(base, "outputs.2.mute", True)
    lines = export_script(doc, header=None, mute_wrap=False).splitlines()
    assert lines[0] == "# Input 1"
    assert "output 2 mute on" in lines and "output 1 mute off" in lines
    assert lines.index("output 2 gain -- 0") < lines.index("output 2 mute on")


def test_export_mute_wrap_keeps_muted_outputs_muted(base):
    doc = set_path(set_path(base, "outputs.2.mute", True), "outputs.3.mute", True)
    lines = export_script(doc).splitlines()
    assert lines[-3:] == ["# Final mute states", "output 0 mute off", "output 1 mute off"]
    final, _ = import_script(export_script(doc), base)
    assert [ch.mute for ch in final.outputs] == [False, False, True, True]


def test_export_negative_values_use_separators(base):
    doc = set_path(base, "outputs.0.gain_db", -6.5)
    doc = set_path(doc, "inputs.0.routing.1.gain_db", -3.0)
    doc = set_path(doc, "outputs.0.compressor.ratio", -1.0)
    doc = set_path(doc, "outputs.0.compressor.release_ms", 12.5)
    script = export_script(doc)
    assert "output 0 gain -- -6.5" in script
    assert "input 0 routing 1 gain -- -3" in script
    assert "output 0 compressor -b on -t 0 --ratio=-1 -a 10 -r 12.5" in script
    for line in script.splitlines():
        words = line.split()
        if line.startswith("#") or "compressor" in words:
            continue
        first_negative = next((i for i, w in enumerate(words) if w.startswith("-") and w != "--"), None)
        if first_negative is not None:
            assert "--" in words[:first_negative], line


def test_export_mixed_banks(base):
    doc = set_path(base, "outputs.0.peq.4.compiled", Biquad(b0=2.0))
    doc = set_path(doc, "outputs.0.peq.4.bypass", False)
    doc = set_path(doc, "outputs.1.crossover.1.compiled.3", Biquad(b0=0.5))
    doc = set_path(doc, "outputs.1.crossover.1.bypass", False)
    script = export_script(doc)
    assert "output 0 peq 4 set -- 2 0 0 0 0" in script and "output 0 peq 4 bypass off" in script
    assert "output 0 peq 3 bypass on" in script and "output 0 peq all" not in script
    assert "output 1 crossover 0 all set -- 1 0 0 0 0" in script
    assert "output 1 crossover 1 3 set -- 0.5 0 0 0 0" in script
    assert "output 1 crossover 0 all bypass on" in script and "output 1 crossover 1 all bypass off" in script


def test_export_skips_external_bands(base):
    doc = set_path(base, "outputs.1.peq.2.external", True)
    doc = set_path(doc, "outputs.1.peq.2.compiled", Biquad(b0=3.0))
    script = export_script(doc)
    assert "# output 1 peq 2: managed by another tool, not exported" in script
    assert "output 1 peq 2 " not in script
    assert "output 1 peq 0 set -- 1 0 0 0 0" in script and "output 1 peq 9 bypass on" in script


def test_export_delay_rounded(base):
    doc = set_path(base, "outputs.0.delay_ms", 10.41)
    assert "output 0 delay 10.40625" in export_script(doc)


def test_export_fir_only_as_bypass(base):
    profile = M2X4HD.model_copy(update={"fir_supported": True})
    doc = set_path(base, "outputs.0.fir.bypass", False)
    script = export_script(doc, profile)
    assert "# output 0 FIR is enabled in this preset, but its taps can't be exported as commands" in script
    assert "output 0 fir bypass on" in script and "fir bypass off" not in script
    parsed = parse_script(script, profile)
    assert parsed.ok and parsed.warnings == []


# --- property tests ---------------------------------------------------------------------------------------------

coeff = st.floats(min_value=-4.0, max_value=4.0, allow_nan=False, allow_infinity=False)
biquads = st.one_of(st.just(PASSTHROUGH), st.builds(Biquad, b0=coeff, b1=coeff, b2=coeff, a1=coeff, a2=coeff))
gains = st.floats(min_value=-100.0, max_value=24.0, allow_nan=False, allow_infinity=False)
anything = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


@st.composite
def config_docs(draw) -> ConfigDoc:
    data = default_config_doc(M2X4HD).model_dump()
    for ch in data["inputs"] + data["outputs"]:
        ch["gain_db"] = draw(gains)
        ch["mute"] = draw(st.booleans())
        uniform = draw(st.booleans())  # exercise the compressed "all" forms too
        shared = draw(biquads)
        for band in ch["peq"]:
            band["compiled"] = (shared if uniform else draw(biquads)).model_dump()
            band["bypass"] = draw(st.booleans())
    for ch in data["inputs"]:
        for cell in ch["routing"]:
            cell["enabled"] = draw(st.booleans())
            cell["gain_db"] = draw(gains)
    for ch in data["outputs"]:
        ch["invert"] = draw(st.booleans())
        ch["delay_ms"] = draw(st.integers(0, M2X4HD.delay_max_samples)) / 96
        uniform = draw(st.booleans())
        shared = draw(biquads)
        for group in ch["crossover"]:
            group["bypass"] = draw(st.booleans())
            group["compiled"] = [(shared if uniform else draw(biquads)).model_dump() for _ in group["compiled"]]
        ch["compressor"] = {
            "bypass": draw(st.booleans()),
            "threshold_db": draw(anything),
            "ratio": draw(anything),
            "attack_ms": draw(anything),
            "release_ms": draw(anything),
        }
    return ConfigDoc.model_validate(data)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(doc=config_docs(), other=config_docs(), mute_wrap=st.booleans())
def test_roundtrip_property(doc, other, mute_wrap):
    script = export_script(doc, mute_wrap=mute_wrap)
    parsed = parse_script(script)
    assert parsed.ok and parsed.warnings == []
    # The export is the full state: it reproduces the doc whatever the starting point.
    assert device_state(parsed.apply(other)) == device_state(doc)
    again = parsed.apply(default_config_doc(M2X4HD))
    assert export_script(again, mute_wrap=mute_wrap) == script

    # Safety: nothing is un-bypassed before its coefficients have been written.
    seen: set[str] = set()
    for op in parsed.ops:
        if op.path.endswith(".bypass") and op.value is False and ".compressor." not in op.path:
            owner = op.path.removesuffix(".bypass")
            n = M2X4HD.crossover_biquads if ".crossover." in owner else 1
            needed = {f"{owner}.compiled"} if n == 1 else {f"{owner}.compiled.{k}" for k in range(n)}
            assert needed <= seen, op.path
        seen.add(op.path)


TOKENS = [
    "minidsp", "output", "input", "0", "1", "3", "4", "9", "10", "all", "peq", "crossover", "set", "bypass",
    "clear", "import", "on", "off", "true", "toggle", "--", "gain", "-3.5", "1e-5", "nan", "mute", "delay", "invert",
    "routing", "enable", "compressor", "-t", "-k", "-b", "--ratio=2", "fir", "source", "#", "'", '"', "\\", "$x",
    "for", ";", "1", "0", "0", "0", "-0.5",
]  # fmt: skip


@settings(max_examples=300, deadline=None)
@given(lines=st.lists(st.lists(st.sampled_from(TOKENS), max_size=12).map(" ".join), max_size=12))
def test_fuzz_token_soup(lines):
    text = "\n".join(lines)
    parsed = parse_script(text)
    n_lines = max(len(text.splitlines()), 1)
    for issue in parsed.errors + parsed.warnings:
        assert issue.message and 1 <= issue.line <= n_lines
    doc = parsed.apply(default_config_doc(M2X4HD))  # every op path resolves and every value validates
    assert len(doc.outputs) == 4
    for op in parsed.ops:
        assert get_path(doc, op.path) is not None or op.path.endswith(".design")


@settings(max_examples=300, deadline=None)
@given(text=st.text(max_size=300))
def test_fuzz_arbitrary_text(text):
    parsed = parse_script(text)
    parsed.apply(default_config_doc(M2X4HD))
    assert all(issue.message for issue in parsed.errors + parsed.warnings)
