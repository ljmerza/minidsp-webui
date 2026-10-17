"""Raw frame framing, response parsing, read planning, the 2x4HD memory map and the websocket reader."""

import asyncio
import struct

import pytest
from websockets.asyncio.server import serve

from app.daemon.client import DaemonError, DaemonUnavailable
from app.daemon.rawframes import (
    M2X4HD_MAP,
    MAX_READ_FLOATS,
    FloatData,
    RawFrameError,
    RawFrameReader,
    RawFrameTimeout,
    as_floats,
    as_word,
    checksum,
    frame,
    lookup,
    memory_map_for,
    parse_float_data,
    plan_biquad_reads,
    plan_scalar_reads,
    raw_ws_url,
    read_floats_frame,
    unframe,
)

from .conftest import (
    ACK,
    LR4_80_LP,
    REQ_BLOCK_1C,
    REQ_DELAYS,
    REQ_GAIN_OUT3,
    REQ_GATES,
    RESP_BLOCK_1C,
    RESP_DELAYS,
    RESP_GAIN_OUT3,
    RESP_GATES,
    FakeBridge,
    f32,
    hid,
)

# --- framing ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("addr", "count", "recorded"),
    [(0x1E, 1, REQ_GAIN_OUT3), (0x1C, 14, REQ_BLOCK_1C), (0x00, 14, REQ_GATES), (0x40, 4, REQ_DELAYS)],
)
def test_read_request_matches_recorded_bytes(addr, count, recorded):
    assert read_floats_frame(addr, count) == recorded


def test_frame_layout_like_minidsp_rs():
    # protocol/src/packet.rs frame_test: len = payload + 1, checksum over len + payload.
    framed = frame(bytes([0x05, 0xFF, 0xDA, 0x02]))
    assert framed[0] == 5
    assert len(framed) == 6
    assert framed[-1] == checksum(framed[:-1]) == (5 + 0x05 + 0xFF + 0xDA + 0x02) & 0xFF


def test_address_is_two_bytes_big_endian_even_for_biquads():
    assert read_floats_frame(8580, 10)[1:5] == bytes([0x14, 0x21, 0x84, 10])


@pytest.mark.parametrize(("addr", "count"), [(-1, 1), (0x10000, 1), (0, 0), (0, MAX_READ_FLOATS + 1)])
def test_read_request_bounds(addr, count):
    with pytest.raises(ValueError):
        read_floats_frame(addr, count)


def test_frame_refuses_oversized_payload():
    with pytest.raises(ValueError):
        frame(bytes(63))


# --- responses ----------------------------------------------------------------------------------------------------


def test_parse_recorded_gain_block():
    data = parse_float_data(RESP_BLOCK_1C)
    assert data is not None
    assert (data.base, len(data)) == (0x1C, 14)
    values = data.floats()
    assert values[0] == pytest.approx(11.765434)
    assert values[12] == -30.0  # compressor threshold out1 at 0x28
    assert data.float_at(0x28) == -30.0


def test_parse_recorded_single_gain_ignores_stale_bytes():
    data = parse_float_data(RESP_GAIN_OUT3)
    assert data == FloatData(0x1E, bytes(4))
    assert data.floats() == [0.0]


def test_integer_registers_decode_as_u32():
    gates = parse_float_data(RESP_GATES)
    assert gates.words()[:6] == [2, 2, 2, 2, 1, 1]
    assert parse_float_data(RESP_DELAYS).word_at(0x40) == 864


def test_word_at_outside_the_block():
    with pytest.raises(IndexError):
        parse_float_data(RESP_DELAYS).word_at(0x44)


@pytest.mark.parametrize(
    "msg",
    [
        b"",
        ACK,  # len 1: empty payload
        bytes([0]) + bytes(63),
        bytes([0x3C, 0x14, 0x00]),  # shorter than its length byte
        hid(bytes([0x08, 0x31, 0x00, 0x1E, 0, 0, 0, 0])),  # another command
        hid(bytes([0x07, 0x14, 0x00, 0x1E, 0, 0, 0])),  # not whole words
        hid(bytes([0x03, 0x14, 0x00])),  # no base address
    ],
)
def test_non_float_frames_are_skipped(msg):
    assert parse_float_data(msg) is None


def test_unframe():
    assert unframe(bytes([3, 1, 2, 99])) == bytes([1, 2])
    assert unframe(b"") is None


def test_lookup_and_decoders():
    blocks = [parse_float_data(RESP_BLOCK_1C), parse_float_data(RESP_DELAYS)]
    assert as_floats(lookup(blocks, 0x28)) == [-30.0]
    assert as_word(lookup(blocks, 0x40)) == 864
    with pytest.raises(KeyError):
        lookup(blocks, 0x40, 5)  # only 4 words were read there


# --- planning -----------------------------------------------------------------------------------------------------


def test_scalar_plan_for_every_2x4hd_register():
    m = M2X4HD_MAP
    addrs = [*m.input_gate, *m.output_gate, *m.input_gain, *m.output_gain]
    addrs += [*m.compressor_threshold, *m.output_delay, *m.output_invert]
    reads = plan_scalar_reads(addrs)
    assert reads == [(0, 6), (26, 6), (40, 13), (58, 10), (80, 4)]
    assert all(count <= MAX_READ_FLOATS for _, count in reads)


def test_scalar_plan_edge_cases():
    assert plan_scalar_reads([]) == []
    assert plan_scalar_reads([5, 5, 5]) == [(5, 1)]
    assert plan_scalar_reads([0, 13, 14]) == [(0, 14), (14, 1)]


def test_biquad_plan_pairs_adjacent_biquads_only():
    assert plan_biquad_reads([8580, 8585, 8590, 8595, 8600]) == [(8580, 10), (8590, 10), (8600, 5)]
    assert plan_biquad_reads([8300, 8280, 8290]) == [(8280, 5), (8290, 5), (8300, 5)]
    assert plan_biquad_reads([]) == []


# --- memory map ---------------------------------------------------------------------------------------------------


def test_2x4hd_map_matches_minidsp_rs_symbols():
    # Values from protocol/src/device/m2x4hd.rs.
    m = memory_map_for("2x4HD")
    assert m is M2X4HD_MAP
    assert m.input_gate == (0, 1)  # D_GAIN_1/2_0_STATUS
    assert m.output_gate == (2, 3, 4, 5)  # D_GAIN_3..6_0_STATUS
    assert m.input_gain == (26, 27)
    assert m.output_gain == (28, 29, 30, 31)
    assert m.compressor_threshold == (40, 46, 52, 58)
    assert m.output_delay == (64, 65, 66, 67)
    assert m.output_invert == (80, 81, 82, 83)  # POLARITY_OUT_1..4_0
    # The device spec lists PEQ_n_10 first: band 0 is the highest-numbered symbol.
    assert m.input_peq[0][0] == 8325  # PEQ_1_10
    assert m.input_peq[0][9] == 8280  # PEQ_1_1
    assert m.input_peq[1][0] == 8375  # PEQ_2_10
    assert m.output_peq[0][0] == 8425  # PEQ_3_10
    assert m.output_peq[3][9] == 8530  # PEQ_6_1
    assert m.output_crossover == ((8580, 8600), (8620, 8640), (8660, 8680), (8700, 8720))  # BPF_n_1, BPF_n_5
    assert m.crossover_biquad(0, 0, 2) == 8590
    assert m.crossover_biquad(3, 1, 3) == 8735
    with pytest.raises(IndexError):
        m.crossover_biquad(0, 0, 4)


def test_spike_addresses_line_up():
    # The spike's recorded block at 0x1c starts with D_GAIN of Out 1 and holds Out 1's threshold at 0x28.
    assert M2X4HD_MAP.output_gain[0] == 0x1C
    assert M2X4HD_MAP.compressor_threshold[0] == 0x28
    assert M2X4HD_MAP.output_delay[0] == 0x40
    assert M2X4HD_MAP.output_gate[2:] == (0x04, 0x05)


def test_unknown_models_have_no_map():
    assert memory_map_for("4x10HD") is None
    assert memory_map_for(None) is None


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("http://127.0.0.1:5380", "ws://127.0.0.1:5380/devices/0/ws"),
        ("http://127.0.0.1:5380/", "ws://127.0.0.1:5380/devices/0/ws"),
        ("https://dsp.example/minidsp", "wss://dsp.example/minidsp/devices/0/ws"),
    ],
)
def test_raw_ws_url(base, url):
    assert raw_ws_url(base, 0) == url


# --- reader -------------------------------------------------------------------------------------------------------


async def test_reader_matches_responses_among_broadcast_noise():
    bridge = FakeBridge({0x28: f32(-30.0), 0x1C: f32(11.765434)})
    async with RawFrameReader("ws://x/devices/0/ws", connect=bridge.connect, timeout=0.5) as reader:
        block = await reader.read(0x1C, 14)
        single = await reader.read(0x28, 1)
    assert block.float_at(0x28) == -30.0
    assert block.floats()[0] == pytest.approx(11.765434)
    assert single.floats() == [-30.0]
    assert reader.reads == 2
    assert reader.skipped >= 6  # acks, a text frame, level reads and a same-base read of another length
    assert bridge.kwargs["ping_interval"] is None
    assert bridge.sockets[0].closed


async def test_reader_reads_biquads_bit_exact():
    bridge = FakeBridge({8580 + k: f32(v) for k, v in enumerate(LR4_80_LP)})
    async with RawFrameReader("ws://x", connect=bridge.connect) as reader:
        blocks = await reader.read_many([(8580, 10)])
    values = blocks[8580].floats()
    assert values[:5] == [struct.unpack("<f", f32(v))[0] for v in LR4_80_LP]
    assert values[5:] == [0.0] * 5


async def test_reader_retries_a_lost_read_then_times_out():
    bridge = FakeBridge()
    bridge.drop = 1
    async with RawFrameReader("ws://x", connect=bridge.connect, timeout=0.05, retries=1) as reader:
        await reader.read(0x40, 4)
        assert reader.reads == 2
        bridge.drop = 2
        with pytest.raises(RawFrameTimeout):
            await reader.read(0x40, 4)
    assert issubclass(RawFrameTimeout, DaemonError)


async def test_reader_requires_a_connection():
    with pytest.raises(RawFrameError):
        await RawFrameReader("ws://x").read(0, 1)


async def test_connection_refused_is_daemon_unavailable():
    with pytest.raises(DaemonUnavailable):
        async with RawFrameReader("ws://127.0.0.1:1/devices/0/ws", open_timeout=1):
            pass


async def _bridge_handler(ws):
    """A minimal frame bridge: answers ReadFloats with recorded-style frames, after a broadcast ack."""
    async for message in ws:
        addr, count = (message[2] << 8) | message[3], message[4]
        await ws.send(ACK)
        await ws.send(
            hid(bytes([4 + 4 * count, 0x14, addr >> 8, addr & 0xFF]) + struct.pack(f"<{count}I", *[7] * count))
        )


async def test_reader_over_a_real_websocket():
    async with serve(_bridge_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with RawFrameReader(raw_ws_url(f"http://127.0.0.1:{port}", 0), timeout=1) as reader:
            data = await reader.read(0x40, 4)
    assert data.words() == [7, 7, 7, 7]


async def test_server_refusing_the_upgrade_is_a_frame_error():
    async def refuse(connection, request):
        return connection.respond(503, "DeviceNotReady\n")

    async with serve(_bridge_handler, "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RawFrameError, match="refused"):
            async with RawFrameReader(f"ws://127.0.0.1:{port}/devices/0/ws", timeout=1):
                pass


async def test_bridge_closing_mid_read_is_a_frame_error():
    async def close_at_once(ws):
        await ws.recv()
        await ws.close()

    async with serve(close_at_once, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with RawFrameReader(f"ws://127.0.0.1:{port}/devices/0/ws", timeout=1) as reader:
            with pytest.raises(RawFrameError, match="closed"):
                await reader.read(0, 1)
    await asyncio.sleep(0)
