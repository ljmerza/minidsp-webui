"""`minidsp -f` command scripts: parse them into ConfigDoc operations, and export a ConfigDoc as one.

Import accepts the subset of the minidsp-rs CLI grammar that maps onto a preset's ConfigDoc::

    [minidsp] output N gain|mute|delay|invert …
    [minidsp] output|input N peq K|all set|bypass|clear …
    [minidsp] output N crossover G|all K|all set|bypass|clear …
    [minidsp] output N compressor [-b on|off] [-t T] [-k R] [-a A] [-r R]
    [minidsp] input N gain|mute …
    [minidsp] input N routing M enable on|off | gain <dB>

Blank lines and ``#`` comments are skipped, ``--`` separators are optional, and a trailing ``\\`` joins the next
line. Master and meta commands (volume, source, preset, status, debug …) and file imports are ignored with a
warning. Malformed lines are reported as errors and produce no operations.

Semantics follow the CLI (minidsp-rs ``bin/minidsp/handlers.rs``): ``peq … clear`` resets the coefficients to
pass-through **and** un-bypasses the band, ``crossover … clear`` resets every biquad of the group(s) and leaves
the bypass alone, and crossover ``bypass`` acts on the whole group whatever biquad index is given.

The exporter writes the full state of a ConfigDoc (not a diff), without the ``minidsp`` prefix, so the file can be
run as-is with ``minidsp -f <file>``. Coefficients are always written before the bypass that enables them.
"""

import re
import shlex
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.model.config_doc import Compressor, ConfigDoc, CrossoverGroup, PathError, PeqBand, is_device_path
from app.model.device_profile import M2X4HD, DeviceProfile
from app.model.filters import PASSTHROUGH, Biquad, FilterDesign

# --- results ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptOp:
    """Set ``path`` (a ConfigDoc dotted path) to ``value``. ``line`` is the 1-based script line it came from."""

    path: str
    value: Any
    line: int


@dataclass(frozen=True)
class ScriptIssue:
    line: int
    text: str
    message: str


@dataclass
class ParsedScript:
    ops: list[ScriptOp] = field(default_factory=list)
    warnings: list[ScriptIssue] = field(default_factory=list)  # lines ignored on purpose (master, FIR, imports)
    errors: list[ScriptIssue] = field(default_factory=list)  # malformed lines; they produced no ops

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def device_paths(self) -> list[str]:
        """Unique device-facing paths the script sets, in order of first appearance (design paths excluded)."""
        return [p for p in dict.fromkeys(op.path for op in self.ops) if is_device_path(p)]

    def apply(self, doc: ConfigDoc) -> ConfigDoc:
        return apply_ops(doc, self.ops)


def apply_ops(doc: ConfigDoc, ops: Iterable[ScriptOp]) -> ConfigDoc:
    """Return a new validated document with every op applied in order. Raises `PathError` for a bad path."""
    data = doc.model_dump()
    for op in ops:
        parent, key = _resolve_parent(data, op.path)
        parent[key] = op.value.model_dump() if isinstance(op.value, BaseModel) else op.value
    return ConfigDoc.model_validate(data)


def _resolve_parent(data: dict, path: str) -> tuple[Any, Any]:
    parts = path.split(".") if path else []
    if not parts:
        raise PathError(path)
    node: Any = data
    for i, part in enumerate(parts):
        if isinstance(node, list):
            if not (part.isascii() and part.isdigit()) or int(part) >= len(node):
                raise PathError(path)
            key: Any = int(part)
        elif isinstance(node, dict) and part in node:
            key = part
        else:
            raise PathError(path)
        if i == len(parts) - 1:
            return node, key
        node = node[key]
    raise PathError(path)  # pragma: no cover - the loop always returns


# --- parser -----------------------------------------------------------------------------------------------------


class _LineError(Exception):
    """The line is malformed."""


class _Ignored(Exception):
    """The line is valid CLI but not part of a preset config."""


_MASTER_COMMANDS = {
    "gain": "master volume",
    "mute": "master mute",
    "source": "input source",
    "config": "preset selection",
    "dirac": "Dirac Live",
    "status": "status query",
    "probe": "device probe",
    "debug": "debug command",
    "server": "server command",
}
_SHELL_WORDS = frozenset({"for", "do", "done", "while", "until", "if", "then", "else", "elif", "fi", "case", "esac"})
_SHELL_CHARS = re.compile(r"[$`;|&<>(){}]")
# ASCII only, like Rust's number parsing (Python's \d and float() also accept other scripts' digits).
_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INDEX = re.compile(r"\+?[0-9]+")
_BOOLS = {"on": True, "true": True, "off": False, "false": False}
_COMPRESSOR_FLAGS = {
    "-b": "bypass",
    "--bypass": "bypass",
    "-t": "threshold_db",
    "--threshold": "threshold_db",
    "-k": "ratio",
    "--ratio": "ratio",
    "-a": "attack_ms",
    "--attack": "attack_ms",
    "-r": "release_ms",
    "--release": "release_ms",
}

_Ops = list[tuple[str, Any]]


class _Args:
    def __init__(self, words: Sequence[str]) -> None:
        self._words = list(words)
        self._pos = 0

    def __bool__(self) -> bool:
        return self._pos < len(self._words)

    def next(self, what: str) -> str:
        if not self:
            raise _LineError(f"missing {what}")
        self._pos += 1
        return self._words[self._pos - 1]

    def keyword(self, what: str) -> str:
        return self.next(what).lower()

    def rest(self) -> list[str]:
        rest, self._pos = self._words[self._pos :], len(self._words)
        return rest

    def end(self) -> None:
        if self:
            raise _LineError(f"unexpected extra arguments: {' '.join(self._words[self._pos :])}")


def _number(token: str, what: str) -> float:
    if not _NUMBER.fullmatch(token):
        raise _LineError(f"{what}: {token!r} is not a finite number")
    value = float(token)
    if value in (float("inf"), float("-inf")):
        raise _LineError(f"{what}: {token!r} is out of range")
    return value


def _bool(token: str, what: str) -> bool:
    try:
        return _BOOLS[token.lower()]
    except KeyError:
        raise _LineError(f"{what}: expected on or off, got {token!r}") from None


def _index(token: str, count: int, what: str) -> int:
    if not _INDEX.fullmatch(token):
        raise _LineError(f"{what}: {token!r} is not an index")
    idx = int(token)
    if idx >= count:
        raise _LineError(f"{what} {idx} out of range (0..{count - 1})")
    return idx


def _targets(token: str, count: int, what: str) -> list[int]:
    return list(range(count)) if token.lower() == "all" else [_index(token, count, what)]


def _biquad(tokens: list[str]) -> Biquad:
    if len(tokens) != 5:
        raise _LineError(f"set needs 5 coefficients (b0 b1 b2 a1 a2), got {len(tokens)}")
    return Biquad(**dict(zip(("b0", "b1", "b2", "a1", "a2"), (_number(t, "coefficient") for t in tokens), strict=True)))


def _peq_design(bq: Biquad) -> FilterDesign:
    return FilterDesign() if bq.is_passthrough else FilterDesign(type="raw", raw=bq)


def _parse_peq(args: _Args, base: str, count: int) -> _Ops:
    bands = _targets(args.next("peq index"), count, "peq")
    action = args.keyword("peq action (set, bypass or clear)")
    if action == "set":
        bq = _biquad(args.rest())
        return [op for k in bands for op in ((f"{base}.{k}.compiled", bq), (f"{base}.{k}.design", _peq_design(bq)))]
    if action == "bypass":
        value = _bool(args.next("bypass value"), "bypass")
        args.end()
        return [(f"{base}.{k}.bypass", value) for k in bands]
    if action == "clear":
        args.end()
        return [
            op
            for k in bands
            for op in (
                (f"{base}.{k}.compiled", PASSTHROUGH),
                (f"{base}.{k}.design", FilterDesign()),
                (f"{base}.{k}.bypass", False),
            )
        ]
    if action == "import":
        raise _Ignored("'peq … import' reads a file on the DSP host; use the REW import instead")
    raise _LineError(f"unknown peq action {action!r}")


def _parse_crossover(args: _Args, base: str, profile: DeviceProfile) -> _Ops:
    groups = _targets(args.next("crossover group"), profile.crossover_groups, "crossover group")
    biquads = _targets(args.next("crossover biquad index"), profile.crossover_biquads, "crossover biquad")
    action = args.keyword("crossover action (set, bypass or clear)")
    if action == "set":
        bq = _biquad(args.rest())
        ops: _Ops = []
        for g in groups:
            ops.extend((f"{base}.{g}.compiled.{k}", bq) for k in biquads)
            ops.append((f"{base}.{g}.design", None))
        return ops
    if action == "bypass":
        value = _bool(args.next("bypass value"), "bypass")
        args.end()
        return [(f"{base}.{g}.bypass", value) for g in groups]
    if action == "clear":
        args.end()
        ops = []
        for g in groups:
            ops.extend((f"{base}.{g}.compiled.{k}", PASSTHROUGH) for k in range(profile.crossover_biquads))
            ops.append((f"{base}.{g}.design", None))
        return ops
    if action == "import":
        raise _Ignored("'crossover … import' reads a file on the DSP host; use the REW import instead")
    raise _LineError(f"unknown crossover action {action!r}")


def _parse_compressor(args: _Args, base: str) -> _Ops:
    values: dict[str, Any] = {}
    while args:
        token = args.next("compressor option")
        if token.startswith("--") and "=" in token:
            flag, raw = token.split("=", 1)
        elif not token.startswith("--") and len(token) > 2 and token[:2] in _COMPRESSOR_FLAGS:
            flag, raw = token[:2], token[2:].removeprefix("=")
        else:
            flag, raw = token, None
        name = _COMPRESSOR_FLAGS.get(flag)
        if name is None:
            raise _LineError(f"unknown compressor option {token!r}")
        if name in values:
            raise _LineError(f"compressor option {flag} given twice")
        raw = args.next(f"value for {flag}") if raw is None else raw
        values[name] = _bool(raw, flag) if name == "bypass" else _number(raw, flag)
    if not values:
        raise _Ignored("compressor without options changes nothing")
    return [(f"{base}.compressor.{name}", value) for name, value in values.items()]


def _parse_output(args: _Args, profile: DeviceProfile) -> _Ops:
    n = _index(args.next("output index"), profile.n_outputs, "output")
    base = f"outputs.{n}"
    sub = args.keyword("output command")
    if sub == "crossover":
        return _parse_crossover(args, f"{base}.crossover", profile)
    if sub == "peq":
        return _parse_peq(args, f"{base}.peq", profile.peq_per_output)
    if sub == "compressor":
        return _parse_compressor(args, base)
    if sub == "fir":
        action = args.keyword("fir action")
        if action == "bypass" and profile.fir_supported:
            value = _bool(args.next("bypass value"), "bypass")
            args.end()
            return [(f"{base}.fir.bypass", value)]
        raise _Ignored("FIR lines aren't imported; load FIR taps from a WAV file on the output's FIR tab")
    if sub in ("gain", "delay"):
        value = _number(args.next(sub), sub)
        args.end()
        if sub == "gain":
            return [(f"{base}.gain_db", value)]
        if value < 0:
            raise _LineError("delay can't be negative")
        # Stored rounded to whole samples so the doc holds exactly what the device will.
        return [(f"{base}.delay_ms", profile.delay_ms_from_samples(profile.delay_samples(value)))]
    if sub in ("mute", "invert"):
        value = _bool(args.next(f"{sub} value"), sub)
        args.end()
        return [(f"{base}.{sub}", value)]
    raise _LineError(f"unknown output command {sub!r}")


def _parse_input(args: _Args, profile: DeviceProfile) -> _Ops:
    n = _index(args.next("input index"), profile.n_inputs, "input")
    base = f"inputs.{n}"
    sub = args.keyword("input command")
    if sub == "peq":
        return _parse_peq(args, f"{base}.peq", profile.peq_per_input)
    if sub == "gain":
        value = _number(args.next("gain"), "gain")
        args.end()
        return [(f"{base}.gain_db", value)]
    if sub == "mute":
        value = _bool(args.next("mute value"), "mute")
        args.end()
        return [(f"{base}.mute", value)]
    if sub == "routing":
        m = _index(args.next("routing output index"), profile.n_outputs, "routing output")
        action = args.keyword("routing action (enable or gain)")
        if action == "enable":
            enabled = _bool(args.next("enable value"), "enable")
            args.end()
            return [(f"{base}.routing.{m}.enabled", enabled)]
        if action == "gain":
            value = _number(args.next("gain"), "gain")
            args.end()
            return [(f"{base}.routing.{m}.gain_db", value)]
        raise _LineError(f"unknown routing action {action!r}")
    raise _LineError(f"unknown input command {sub!r}")


def _parse_line(line: str, profile: DeviceProfile) -> _Ops:
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError as exc:
        raise _LineError(f"can't split line: {exc}") from None
    if not tokens:
        return []
    if tokens[0] in _SHELL_WORDS or any(_SHELL_CHARS.search(t) for t in tokens):
        raise _LineError("shell constructs (loops, variables, pipes) aren't supported; expand them to literal commands")
    if tokens[0] == "minidsp" or tokens[0].endswith("/minidsp"):
        tokens = tokens[1:]
    words = [t for t in tokens if t != "--"]
    if not words:
        raise _LineError("missing command")
    args = _Args(words[1:])
    command = words[0].lower()
    if command == "output":
        return _parse_output(args, profile)
    if command == "input":
        return _parse_input(args, profile)
    if command in _MASTER_COMMANDS:
        raise _Ignored(f"{_MASTER_COMMANDS[command]} isn't part of a preset config; ignored")
    raise _LineError(f"unknown command {words[0]!r}")


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """(first line number, text) per command, joining lines that end in a backslash."""
    pending: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.removeprefix("﻿").splitlines(), start=1):
        if not pending:
            start = lineno
        stripped = raw.rstrip()
        if stripped.endswith("\\") and not stripped.lstrip().startswith("#"):
            pending.append(stripped[:-1])
            continue
        pending.append(raw)
        yield start, " ".join(pending).strip()
        pending = []
    if pending:
        yield start, " ".join(pending).strip()


def parse_script(text: str, profile: DeviceProfile = M2X4HD) -> ParsedScript:
    """Parse a command script into ConfigDoc ops. Never raises for bad input; see `errors` and `warnings`."""
    result = ParsedScript()
    for lineno, line in _logical_lines(text):
        if not line or line.startswith("#"):
            continue
        try:
            ops = _parse_line(line, profile)
        except _Ignored as exc:
            result.warnings.append(ScriptIssue(lineno, line, str(exc)))
        except _LineError as exc:
            result.errors.append(ScriptIssue(lineno, line, str(exc)))
        else:
            result.ops.extend(ScriptOp(path, value, lineno) for path, value in ops)
    return result


def import_script(text: str, base: ConfigDoc, profile: DeviceProfile = M2X4HD) -> tuple[ConfigDoc, ParsedScript]:
    """Parse ``text`` and apply its ops on top of ``base``. Lines with errors are skipped."""
    parsed = parse_script(text, profile)
    return parsed.apply(base), parsed


# --- exporter ---------------------------------------------------------------------------------------------------

DEFAULT_HEADER = (
    "minidsp-webui preset export.\n"
    "Run it on the DSP host with: minidsp -f <file>\n"
    "The commands write to the device's active preset. Master volume, source and preset aren't included."
)


def _num(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _onoff(value: bool) -> str:
    return "on" if value else "off"


def _coeffs(bq: Biquad) -> str:
    return " ".join(_num(c) for c in bq.as_tuple())


def _peq_lines(prefix: str, bands: list[PeqBand], expected: int) -> list[str]:
    external = [k for k, band in enumerate(bands) if band.external]
    managed = [(k, band) for k, band in enumerate(bands) if not band.external]
    lines = []
    if external:
        lines.append(f"# {prefix} {' '.join(map(str, external))}: managed by another tool, not exported")
    use_all = not external and len(bands) == expected
    if use_all and len({band.compiled.as_tuple() for band in bands}) == 1:
        lines.append(f"{prefix} all set -- {_coeffs(bands[0].compiled)}")
    else:
        lines.extend(f"{prefix} {k} set -- {_coeffs(band.compiled)}" for k, band in managed)
    if use_all and len({band.bypass for band in bands}) == 1:
        lines.append(f"{prefix} all bypass {_onoff(bands[0].bypass)}")
    else:
        lines.extend(f"{prefix} {k} bypass {_onoff(band.bypass)}" for k, band in managed)
    return lines


def _crossover_lines(prefix: str, groups: list[CrossoverGroup], profile: DeviceProfile) -> list[str]:
    lines = []
    full = len(groups) == profile.crossover_groups and all(len(g.compiled) == profile.crossover_biquads for g in groups)
    distinct = {bq.as_tuple() for g in groups for bq in g.compiled}
    if full and len(distinct) == 1:
        lines.append(f"{prefix} all all set -- {_coeffs(groups[0].compiled[0])}")
    else:
        for g, group in enumerate(groups):
            if len(group.compiled) == profile.crossover_biquads and len({bq.as_tuple() for bq in group.compiled}) == 1:
                lines.append(f"{prefix} {g} all set -- {_coeffs(group.compiled[0])}")
            else:
                lines.extend(f"{prefix} {g} {k} set -- {_coeffs(bq)}" for k, bq in enumerate(group.compiled))
    if full and len({g.bypass for g in groups}) == 1:
        lines.append(f"{prefix} all all bypass {_onoff(groups[0].bypass)}")
    else:
        lines.extend(f"{prefix} {g} all bypass {_onoff(group.bypass)}" for g, group in enumerate(groups))
    return lines


def _compressor_line(prefix: str, comp: Compressor) -> str:
    # clap lets only the threshold take a hyphen value after a space; "--long=value" works for any option.
    parts = [f"{prefix} compressor -b {_onoff(comp.bypass)} -t {_num(comp.threshold_db)}"]
    for short, long, value in (
        ("-k", "--ratio", comp.ratio),
        ("-a", "--attack", comp.attack_ms),
        ("-r", "--release", comp.release_ms),
    ):
        parts.append(f"{long}={_num(value)}" if value < 0 else f"{short} {_num(value)}")
    return " ".join(parts)


def export_script(
    doc: ConfigDoc,
    profile: DeviceProfile = M2X4HD,
    *,
    mute_wrap: bool = True,
    header: str | None = DEFAULT_HEADER,
) -> str:
    """The full ConfigDoc as a ``minidsp -f`` script.

    With ``mute_wrap`` every output is muted first and the unmuted ones are released last, so nothing half-written
    is ever audible. Delays are written rounded to whole samples. PEQ bands flagged ``external`` are skipped, and
    FIR taps can't be expressed as commands: a FIR is only exported as bypassed.
    """
    lines: list[str] = []
    if header:
        lines.extend(f"# {text}".rstrip() for text in header.splitlines())
    if mute_wrap:
        lines.append("# Mute every output while the preset is written; the final mute states come last.")
        lines.extend(f"output {n} mute on" for n in range(len(doc.outputs)))

    for i, ch in enumerate(doc.inputs):
        prefix = f"input {i}"
        lines.append(f"# Input {i + 1}")
        lines.append(f"{prefix} gain -- {_num(ch.gain_db)}")
        lines.append(f"{prefix} mute {_onoff(ch.mute)}")
        lines.extend(_peq_lines(f"{prefix} peq", ch.peq, profile.peq_per_input))
        for m, cell in enumerate(ch.routing):
            lines.append(f"{prefix} routing {m} enable {_onoff(cell.enabled)}")
            lines.append(f"{prefix} routing {m} gain -- {_num(cell.gain_db)}")

    for n, ch in enumerate(doc.outputs):
        prefix = f"output {n}"
        lines.append(f"# Output {n + 1}")
        lines.append(f"{prefix} gain -- {_num(ch.gain_db)}")
        if not mute_wrap:
            lines.append(f"{prefix} mute {_onoff(ch.mute)}")
        delay_ms = profile.delay_ms_from_samples(profile.delay_samples(ch.delay_ms))
        lines.append(f"{prefix} delay {_num(delay_ms)}")
        lines.append(f"{prefix} invert {_onoff(ch.invert)}")
        lines.extend(_peq_lines(f"{prefix} peq", ch.peq, profile.peq_per_output))
        lines.extend(_crossover_lines(f"{prefix} crossover", ch.crossover, profile))
        lines.append(_compressor_line(prefix, ch.compressor))
        if profile.fir_supported:
            if not ch.fir.bypass:
                lines.append(f"# {prefix} FIR is enabled in this preset, but its taps can't be exported as commands")
            lines.append(f"{prefix} fir bypass on")

    if mute_wrap:
        unmuted = [n for n, ch in enumerate(doc.outputs) if not ch.mute]
        if unmuted:
            lines.append("# Final mute states")
            lines.extend(f"output {n} mute off" for n in unmuted)
    return "\n".join(lines) + "\n"
