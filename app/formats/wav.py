"""WAV reader for FIR impulses (docs 02 FIR rows, docs 06 ``formats/wav.py``).

Parses RIFF/WAVE in memory (no temp files): PCM 8/16/24/32-bit and 32-bit IEEE float, plain or
``WAVE_FORMAT_EXTENSIBLE``. Samples are scaled the way minidsp-rs' ``fir import`` scales them, so a file loaded here
puts the same taps on the device as the CLI would: unsigned 8-bit ``(x - 128) / 128``, 16-bit ``x / 32767``,
24-bit ``x / 2^23``, float as is. minidsp-rs can't read 32-bit integer PCM; here it is scaled by ``1 / 2^31``.
"""

import math
import struct
from dataclasses import dataclass

import numpy as np

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
# Tail shared by the KSDATAFORMAT_SUBTYPE_* GUIDs; the first two bytes are the plain format code.
_SUBFORMAT_TAIL = bytes.fromhex("000000001000800000aa00389b71")
_PCM_BITS = (8, 16, 24, 32)


class WavError(ValueError):
    """The bytes aren't a WAV file this reader (or the device) can use."""


@dataclass(frozen=True)
class WavData:
    samples: np.ndarray  # float64, shape (frames, channels)
    sample_rate: int
    channels: int
    bits: int
    encoding: str  # "pcm" | "float"

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def description(self) -> str:
        kind = "PCM" if self.encoding == "pcm" else "float"
        layout = "mono" if self.channels == 1 else f"{self.channels} channels"
        return f"{self.bits}-bit {kind}, {layout}, {self.sample_rate / 1000:g} kHz"


@dataclass(frozen=True)
class _Format:
    code: int
    channels: int
    sample_rate: int
    block_align: int
    bits: int


def _parse_fmt(body: bytes) -> _Format:
    if len(body) < 16:
        raise WavError(f"the fmt chunk is {len(body)} bytes, expected at least 16")
    code, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", body)
    if code == WAVE_FORMAT_EXTENSIBLE:
        if len(body) < 40:
            raise WavError("WAVE_FORMAT_EXTENSIBLE without its 40-byte fmt extension")
        subformat = body[24:40]
        if subformat[2:] != _SUBFORMAT_TAIL:
            raise WavError("unknown WAVE_FORMAT_EXTENSIBLE sub-format")
        code = struct.unpack_from("<H", subformat)[0]
    return _Format(code, channels, rate, block_align, bits)


def _chunks(data: bytes) -> dict[bytes, bytes]:
    """First occurrence of each chunk after the RIFF/WAVE header."""
    if len(data) < 12 or data[8:12] != b"WAVE" or data[:4] not in (b"RIFF", b"RIFX"):
        raise WavError("not a WAV file (no RIFF/WAVE header)")
    if data[:4] == b"RIFX":
        raise WavError("big-endian (RIFX) WAV files aren't supported")
    chunks: dict[bytes, bytes] = {}
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = data[pos : pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        if start + size > len(data):
            name = chunk_id.decode("latin-1").strip()
            raise WavError(f"truncated file: the {name!r} chunk needs {size} bytes, {len(data) - start} are left")
        chunks.setdefault(chunk_id, data[start : start + size])
        pos = start + size + (size & 1)  # chunks are padded to an even length
    return chunks


def _decode(raw: bytes, fmt: _Format) -> tuple[np.ndarray, str]:
    if fmt.code == WAVE_FORMAT_PCM:
        if fmt.bits not in _PCM_BITS:
            raise WavError(f"{fmt.bits}-bit PCM isn't supported (8, 16, 24 or 32-bit)")
        if fmt.bits == 8:
            values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
        elif fmt.bits == 16:
            values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767.0
        elif fmt.bits == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
            values = ints.astype(np.float64) / float(1 << 23)
        else:
            values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
        return values, "pcm"
    if fmt.code == WAVE_FORMAT_IEEE_FLOAT:
        if fmt.bits != 32:
            raise WavError(f"{fmt.bits}-bit float isn't supported; export 32-bit float or PCM")
        values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        if not np.all(np.isfinite(values)):
            raise WavError("the file contains NaN or infinite samples")
        return values, "float"
    raise WavError(f"unsupported WAV encoding 0x{fmt.code:04x} (PCM or 32-bit float only)")


def read_wav(data: bytes) -> WavData:
    """Parse a whole WAV file. Raises `WavError` with a readable reason."""
    chunks = _chunks(bytes(data))
    if b"fmt " not in chunks:
        raise WavError("no fmt chunk")
    if b"data" not in chunks:
        raise WavError("no data chunk")
    fmt = _parse_fmt(chunks[b"fmt "])
    if fmt.channels < 1:
        raise WavError("the file declares no channels")
    if fmt.sample_rate < 1:
        raise WavError("the file declares no sample rate")
    width = math.ceil(fmt.bits / 8) if fmt.bits > 0 else 0
    if width == 0 or fmt.block_align != width * fmt.channels:
        raise WavError(
            f"inconsistent format: {fmt.channels} channel(s) of {fmt.bits} bits but {fmt.block_align} bytes per frame"
        )
    raw = chunks[b"data"]
    if len(raw) % fmt.block_align:
        raise WavError(f"the data chunk ({len(raw)} bytes) isn't a whole number of {fmt.block_align}-byte frames")
    if not raw:
        raise WavError("the file contains no samples")
    values, encoding = _decode(raw, fmt)
    return WavData(
        samples=values.reshape(-1, fmt.channels),
        sample_rate=fmt.sample_rate,
        channels=fmt.channels,
        bits=fmt.bits,
        encoding=encoding,
    )


def read_fir_taps(data: bytes, *, sample_rate: int = 96_000, max_taps: int = 4096) -> np.ndarray:
    """FIR taps (float64, 1-D) from a mono WAV at the device's internal rate, with at most ``max_taps`` samples."""
    wav = read_wav(data)
    if wav.channels != 1:
        raise WavError(f"a FIR impulse must be mono; this file has {wav.channels} channels")
    if wav.sample_rate != sample_rate:
        raise WavError(
            f"a FIR impulse must be {sample_rate / 1000:g} kHz (the device's internal rate); this file is "
            f"{wav.sample_rate / 1000:g} kHz. Resample it first"
        )
    if wav.frames > max_taps:
        raise WavError(f"the impulse has {wav.frames} taps; the device takes at most {max_taps}")
    taps = wav.samples[:, 0].copy()
    if not np.any(taps):
        raise WavError("the impulse is all zeros (it would silence the output)")
    return taps


def write_wav(
    samples: np.ndarray | list[float],
    *,
    sample_rate: int = 96_000,
    bits: int = 32,
    encoding: str = "float",
    channels: int = 1,
    extensible: bool = False,
) -> bytes:
    """Encode samples (frames x channels, or 1-D for mono) as a WAV file. Used for tests and FIR export."""
    arr = np.asarray(samples, dtype=np.float64).reshape(-1, channels)
    flat = arr.reshape(-1)
    if encoding == "float":
        if bits != 32:
            raise ValueError("only 32-bit float is supported")
        raw = flat.astype("<f4").tobytes()
        code = WAVE_FORMAT_IEEE_FLOAT
    elif encoding == "pcm":
        if bits == 8:
            raw = np.clip(np.round(flat * 128.0 + 128.0), 0, 255).astype(np.uint8).tobytes()
        elif bits == 16:
            raw = np.clip(np.round(flat * 32767.0), -32768, 32767).astype("<i2").tobytes()
        elif bits == 24:
            ints = np.clip(np.round(flat * (1 << 23)), -(1 << 23), (1 << 23) - 1).astype(np.int64) & 0xFFFFFF
            raw = np.stack([ints & 0xFF, (ints >> 8) & 0xFF, (ints >> 16) & 0xFF], axis=1).astype(np.uint8).tobytes()
        elif bits == 32:
            raw = np.clip(np.round(flat * (1 << 31)), -(1 << 31), (1 << 31) - 1).astype("<i4").tobytes()
        else:
            raise ValueError(f"{bits}-bit PCM is not supported")
        code = WAVE_FORMAT_PCM
    else:
        raise ValueError(f"unknown encoding {encoding!r}")
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", code, channels, sample_rate, sample_rate * block_align, block_align, bits)
    if extensible:
        guid = struct.pack("<H", code) + _SUBFORMAT_TAIL
        fmt = (
            struct.pack("<HHIIH", WAVE_FORMAT_EXTENSIBLE, channels, sample_rate, sample_rate * block_align, block_align)
            + struct.pack("<HHHI", bits, 22, bits, 0)
            + guid
        )
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    if len(raw) & 1:
        body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body
