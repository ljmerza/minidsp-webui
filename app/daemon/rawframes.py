"""Read-only DSP memory readback over minidspd's raw HID frame bridge ``/devices/{i}/ws`` (docs 03 §9, spike 0.8).

The bridge forwards binary websocket messages to the device's transport hub as HID frames and broadcasts every
frame the device sends back to every client of the hub. Only ``ReadFloats`` (opcode ``0x14``) is ever sent from
here: the app never writes raw frames.

Framing (``protocol/src/packet.rs``): ``[len = len(payload) + 1] + payload + [sum(len + payload) & 0xFF]``. The
``ReadFloats`` payload is ``0x14, addr_hi, addr_lo, count``. The address is two bytes big-endian even on devices
whose writes use the 3-byte ``AddrLen3`` dialect (the 2x4HD), and ``count`` is at most 14.

A response is one HID frame (64 bytes): ``msg[0]`` is the length including itself and ``msg[1:msg[0]]`` is
``0x14, base_hi, base_lo`` followed by ``count`` little-endian 32-bit words. Bytes past ``msg[0]`` are stale buffer
contents and responses carry no checksum. Integer registers (gates, delays, polarity, status) hold u32 values in the
same 4-byte slots.

Reads inside biquad regions snap to the biquad boundary, so biquads are read one or two at a time from their first
coefficient. Every response is broadcast, so frames are matched on opcode, base address and length; everything else
(write acks from other clients, level reads by the daemon) is skipped.
"""

import asyncio
import contextlib
import struct
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.daemon.client import DaemonError, DaemonUnavailable

READ_FLOATS = 0x14
MAX_READ_FLOATS = 14
BIQUAD_LEN = 5
MAX_ADDR = 0xFFFF

# Gate (mute) status registers on AddrLen3 devices: minidsp-rs `Dialect::mute` writes 1 for muted, 2 for unmuted.
GATE_MUTED = 1
GATE_UNMUTED = 2


class RawFrameError(DaemonError):
    """The raw frame bridge refused the connection, closed it, or never answered a read."""


class RawFrameTimeout(RawFrameError):
    """No matching response arrived for a read."""


# --- framing ------------------------------------------------------------------------------------------------------


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def frame(payload: bytes) -> bytes:
    """``[len(payload) + 1] + payload + [checksum]``, as `protocol::packet::frame`."""
    if len(payload) > 62:
        raise ValueError(f"payload of {len(payload)} bytes does not fit in one HID frame")
    head = bytes([len(payload) + 1]) + payload
    return head + bytes([checksum(head)])


def read_floats_frame(addr: int, count: int) -> bytes:
    if not 0 <= addr <= MAX_ADDR:
        raise ValueError(f"address {addr} out of range")
    if not 1 <= count <= MAX_READ_FLOATS:
        raise ValueError(f"a read covers 1..{MAX_READ_FLOATS} floats, not {count}")
    return frame(bytes([READ_FLOATS, addr >> 8, addr & 0xFF, count]))


def unframe(msg: bytes) -> bytes | None:
    """The data of a response frame (``msg[1:msg[0]]``), or None for an empty or truncated frame."""
    if not msg:
        return None
    length = msg[0]
    if length == 0 or len(msg) < length:
        return None
    return bytes(msg[1:length])


@dataclass(frozen=True)
class FloatData:
    """One ``ReadFloats`` response: ``count`` 4-byte words starting at ``base``."""

    base: int
    raw: bytes

    def __len__(self) -> int:
        return len(self.raw) // 4

    def floats(self) -> list[float]:
        return list(struct.unpack(f"<{len(self)}f", self.raw))

    def words(self) -> list[int]:
        return list(struct.unpack(f"<{len(self)}I", self.raw))

    def float_at(self, addr: int) -> float:
        return self.floats()[self._offset(addr)]

    def word_at(self, addr: int) -> int:
        return self.words()[self._offset(addr)]

    def _offset(self, addr: int) -> int:
        offset = addr - self.base
        if not 0 <= offset < len(self):
            raise IndexError(f"address {addr} is not in the block read at {self.base}")
        return offset


def parse_float_data(msg: bytes) -> FloatData | None:
    """A ``ReadFloats`` response, or None for any other frame (acks, other commands, truncated frames)."""
    data = unframe(msg)
    if data is None or len(data) < 3 or data[0] != READ_FLOATS:
        return None
    payload = data[3:]
    if len(payload) % 4:
        return None
    return FloatData(base=(data[1] << 8) | data[2], raw=payload)


# --- read planning ------------------------------------------------------------------------------------------------


def plan_scalar_reads(addrs: Iterable[int], max_count: int = MAX_READ_FLOATS) -> list[tuple[int, int]]:
    """``(base, count)`` reads covering every address, grouping nearby ones into reads of at most ``max_count``.

    Only for plain registers: biquad regions need `plan_biquad_reads`.
    """
    reads: list[tuple[int, int]] = []
    start = last = None
    for addr in sorted(set(addrs)):
        if start is not None and last is not None and addr - start < max_count:
            last = addr
            continue
        if start is not None and last is not None:
            reads.append((start, last - start + 1))
        start = last = addr
    if start is not None and last is not None:
        reads.append((start, last - start + 1))
    return reads


def plan_biquad_reads(addrs: Iterable[int]) -> list[tuple[int, int]]:
    """``(base, count)`` reads for biquads (each 5 floats from ``addr``): two adjacent biquads per read at most."""
    per_read = MAX_READ_FLOATS // BIQUAD_LEN
    reads: list[tuple[int, int]] = []
    run: list[int] = []
    for addr in sorted(set(addrs)):
        if run and (addr != run[-1] + BIQUAD_LEN or len(run) == per_read):
            reads.append((run[0], len(run) * BIQUAD_LEN))
            run = []
        run.append(addr)
    if run:
        reads.append((run[0], len(run) * BIQUAD_LEN))
    return reads


# --- memory map ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryMap:
    """Addresses of the readable settings of one device model (from minidsp-rs ``protocol/src/device/*.rs``).

    PEQ tables are indexed like minidsp-rs and the daemon's config API (band 0 is the first entry of the device
    spec, which on the 2x4HD is the highest-numbered symbol). Crossover groups list the address of their first
    biquad; the group's biquads follow every `BIQUAD_LEN` floats.
    """

    product_name: str
    input_gate: tuple[int, ...]
    input_gain: tuple[int, ...]
    input_peq: tuple[tuple[int, ...], ...]
    output_gate: tuple[int, ...]
    output_gain: tuple[int, ...]
    output_delay: tuple[int, ...]
    output_invert: tuple[int, ...]
    output_peq: tuple[tuple[int, ...], ...]
    output_crossover: tuple[tuple[int, ...], ...]
    compressor_threshold: tuple[int, ...]
    crossover_biquads: int = 4

    def crossover_biquad(self, output: int, group: int, biquad: int) -> int:
        if not 0 <= biquad < self.crossover_biquads:
            raise IndexError(f"crossover biquad {biquad} out of range")
        return self.output_crossover[output][group] + BIQUAD_LEN * biquad


def _m2x4hd() -> MemoryMap:
    # Symbols of protocol/src/device/m2x4hd.rs (generated by minidsp-devtools). Channel numbers in the names are
    # 1-based DSP blocks: 1-2 are the inputs, 3-6 the outputs.
    d_gain_status = {n: n - 1 for n in range(1, 7)}  # D_GAIN_n_0_STATUS
    d_gain = {n: 25 + n for n in range(1, 7)}  # D_GAIN_n_0
    comp_threshold = {n: 40 + 6 * (n - 3) for n in range(3, 7)}  # COMP_n_0_THRESHOLD
    delay = {n: 64 + (n - 3) for n in range(3, 7)}  # DELAY_n_0
    polarity_out = {n: 80 + (n - 1) for n in range(1, 5)}  # POLARITY_OUT_n_0
    peq = {(ch, k): 8280 + 50 * (ch - 1) + 5 * (k - 1) for ch in range(1, 7) for k in range(1, 11)}  # PEQ_ch_k
    bpf = {(ch, k): 8580 + 40 * (ch - 3) + 5 * (k - 1) for ch in range(3, 7) for k in (1, 5)}  # BPF_ch_k

    def peqs(ch: int) -> tuple[int, ...]:
        return tuple(peq[ch, k] for k in range(10, 0, -1))  # the device spec lists PEQ_ch_10 first

    return MemoryMap(
        product_name="2x4HD",
        input_gate=(d_gain_status[1], d_gain_status[2]),
        input_gain=(d_gain[1], d_gain[2]),
        input_peq=(peqs(1), peqs(2)),
        output_gate=tuple(d_gain_status[n] for n in range(3, 7)),
        output_gain=tuple(d_gain[n] for n in range(3, 7)),
        output_delay=tuple(delay[n] for n in range(3, 7)),
        output_invert=tuple(polarity_out[n] for n in range(1, 5)),
        output_peq=tuple(peqs(n) for n in range(3, 7)),
        output_crossover=tuple((bpf[n, 1], bpf[n, 5]) for n in range(3, 7)),
        compressor_threshold=tuple(comp_threshold[n] for n in range(3, 7)),
    )


M2X4HD_MAP = _m2x4hd()
MEMORY_MAPS: dict[str, MemoryMap] = {M2X4HD_MAP.product_name: M2X4HD_MAP}


def memory_map_for(product_name: str | None) -> MemoryMap | None:
    return MEMORY_MAPS.get(product_name) if product_name else None


# --- websocket reader ---------------------------------------------------------------------------------------------


def raw_ws_url(base_url: str, index: int) -> str:
    """``ws://host:port/devices/{index}/ws`` for a daemon base URL."""
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/devices/{index}/ws", "", ""))


class RawFrameReader:
    """One connection to the frame bridge. Reads are sequential: one request in flight at a time.

    ``connect`` is `websockets.asyncio.client.connect` (or a test double with the same call shape).
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 1.0,
        open_timeout: float = 5.0,
        retries: int = 1,
        connect: Callable[..., Any] = ws_connect,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.open_timeout = open_timeout
        self.retries = retries
        self._connect = connect
        self._ws: Any = None
        self.reads = 0  # requests sent, retries included
        self.skipped = 0  # frames that weren't the response being waited for

    async def __aenter__(self) -> Self:
        try:
            # minidspd's bridge treats any non-binary message (pings, close) as the end of the stream and never
            # answers it: no keepalive pings, and a short close timeout.
            self._ws = await self._connect(
                self.url, open_timeout=self.open_timeout, ping_interval=None, close_timeout=0.5, max_size=2**16
            )
        except InvalidStatus as exc:
            raise RawFrameError(f"minidspd refused the frame bridge ({exc})") from exc
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            raise DaemonUnavailable(f"minidspd frame bridge unreachable at {self.url}: {exc}") from exc
        return self

    async def __aexit__(self, *exc: object) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def read(self, addr: int, count: int) -> FloatData:
        """``count`` words from ``addr``. Retries ``retries`` times when no answer arrives within ``timeout``."""
        if self._ws is None:
            raise RawFrameError("the frame bridge is not connected")
        request = read_floats_frame(addr, count)
        for _ in range(self.retries + 1):
            self.reads += 1
            try:
                await self._ws.send(request)
                return await asyncio.wait_for(self._response(addr, count), self.timeout)
            except TimeoutError:
                continue
            except ConnectionClosed as exc:
                raise RawFrameError(f"minidspd closed the frame bridge: {exc}") from exc
        raise RawFrameTimeout(f"no answer to a read of {count} floats at {addr} (0x{addr:04x})")

    async def _response(self, addr: int, count: int) -> FloatData:
        while True:
            msg = await self._ws.recv()
            if isinstance(msg, (bytes, bytearray, memoryview)):
                data = parse_float_data(bytes(msg))
                if data is not None and data.base == addr and len(data) == count:
                    return data
            self.skipped += 1

    async def read_many(self, reads: Sequence[tuple[int, int]]) -> dict[int, FloatData]:
        """Every ``(base, count)`` read, keyed by base address."""
        return {base: await self.read(base, count) for base, count in reads}


def lookup(blocks: Iterable[FloatData], addr: int, count: int = 1) -> bytes:
    """The raw bytes of ``count`` words at ``addr`` from whichever block covers them."""
    for block in blocks:
        start = addr - block.base
        if start >= 0 and start + count <= len(block):
            return block.raw[4 * start : 4 * (start + count)]
    raise KeyError(f"address {addr} was not read")


def as_floats(raw: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def as_word(raw: bytes) -> int:
    return struct.unpack("<I", raw[:4])[0]
