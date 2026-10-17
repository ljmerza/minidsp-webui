"""RIFF/WAVE parsing for FIR impulses: encodings, scaling (same as minidsp-rs), chunk layout and refusals."""

import struct

import numpy as np
import pytest

from app.formats.wav import WavError, read_fir_taps, read_wav, write_wav


def riff(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"WAVE"
    for chunk_id, data in chunks:
        body += chunk_id + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def fmt(code=1, channels=1, rate=96_000, bits=16, block_align=None) -> bytes:
    block_align = channels * bits // 8 if block_align is None else block_align
    return struct.pack("<HHIIHH", code, channels, rate, rate * block_align, block_align, bits)


TAPS = np.array([0.0, 0.5, -0.25, 0.125, 1.0 - 2**-10, -0.75])


@pytest.mark.parametrize(
    ("encoding", "bits", "tol"),
    [("pcm", 8, 1 / 128), ("pcm", 16, 1 / 32767), ("pcm", 24, 2**-23), ("pcm", 32, 2**-31), ("float", 32, 1e-7)],
)
@pytest.mark.parametrize("extensible", [False, True])
def test_round_trip_every_encoding(encoding, bits, tol, extensible):
    data = write_wav(TAPS, bits=bits, encoding=encoding, extensible=extensible)
    wav = read_wav(data)
    assert (wav.sample_rate, wav.channels, wav.bits, wav.encoding) == (96_000, 1, bits, encoding)
    assert wav.frames == len(TAPS)
    np.testing.assert_allclose(wav.samples[:, 0], TAPS, atol=tol)
    assert wav.description.startswith(f"{bits}-bit")


def test_scaling_matches_minidsp_rs():
    # 8-bit unsigned: (x - 128) / 128.
    assert read_wav(riff((b"fmt ", fmt(bits=8)), (b"data", bytes([0, 128, 255])))).samples[:, 0].tolist() == [
        -1.0,
        0.0,
        127 / 128,
    ]
    # 16-bit: x / i16::MAX, so +32767 is exactly 1.0.
    raw16 = struct.pack("<3h", -32768, 0, 32767)
    assert read_wav(riff((b"fmt ", fmt(bits=16)), (b"data", raw16))).samples[:, 0].tolist() == [
        -32768 / 32767,
        0.0,
        1.0,
    ]
    # 24-bit: x / 2^23, sign-extended.
    raw24 = bytes([0x00, 0x00, 0x80, 0xFF, 0xFF, 0x7F, 0x00, 0x00, 0x40])
    assert read_wav(riff((b"fmt ", fmt(bits=24)), (b"data", raw24))).samples[:, 0].tolist() == [
        -1.0,
        (2**23 - 1) / 2**23,
        0.5,
    ]
    # 32-bit: x / 2^31.
    raw32 = struct.pack("<2i", -(2**31), 2**30)
    assert read_wav(riff((b"fmt ", fmt(bits=32)), (b"data", raw32))).samples[:, 0].tolist() == [-1.0, 0.5]


def test_float32_taps_are_exact():
    taps = np.array([0.1, -0.3, 1e-9], dtype=np.float32)
    got = read_fir_taps(write_wav(taps))
    assert got.dtype == np.float64
    assert got.tolist() == taps.astype(np.float64).tolist()


def test_skips_unknown_chunks_and_padding():
    data = riff(
        (b"LIST", b"INFOabc"),  # odd length: padded
        (b"fmt ", fmt(bits=16)),
        (b"junk", b""),
        (b"data", struct.pack("<2h", 16384, -16384)),
    )
    np.testing.assert_allclose(read_wav(data).samples[:, 0], [16384 / 32767, -16384 / 32767])


def test_fmt_after_data_is_fine():
    data = riff((b"data", struct.pack("<h", 32767)), (b"fmt ", fmt(bits=16)))
    assert read_wav(data).samples[:, 0].tolist() == [1.0]


def test_stereo_frames_are_interleaved():
    wav = read_wav(write_wav([[0.5, -0.5], [0.25, -0.25]], channels=2))
    assert wav.channels == 2 and wav.samples.tolist() == [[0.5, -0.5], [0.25, -0.25]]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "not a WAV file"),
        (b"RIFF\x00\x00\x00\x00AVI LIST", "not a WAV file"),
        (b"RIFX\x00\x00\x00\x00WAVE", "big-endian"),
        (riff((b"data", b"\x00\x00")), "no fmt chunk"),
        (riff((b"fmt ", fmt())), "no data chunk"),
        (riff((b"fmt ", fmt()[:12]), (b"data", b"\x00\x00")), "at least 16"),
        (riff((b"fmt ", fmt()), (b"data", b"")), "no samples"),
        (riff((b"fmt ", fmt(bits=16)), (b"data", b"\x00\x00\x00")), "whole number"),
        (riff((b"fmt ", fmt(bits=16, block_align=4)), (b"data", b"\x00" * 4)), "inconsistent format"),
        (riff((b"fmt ", fmt(bits=12, block_align=2)), (b"data", b"\x00" * 4)), "12-bit PCM"),
        (riff((b"fmt ", fmt(code=3, bits=64)), (b"data", b"\x00" * 8)), "64-bit float"),
        (riff((b"fmt ", fmt(code=6, bits=8)), (b"data", b"\x00")), "unsupported WAV encoding 0x0006"),
        (riff((b"fmt ", fmt(channels=0)), (b"data", b"\x00\x00")), "no channels"),
        (riff((b"fmt ", fmt(code=3, bits=32)), (b"data", struct.pack("<f", float("nan")))), "NaN"),
        (riff((b"fmt ", fmt(code=0xFFFE)), (b"data", b"\x00\x00")), "EXTENSIBLE"),
    ],
)
def test_refuses_bad_files(data, message):
    with pytest.raises(WavError, match=message):
        read_wav(data)


def test_refuses_truncated_chunk():
    data = riff((b"fmt ", fmt()), (b"data", b"\x00\x00" * 4))[:-2]
    with pytest.raises(WavError, match="truncated"):
        read_wav(data)


def test_refuses_unknown_extensible_subformat():
    data = bytearray(write_wav([0.5], extensible=True))
    data[data.index(b"\x10\x00\x80\x00")] ^= 0xFF  # corrupt the GUID tail
    with pytest.raises(WavError, match="sub-format"):
        read_wav(bytes(data))


def test_fir_taps_rules():
    assert read_fir_taps(write_wav([1.0, 0.0, 0.0])).tolist() == [1.0, 0.0, 0.0]
    with pytest.raises(WavError, match="must be mono"):
        read_fir_taps(write_wav([[1.0, 1.0]], channels=2))
    with pytest.raises(WavError, match="must be 96 kHz.*48 kHz"):
        read_fir_taps(write_wav([1.0], sample_rate=48_000))
    with pytest.raises(WavError, match="4097 taps; the device takes at most 4096"):
        read_fir_taps(write_wav(np.r_[1.0, np.zeros(4096)]))
    assert len(read_fir_taps(write_wav(np.r_[1.0, np.zeros(4095)]))) == 4096
    with pytest.raises(WavError, match="all zeros"):
        read_fir_taps(write_wav([0.0, 0.0]))
    assert len(read_fir_taps(write_wav([1.0] * 10), max_taps=10, sample_rate=96_000)) == 10
