"""Read and decrypt MeshCore group text out of the Repeater's SQLite database.

The Repeater REST API has no channel-chat endpoint: ``/api/logs`` is operational
telemetry and ``/api/room_messages`` covers room servers only. Group text lives
in the ``packets`` table as ``type=5`` (GRP_TXT) rows holding the original
ciphertext, which is what this module decodes.

Payload layout, per the MeshCore wire format::

    [1 byte channel hash][2 byte HMAC][ciphertext...]

* HMAC = HMAC-SHA256(32-byte channel secret, ciphertext), truncated to 2 bytes
* AES-128-ECB with ``secret[:16]``
* plaintext = uint32 LE device timestamp | flags byte | NUL-terminated body

The leading channel-hash byte is NOT a reliable channel identifier (it collides
across channels), so a candidate key is accepted only when the MAC verifies.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import sqlite3
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

GROUP_TEXT_PACKET_TYPE = 5
MAC_BYTES = 2
BLOCK_BYTES = 16


@dataclass(frozen=True)
class MeshMessage:
    packet_id: int
    channel: str
    text: str
    device_timestamp: int
    transmitted: int


def open_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open the repeater database without any chance of writing to it.

    The Repeater runs in WAL mode; a writable handle from another process risks
    lock contention and, in the worst case, corruption. ``mode=ro`` is the whole
    point of this function existing as a function.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"repeater database not found: {path}")
    # Quote the path: a '?' or '#' in it would otherwise be parsed as URI syntax
    # and silently open the wrong file.
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def load_channel_secrets(db_path: str | Path) -> Dict[str, List[bytes]]:
    """Every secret for every channel name, across all companion identities.

    The same channel name appears under several ``companion_hash`` rows; the
    secret is identical between them in practice, but the decryptor tries every
    candidate rather than trusting one hash.
    """
    conn = open_read_only(db_path)
    try:
        rows = conn.execute(
            "SELECT name, secret FROM companion_channels WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, List[bytes]] = {}
    for row in rows:
        secret = bytes(row["secret"] or b"")
        if len(secret) >= 16:
            out.setdefault(str(row["name"]), []).append(secret)
    return out


def decrypt_group_text(
    payload_hex: str, candidates: Dict[str, List[bytes]]
) -> Optional[Dict[str, object]]:
    """Decrypt one GRP_TXT payload, or return ``None`` if nothing verifies.

    ``candidates`` maps channel name -> list of 32-byte secrets. The first
    (name, secret) pair whose MAC matches wins.
    """
    try:
        raw = bytes.fromhex(payload_hex)
    except (TypeError, ValueError):
        return None
    if len(raw) < 3 + BLOCK_BYTES:
        return None
    ciphertext = raw[3:]
    if len(ciphertext) % BLOCK_BYTES:
        return None  # unaligned ciphertext is not a real message
    mac = raw[1:3]

    for name, secrets in candidates.items():
        for secret in secrets:
            if not hmac.compare_digest(
                hmac.new(secret, ciphertext, hashlib.sha256).digest()[:MAC_BYTES], mac
            ):
                continue
            decryptor = Cipher(algorithms.AES(secret[:16]), modes.ECB()).decryptor()
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()
            if len(plaintext) < 5:
                return None
            flags = plaintext[4]
            if flags >> 2:
                return None  # unsupported group-text subtype
            body = plaintext[5:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            return {
                "channel": name,
                "text": body,
                "device_timestamp": int.from_bytes(plaintext[:4], "little"),
            }
    return None


class PacketTailer:
    """Tail ``packets`` for new group text and hand decoded messages upward.

    Deduplicates on ``(device_timestamp, text)``: every group text is logged once
    per hop/retransmission, so without this each message appears roughly four
    times.
    """

    def __init__(
        self,
        db_path: str | Path,
        secrets: Dict[str, List[bytes]],
        *,
        wanted_channels: Optional[Iterable[str]] = None,
        start_from_latest: bool = True,
        dedupe_size: int = 4000,
    ) -> None:
        self.db_path = str(db_path)
        self.secrets = secrets
        self.wanted = set(wanted_channels) if wanted_channels is not None else None
        self._cursor = 0
        self._seen: "OrderedDict[tuple, None]" = OrderedDict()
        self._dedupe_size = dedupe_size
        self._start_from_latest = start_from_latest
        self._initialised = False

    # ── cursor ─────────────────────────────────────────────────────────
    def _init_cursor(self) -> None:
        conn = open_read_only(self.db_path)
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM packets").fetchone()
            self._cursor = 0 if not self._start_from_latest else int(row["m"])
        finally:
            conn.close()
        self._initialised = True
        logger.info("packet tail starting at id %d", self._cursor)

    def _remember(self, key: tuple) -> bool:
        """Return True if this message is new, False if already seen."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._dedupe_size:
            self._seen.popitem(last=False)
        return True

    # ── reads ──────────────────────────────────────────────────────────
    def fetch_new(self, limit: int = 200) -> List[MeshMessage]:
        if not self._initialised:
            self._init_cursor()
        conn = open_read_only(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, payload, transmitted FROM packets "
                "WHERE type = ? AND id > ? ORDER BY id LIMIT ?",
                (GROUP_TEXT_PACKET_TYPE, self._cursor, limit),
            ).fetchall()
        finally:
            conn.close()

        out: List[MeshMessage] = []
        for row in rows:
            self._cursor = max(self._cursor, int(row["id"]))
            decoded = decrypt_group_text(row["payload"] or "", self.secrets)
            if decoded is None:
                continue
            channel = str(decoded["channel"])
            if self.wanted is not None and channel not in self.wanted:
                continue
            text = str(decoded["text"])
            key = (decoded["device_timestamp"], text, channel)
            if not self._remember(key):
                continue
            out.append(
                MeshMessage(
                    packet_id=int(row["id"]),
                    channel=channel,
                    text=text,
                    device_timestamp=int(decoded["device_timestamp"]),
                    transmitted=int(row["transmitted"] or 0),
                )
            )
        return out

    def _recover_cursor_if_rebuilt(self) -> bool:
        """Reset the cursor if the packets table went backwards.

        The cursor only ever advances. If the repeater DB is rebuilt, restored
        from backup or the table truncated, MAX(id) drops below it and every
        query then returns nothing — the bridge would silently stop relaying for
        the life of the process.
        """
        conn = open_read_only(self.db_path)
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM packets").fetchone()
        finally:
            conn.close()
        highest = int(row["m"])
        if highest < self._cursor:
            logger.warning(
                "packets table went backwards (cursor %d, max id %d) — resetting cursor; "
                "the database was probably rebuilt",
                self._cursor, highest,
            )
            self._cursor = 0
            return True
        return False

    async def run(self, on_message: Callable[[MeshMessage], None], poll_interval: float) -> None:
        import asyncio

        failures = 0
        while True:
            try:
                batch = await asyncio.to_thread(self.fetch_new)
                if not batch:
                    await asyncio.to_thread(self._recover_cursor_if_rebuilt)
                for message in batch:
                    try:
                        on_message(message)
                    except Exception:  # noqa: BLE001 - one message must not drop the batch
                        logger.exception("handler failed for packet %s", message.packet_id)
                failures = 0
            except FileNotFoundError:
                failures += 1
                # Throttled like any other failure: a wrong path would otherwise
                # emit ~43k log lines a day and never escalate.
                if failures == 1 or failures % 60 == 0:
                    logger.error(
                        "repeater database disappeared: %s (%d consecutive)",
                        self.db_path, failures,
                    )
            except Exception:  # noqa: BLE001 - never kill the tail loop
                failures += 1
                # Log the first failure loudly, then every 60th, so a locked or
                # broken DB cannot flood the log every poll interval.
                if failures == 1 or failures % 60 == 0:
                    logger.exception("packet tail iteration failed (%d consecutive)", failures)
            await asyncio.sleep(poll_interval)
