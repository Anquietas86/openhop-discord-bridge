"""Exercise the companion protocol client against a fake frame server.

The client is the component that talks to real hardware, so its framing, request
correlation, error handling and timeouts matter more than its coverage number
suggests. A fake server speaks the same wire format: 0x3C + uint16 LE length +
payload from host to node, 0x3E back.
"""

import asyncio
import struct

import pytest

from openhop_discord_bridge import protocol as p

FRAME_TO_NODE = 0x3C
FRAME_FROM_NODE = 0x3E


def reply(code: int, body: bytes = b"") -> bytes:
    payload = bytes([code]) + body
    return bytes([FRAME_FROM_NODE]) + struct.pack("<H", len(payload)) + payload


class FakeNode:
    """Minimal companion frame server."""

    def __init__(self, *, fail_command: int | None = None, hang: bool = False) -> None:
        self.fail_command = fail_command
        self.hang = hang
        self.received: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None
        self.writers: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(3)
                if header[0] != FRAME_TO_NODE:
                    return
                length = struct.unpack("<H", header[1:3])[0]
                payload = await reader.readexactly(length)
                self.received.append(payload)
                command = payload[0]

                if self.hang:
                    continue
                if self.fail_command is not None and command == self.fail_command:
                    writer.write(reply(p.RESP_ERROR, bytes([6])))  # illegal_arg
                    await writer.drain()
                    continue

                if command == p.CMD_APP_START:
                    writer.write(reply(p.RESP_SELF_INFO, b"\x00" * 70))
                elif command == p.CMD_SET_CHANNEL:
                    writer.write(reply(p.RESP_OK))
                elif command == p.CMD_GET_CHANNEL:
                    idx = payload[1]
                    name = b"Public" if idx == 0 else b""
                    writer.write(reply(p.RESP_CHANNEL_INFO, bytes([idx]) + name.ljust(32, b"\x00")))
                elif command == p.CMD_SEND_CHANNEL_TXT_MSG:
                    writer.write(reply(p.RESP_OK))
                else:
                    writer.write(reply(p.RESP_ERROR, bytes([1])))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> "FakeNode":
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        # Close handlers first: wait_closed() waits for them, and they are parked
        # on a read, so it would never return.
        for writer in list(self.writers):
            writer.close()
        self.writers.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


def test_app_start_and_provision_channels():
    async def scenario():
        async with FakeNode() as node:
            client = p.CompanionClient("127.0.0.1", node.port)
            await client.open()
            await client.provision_channels({"Public": b"k" * 16, "#test": b"j" * 16})
            assert client.channels == {"Public": 0, "#test": 1}
            read_back = await client.read_channels(limit=3)
            await client.close()
            return node.received, read_back

    received, read_back = asyncio.run(scenario())
    assert received[0][0] == p.CMD_APP_START
    assert [r[0] for r in received[1:3]] == [p.CMD_SET_CHANNEL, p.CMD_SET_CHANNEL]
    # the client sets the name padded to 32 bytes and the 16-byte key
    assert received[1][2:34] == b"Public".ljust(32, b"\x00")
    assert received[1][34:] == b"k" * 16
    assert read_back == {"Public": 0}


def test_node_error_is_raised():
    async def scenario():
        async with FakeNode(fail_command=p.CMD_SET_CHANNEL) as node:
            client = p.CompanionClient("127.0.0.1", node.port)
            await client.open()
            try:
                await client.provision_channels({"Public": b"k" * 16})
            finally:
                await client.close()

    with pytest.raises(p.CompanionError) as exc:
        asyncio.run(scenario())
    assert "illegal_arg" in str(exc.value)


def test_timeout_when_node_never_replies():
    async def scenario():
        async with FakeNode(hang=True) as node:
            client = p.CompanionClient("127.0.0.1", node.port)
            await client.open(timeout=5)
            try:
                await client.request(p.cmd_app_start(), {p.RESP_SELF_INFO}, timeout=0.3)
            finally:
                await client.close()

    with pytest.raises(p.CompanionTimeout):
        asyncio.run(scenario())


def test_send_channel_puts_text_on_the_wire():
    async def scenario():
        async with FakeNode() as node:
            client = p.CompanionClient("127.0.0.1", node.port)
            await client.open()
            await client.send_channel(2, b"hello", 1234)
            await client.close()
            return node.received

    received = asyncio.run(scenario())
    sent = received[-1]
    assert sent[0] == p.CMD_SEND_CHANNEL_TXT_MSG
    assert sent[2] == 2
    assert sent[3:7] == (1234).to_bytes(4, "little")
    assert sent[7:] == b"hello"


def test_pushes_are_ignored_not_mistaken_for_replies():
    """A push (>=0x80) between request and reply must not satisfy the request."""

    class PushyNode(FakeNode):
        async def handle(self, reader, writer):
            header = await reader.readexactly(3)
            length = struct.unpack("<H", header[1:3])[0]
            await reader.readexactly(length)
            # unsolicited push first, then the real reply
            writer.write(bytes([FRAME_FROM_NODE]) + struct.pack("<H", 2) + bytes([0x83, 0x00]))
            writer.write(reply(p.RESP_SELF_INFO, b"\x00" * 70))
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()

    async def scenario():
        server = await asyncio.start_server(PushyNode().handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = p.CompanionClient("127.0.0.1", port)
            await client.open()
            await client.close()
            return True
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) is True


def test_connected_is_false_when_peer_closes():
    async def scenario():
        async with FakeNode() as node:
            client = p.CompanionClient("127.0.0.1", node.port)
            await client.open()
            assert client.connected
            await client.close()
            return client.connected

    assert asyncio.run(scenario()) is False
