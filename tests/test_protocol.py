import pytest

from openhop_discord_bridge import protocol as p


def test_encode_frame_layout():
    frame = p.encode_frame(b"\x01\x03")
    assert frame[0] == p.FRAME_TO_NODE
    assert frame[1:3] == b"\x02\x00"
    assert frame[3:] == b"\x01\x03"


def test_encode_frame_rejects_empty():
    with pytest.raises(ValueError):
        p.encode_frame(b"")


def test_split_frames_handles_partial_and_multiple():
    one = bytes([p.FRAME_FROM_NODE]) + b"\x02\x00" + b"\x05\x00"
    two = bytes([p.FRAME_FROM_NODE]) + b"\x01\x00" + b"\x00"
    payloads, rest = p.split_frames(one + two + b"\x3e\x02")
    assert payloads == [b"\x05\x00", b"\x00"]
    assert rest == b"\x3e\x02"


def test_split_frames_rejects_bad_start_byte():
    with pytest.raises(p.CompanionError):
        p.split_frames(b"\x41\x01\x00")


def test_cmd_send_channel_bytes():
    payload = p.cmd_send_channel(3, b"hi", 0x01020304)
    assert payload[:3] == bytes([0x03, 0x00, 0x03])
    assert payload[3:7] == b"\x04\x03\x02\x01"
    assert payload[7:] == b"hi"


def test_cmd_send_channel_rejects_bad_index():
    with pytest.raises(ValueError):
        p.cmd_send_channel(256, b"x", 0)


def test_cmd_set_channel_layout():
    payload = p.cmd_set_channel(2, "Public", b"k" * 16)
    assert payload[0] == 0x20
    assert payload[1] == 2
    assert payload[2:34] == b"Public".ljust(32, b"\x00")
    assert payload[34:] == b"k" * 16


def test_cmd_set_channel_rejects_wrong_key_size():
    with pytest.raises(ValueError):
        p.cmd_set_channel(0, "Public", b"short")


def test_parse_channel_info():
    body = bytes([4]) + b"Adelaide Chat".ljust(32, b"\x00")
    info = p.parse_channel_info(body)
    assert info == {"channel_idx": 4, "name": "Adelaide Chat"}


def test_parse_channel_info_short_body():
    with pytest.raises(p.CompanionError):
        p.parse_channel_info(b"\x00abc")


def test_error_code_mapping():
    frame = p.Frame(p.RESP_ERROR, bytes([6]))
    assert frame.is_error
    assert frame.error_text() == "illegal_arg"


def test_client_is_not_connected_before_open():
    client = p.CompanionClient("127.0.0.1", 5003)
    assert client.connected is False


def test_closed_event_makes_client_disconnected():
    """A half-closed socket (CLOSE-WAIT) must not read as connected."""
    import asyncio

    async def scenario():
        client = p.CompanionClient("127.0.0.1", 5003)
        client._writer = type("W", (), {"is_closing": lambda self: False})()
        assert client.connected is True
        client._closed.set()  # what the read loop does on peer EOF
        assert client.connected is False
        await client.close()

    asyncio.run(scenario())
