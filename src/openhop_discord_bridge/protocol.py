"""MeshCore companion protocol over TCP (client side, stdlib + asyncio only).

Wire format, verified against a live openHop Repeater frame server:

* host -> node frame: ``0x3C`` + uint16 LE length + payload
* node -> host frame: ``0x3E`` + uint16 LE length + payload

The first payload byte is the command code (host -> node) or the reply/push code
(node -> host). Codes below ``0x80`` answer the command currently in flight;
codes at or above ``0x80`` are unsolicited pushes.

Only what the bridge needs is implemented: APP_START, SET_CHANNEL,
GET_CHANNEL, and SEND_CHANNEL_TXT_MSG. Nothing here writes to the repeater's
configuration; channel provisioning rides the ordinary client protocol and the
node persists it itself.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

FRAME_TO_NODE = 0x3C
FRAME_FROM_NODE = 0x3E
MAX_FRAME_SIZE = 512

CMD_APP_START = 0x01
CMD_SEND_CHANNEL_TXT_MSG = 0x03
CMD_GET_CHANNEL = 0x1F
CMD_SET_CHANNEL = 0x20

RESP_OK = 0x00
RESP_ERROR = 0x01
RESP_SELF_INFO = 0x05
RESP_MSG_SENT = 0x06
RESP_CHANNEL_INFO = 0x12

TXT_PLAIN = 0x00

ERROR_CODES = {
    1: "unsupported_command",
    2: "not_found",
    3: "table_full",
    4: "bad_state",
    5: "file_io_error",
    6: "illegal_arg",
}


class CompanionError(Exception):
    """Protocol-level failure (bad frame, error reply, closed link)."""


class CompanionTimeout(CompanionError):
    """No reply arrived inside the deadline."""


@dataclass(frozen=True)
class Frame:
    code: int
    body: bytes

    @property
    def is_error(self) -> bool:
        return self.code == RESP_ERROR

    def error_text(self) -> str:
        if not self.is_error:
            return ""
        code = self.body[0] if self.body else 0
        return ERROR_CODES.get(code, f"error_{code}")


def encode_frame(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("frame payload must include a command code")
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(f"frame payload too large ({len(payload)} bytes)")
    return bytes([FRAME_TO_NODE]) + struct.pack("<H", len(payload)) + payload


def split_frames(buffer: bytes) -> tuple[List[bytes], bytes]:
    """Split a byte buffer into complete ``0x3E`` frame payloads plus the remainder."""
    payloads: List[bytes] = []
    view = buffer
    while True:
        if len(view) < 3:
            return payloads, view
        if view[0] != FRAME_FROM_NODE:
            raise CompanionError(f"unexpected start byte 0x{view[0]:02x}")
        length = struct.unpack("<H", view[1:3])[0]
        if length == 0:
            # A zero-length payload has no command byte; indexing it would raise
            # IndexError and tear down an otherwise healthy link.
            raise CompanionError("zero-length frame")
        if length > MAX_FRAME_SIZE:
            raise CompanionError(f"implausible frame length {length}")
        if len(view) < 3 + length:
            return payloads, view
        payloads.append(view[3:3 + length])
        view = view[3 + length:]


def cmd_app_start(app_name: str = "discord-bridge") -> bytes:
    return (
        bytes([CMD_APP_START, 0x03])
        + b"      "
        + app_name.encode("ascii", "replace")[:24]
    )


def cmd_set_channel(channel_idx: int, name: str, key: bytes) -> bytes:
    if not 0 <= channel_idx <= 255:
        raise ValueError(f"channel index out of range: {channel_idx}")
    if len(key) != 16:
        raise ValueError("channel key must be 16 bytes")
    raw = name.encode("utf-8")
    if len(raw) > 31:
        raise ValueError("channel name is longer than 31 bytes")
    return bytes([CMD_SET_CHANNEL, channel_idx]) + raw.ljust(32, b"\x00") + key


def cmd_get_channel(channel_idx: int) -> bytes:
    if not 0 <= channel_idx <= 255:
        raise ValueError(f"channel index out of range: {channel_idx}")
    return bytes([CMD_GET_CHANNEL, channel_idx])


def cmd_send_channel(channel_idx: int, text: bytes, timestamp: int) -> bytes:
    if not 0 <= channel_idx <= 255:
        raise ValueError(f"channel index out of range: {channel_idx}")
    return (
        bytes([CMD_SEND_CHANNEL_TXT_MSG, TXT_PLAIN, channel_idx])
        + (timestamp & 0xFFFFFFFF).to_bytes(4, "little")
        + text
    )


def parse_channel_info(body: bytes) -> Dict[str, object]:
    """RESP_CHANNEL_INFO body: channel_idx(1) + name(32)."""
    if len(body) < 33:
        raise CompanionError("short CHANNEL_INFO reply")
    return {
        "channel_idx": body[0],
        "name": body[1:33].split(b"\x00", 1)[0].decode("utf-8", "replace"),
    }


PushHandler = Callable[[Frame], Awaitable[None]]


class CompanionClient:
    """One TCP session with an openHop companion frame server.

    Commands are serialised by a lock; replies (code < 0x80) are matched to the
    in-flight command and pushes (>= 0x80) are handed to ``on_push``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        app_name: str = "discord-bridge",
        on_push: Optional[PushHandler] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.app_name = app_name
        self._on_push = on_push
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._reply_q: asyncio.Queue[Frame] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._generation = 0
        self.channels: Dict[str, int] = {}

    # ── lifecycle ──────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        """False once the peer has closed, even if our handle has not caught up.

        A half-closed socket sits in CLOSE-WAIT and ``is_closing()`` stays False,
        so the read loop's EOF flag is the reliable signal.
        """
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and not self._closed.is_set()
        )

    async def open(self, timeout: float = 10.0) -> None:
        async with self._lifecycle:
            if self.connected:
                return
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout
            )
            self._generation += 1
            generation = self._generation
            self._closed.clear()
            self._read_task = asyncio.create_task(
                self._read_loop(generation), name="companion-reader"
            )
        frame = await self.request(cmd_app_start(self.app_name), {RESP_SELF_INFO}, timeout=timeout)
        if frame.code != RESP_SELF_INFO:
            raise CompanionError(f"APP_START failed: {frame.error_text() or frame.code}")
        logger.info("companion %s:%s ready", self.host, self.port)

    async def close(self) -> None:
        async with self._lifecycle:
            self._closed.set()
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 - shutdown path
                pass
        self._writer = None
        self._reader = None

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _read_loop(self, generation: int) -> None:
        buffer = b""
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                payloads, buffer = split_frames(buffer)
                for payload in payloads:
                    frame = Frame(payload[0], payload[1:])
                    if frame.code < 0x80:
                        await self._reply_q.put(frame)
                    elif self._on_push is not None:
                        try:
                            await self._on_push(frame)
                        except Exception:  # noqa: BLE001 - a push must not kill the link
                            logger.exception("push handler failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a closed link
            logger.warning("companion read loop ended: %s", exc)
        finally:
            # Only the loop belonging to the CURRENT connection may declare the
            # link down; an orphaned loop from a replaced connection must not.
            if generation == self._generation:
                self._closed.set()

    # ── commands ───────────────────────────────────────────────────────
    async def request(
        self,
        payload: bytes,
        expect: Sequence[int],
        *,
        timeout: float = 10.0,
    ) -> Frame:
        if self._writer is None:
            raise CompanionError("not connected")
        async with self._lock:
            while not self._reply_q.empty():
                self._reply_q.get_nowait()
            self._writer.write(encode_frame(payload))
            await self._writer.drain()
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CompanionTimeout(f"no reply within {timeout}s")
                try:
                    frame = await asyncio.wait_for(self._reply_q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise CompanionTimeout(f"no reply within {timeout}s") from None
                if frame.code in expect or frame.code == RESP_ERROR:
                    if frame.code == RESP_ERROR:
                        raise CompanionError(f"node error: {frame.error_text()}")
                    return frame
                logger.debug("ignoring reply 0x%02x while awaiting %s", frame.code, expect)

    async def provision_channels(self, channels: Dict[str, bytes]) -> None:
        """Push ``name -> 16-byte key`` to the node, index order = insertion order.

        The node persists what it is given, so this doubles as provisioning for a
        fresh companion: no repeater restart and no direct database writes.
        """
        # Build the mapping first, then swap it in with a single assignment.
        # Clearing the live dict and repopulating it across many awaits let
        # _sender_loop read a half-built map during a reconnect and drop the
        # message it was holding (idx is None).
        async with self._lifecycle:
            resolved: Dict[str, int] = {}
            for idx, (name, key) in enumerate(channels.items()):
                await self.request(cmd_set_channel(idx, name, key), {RESP_OK})
                resolved[name] = idx
                logger.info("provisioned channel %s at index %d", name, idx)
            self.channels = resolved

    async def read_channels(self, limit: int = 40) -> Dict[str, int]:
        found: Dict[str, int] = {}
        for idx in range(limit):
            try:
                frame = await self.request(cmd_get_channel(idx), {RESP_CHANNEL_INFO}, timeout=5.0)
            except CompanionError:
                break
            info = parse_channel_info(frame.body)
            name = str(info["name"])
            if not name:
                break
            found[name] = idx
        return found

    async def send_channel(self, channel_idx: int, text: bytes, timestamp: int) -> None:
        await self.request(cmd_send_channel(channel_idx, text, timestamp), {RESP_OK, RESP_MSG_SENT})
