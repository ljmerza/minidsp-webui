"""Recorded raw frames (spike 0.8, real 2x4HD on minidspd 0.1.12) and a fake frame bridge backed by a DSP memory dict.

The fake behaves like the daemon's ``/devices/{i}/ws`` bridge: it checks each request's framing and checksum, answers
``ReadFloats`` with one 64-byte HID frame (stale bytes after ``msg[0]``), and interleaves the broadcast noise seen on
the real hub: write acks from other clients and the daemon's own level reads.
"""

import asyncio
import struct
from collections.abc import Callable
from typing import Any

import pytest

from app.daemon.rawframes import BIQUAD_LEN, M2X4HD_MAP, MemoryMap, checksum
from app.model.config_doc import ConfigDoc
from app.model.device_profile import M2X4HD, DeviceProfile
from app.sync import verify as svc

from ..session.conftest import fake, make_session, repo, session  # noqa: F401

# --- recorded frames (spike-results.md §0.8) ---------------------------------------------------------------------

STALE = bytes.fromhex("a5") * 64  # whatever was left in the HID buffer


def hid(data: bytes) -> bytes:
    """A 64-byte response frame: the recorded bytes padded with stale buffer contents."""
    return data + STALE[len(data) : 64]


REQ_GAIN_OUT3 = bytes.fromhex("0514001e0138")  # D_GAIN 0x1e x1
RESP_GAIN_OUT3 = hid(bytes.fromhex("0814001e00000000"))
REQ_BLOCK_1C = bytes.fromhex("0514001c0e43")  # 0x1c x14: output gains .. compressor threshold out1
RESP_BLOCK_1C = hid(
    bytes.fromhex("3c14001c")
    + bytes.fromhex("383f3c41")  # 11.765434 (+12 dB written)
    + bytes(4 * 11)
    + bytes.fromhex("0000f0c1")  # -30.0 @ 0x28
    + bytes(4)
)
REQ_GATES = bytes.fromhex("051400000e27")  # 0x00 x14: gates and status registers
RESP_GATES = hid(bytes.fromhex("3c140000") + struct.pack("<14I", 2, 2, 2, 2, 1, 1, *([1] * 8)))
REQ_DELAYS = bytes.fromhex("05140040045d")  # 0x40 x4
RESP_DELAYS = hid(bytes.fromhex("14140040") + struct.pack("<4I", 864, 0, 0, 0))
ACK = hid(bytes.fromhex("01"))  # a write ack for another client
LR4_80_LP = (6.828594e-06, 1.3657188e-05, 6.828594e-06, 1.9925952, -0.99262255)  # out1 xover group 0 biquad 0

# Gains as written -> D_GAIN readback (spike 0.2).
GAIN_READBACK = {
    12.0: 11.765434,
    6.0: 5.7448344,
    2.0: 1.7182927,
    1.0: 0.77836126,
    0.5: 0.26727933,
    0.0: 0.0,
    -0.1: -0.2757657,
    -1.0: -1.1598389,
    -6.0: -6.0206,
    -20.0: -20.205997,
    -72.0: -72.2472,
}


def gain_readback(db: float) -> float:
    return GAIN_READBACK.get(db, db - 0.25)


# --- device memory ------------------------------------------------------------------------------------------------


def f32(value: float) -> bytes:
    return struct.pack("<f", value)


def u32(value: int) -> bytes:
    return struct.pack("<I", value)


def memory_from(doc: ConfigDoc, mmap: MemoryMap = M2X4HD_MAP, profile: DeviceProfile = M2X4HD) -> dict[int, bytes]:
    """DSP memory holding ``doc`` the way the device stores it (gains read back biased low, delays as samples)."""
    mem: dict[int, bytes] = {}

    def biquad(addr: int, bq: Any) -> None:
        for k, key in enumerate(("b0", "b1", "b2", "a1", "a2")):
            mem[addr + k] = f32(getattr(bq, key))

    for n, ch in enumerate(doc.inputs):
        mem[mmap.input_gate[n]] = u32(1 if ch.mute else 2)
        mem[mmap.input_gain[n]] = f32(gain_readback(ch.gain_db))
        for k, band in enumerate(ch.peq):
            biquad(mmap.input_peq[n][k], band.compiled)
    for n, ch in enumerate(doc.outputs):
        mem[mmap.output_gate[n]] = u32(1 if ch.mute else 2)
        mem[mmap.output_gain[n]] = f32(gain_readback(ch.gain_db))
        mem[mmap.output_delay[n]] = u32(profile.delay_samples(ch.delay_ms))
        mem[mmap.output_invert[n]] = u32(int(ch.invert))
        mem[mmap.compressor_threshold[n]] = f32(ch.compressor.threshold_db)
        for k, band in enumerate(ch.peq):
            biquad(mmap.output_peq[n][k], band.compiled)
        for g, group in enumerate(ch.crossover):
            for b, bq in enumerate(group.compiled):
                biquad(mmap.crossover_biquad(n, g, b), bq)
    return mem


BIQUAD_REGION = 8280  # PEQ and crossover biquads start here; reads in it snap to a biquad boundary


class FakeWs:
    def __init__(self, bridge: "FakeBridge") -> None:
        self.bridge = bridge
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def send(self, message: bytes) -> None:
        bridge = self.bridge
        assert isinstance(message, bytes)
        assert message[0] == len(message) - 1, "length byte"
        assert message[-1] == checksum(message[:-1]), "checksum"
        assert message[1] == 0x14, "only ReadFloats may be sent"
        addr, count = (message[2] << 8) | message[3], message[4]
        bridge.requests.append((addr, count))
        if bridge.on_request is not None:
            bridge.on_request(addr, count)
        if bridge.drop > 0:
            bridge.drop -= 1
            return
        if bridge.noise:
            self.inbox.put_nowait(ACK)
            self.inbox.put_nowait("text frames are ignored")
            # The daemon's own level read and another client's read of a neighbouring block.
            self.inbox.put_nowait(hid(bytes([4 + 4 * 2, 0x14, 0x00, 0x44]) + bytes(8)))
            self.inbox.put_nowait(hid(bytes([4 + 4, 0x14, addr >> 8, addr & 0xFF]) + bytes(4)) if count != 1 else ACK)
        start = addr
        if addr >= BIQUAD_REGION:
            start = addr - (addr - BIQUAD_REGION) % BIQUAD_LEN
        data = b"".join(bridge.memory.get(start + i, bytes(4)) for i in range(count))
        self.inbox.put_nowait(hid(bytes([4 + 4 * count, 0x14, addr >> 8, addr & 0xFF]) + data))

    async def recv(self) -> Any:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


class FakeBridge:
    """``connect`` stands in for `websockets.asyncio.client.connect`."""

    def __init__(self, memory: dict[int, bytes] | None = None) -> None:
        self.memory = memory if memory is not None else {}
        self.requests: list[tuple[int, int]] = []
        self.urls: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.sockets: list[FakeWs] = []
        self.noise = True
        self.drop = 0  # requests to leave unanswered
        self.on_request: Callable[[int, int], None] | None = None
        self.fail: Exception | None = None

    async def connect(self, url: str, **kwargs: Any) -> FakeWs:
        if self.fail is not None:
            raise self.fail
        self.urls.append(url)
        self.kwargs = kwargs
        ws = FakeWs(self)
        self.sockets.append(ws)
        return ws


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge()


@pytest.fixture
def reader_factory(bridge):
    from app.daemon.rawframes import RawFrameReader

    return lambda url: RawFrameReader(url, timeout=0.2, connect=bridge.connect)


@pytest.fixture(autouse=True)
def _clear_reports():
    svc.forget_reports()
    yield
    svc.forget_reports()
