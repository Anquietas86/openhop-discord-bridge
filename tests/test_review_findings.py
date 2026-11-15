"""Regression tests for the independent review findings (v0.8.0).

Each test pins one defect the external reviewer reported, so the fix cannot be
quietly undone. Where a test targets long-running behaviour (retry, exit status,
cursor recovery) it exercises the real code path with fakes rather than mocks of
our own logic.
"""

import asyncio
import json

import pytest

from openhop_discord_bridge.config import (
    ChannelMapping,
    ConfigError,
    load_config,
)
from openhop_discord_bridge.protocol import CompanionError, split_frames
from openhop_discord_bridge.reader import PacketTailer

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 40


# ── 1. strict booleans: "false" must not enable transmission ────────────
@pytest.mark.parametrize(
    "value,expected",
    [(False, False), ("false", False), ("FALSE", False), (0, 0), ("", False), (True, True), ("true", True), (1, 1)],
)
def test_boolean_flags_are_parsed_strictly(value, expected):
    mapping = ChannelMapping.from_dict(
        {"mesh_channel": "Public", "discord_channel_id": 1, "discord_to_mesh": value}
    )
    assert mapping.discord_to_mesh is bool(expected)


@pytest.mark.parametrize("value", ["maybe", "2", [], {}, 3])
def test_nonsense_boolean_is_rejected_not_guessed(value):
    with pytest.raises(ConfigError):
        ChannelMapping.from_dict(
            {"mesh_channel": "Public", "discord_channel_id": 1, "discord_to_mesh": value}
        )


# ── 2. allowlist typos fail loudly, naming the field ────────────────────
def test_non_numeric_allowlist_entry_names_the_field():
    with pytest.raises(ConfigError, match="allowed_user_ids"):
        ChannelMapping.from_dict(
            {"mesh_channel": "Public", "discord_channel_id": 1, "allowed_user_ids": ["abc"]}
        )


# ── 3. endpoint validation ──────────────────────────────────────────────
@pytest.mark.parametrize("port", [0, 70000, -1])
def test_impossible_port_is_rejected_at_load(tmp_path, port):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "discord_token": TOKEN,
        "companion_port": port,
        "channels": [{"mesh_channel": "Public", "discord_channel_id": 1}],
    }))
    with pytest.raises(ConfigError, match="companion_port"):
        load_config(cfg)


def test_empty_companion_host_is_rejected(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "discord_token": TOKEN,
        "companion_host": "  ",
        "channels": [{"mesh_channel": "Public", "discord_channel_id": 1}],
    }))
    with pytest.raises(ConfigError, match="companion_host"):
        load_config(cfg)


# ── 4. a zero-length frame must not tear down the link ──────────────────
def test_zero_length_frame_is_a_protocol_error_not_an_indexerror():
    frame = b"\x3e" + (0).to_bytes(2, "little")
    with pytest.raises(CompanionError):
        split_frames(frame)


def test_split_frames_still_parses_a_valid_frame():
    payload = b"\x05hello"
    payloads, rest = split_frames(b"\x3e" + len(payload).to_bytes(2, "little") + payload)
    assert payloads == [payload]
    assert rest == b""


# ── 5. cursor recovers if the packets table is rebuilt ─────────────────
def _make_db(path, ids):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE packets (id INTEGER PRIMARY KEY, type INT, packet BLOB, created_at REAL)"
    )
    for pid in ids:
        conn.execute(
            "INSERT INTO packets (id, type, packet, created_at) VALUES (?, 5, X'00', 0)",
            (pid,),
        )
    conn.commit()
    conn.close()


def test_cursor_resets_when_the_database_is_rebuilt(tmp_path):
    db = tmp_path / "repeater.db"
    _make_db(db, [1, 2, 3])
    tailer = PacketTailer(db, {})
    tailer._cursor = 500  # as if the table had reached 500 before the rebuild
    assert tailer._recover_cursor_if_rebuilt() is True
    assert tailer._cursor == 0


def test_cursor_is_left_alone_while_ids_keep_growing(tmp_path):
    db = tmp_path / "repeater.db"
    _make_db(db, [1, 2, 3])
    tailer = PacketTailer(db, {})
    tailer._cursor = 1
    assert tailer._recover_cursor_if_rebuilt() is False
    assert tailer._cursor == 1


# ── 6. a failed transmission is retried, not silently eaten ────────────
class _FlakyLink:
    """Companion that refuses the first send, then accepts."""

    def __init__(self, failures=1):
        self.failures = failures
        self.sent = []
        self.connected = True

    def provision_channels(self, channels):
        return None

    async def open(self):
        self.connected = True

    async def send_channel(self, idx, payload, stamp):
        if self.failures:
            self.failures -= 1
            raise CompanionError("link dropped")
        self.sent.append((idx, payload))


@pytest.mark.asyncio
async def test_failed_transmission_is_retried(tmp_path, monkeypatch):
    from openhop_discord_bridge.server import Bridge

    bridge = Bridge.__new__(Bridge)  # bypass __init__: only the retry path matters
    bridge.link = _FlakyLink(failures=1)
    bridge.discord = None
    bridge.config = type(
        "C",
        (),
        {"tx_retries": 2, "channels": [{"mesh_channel": "Public", "discord_channel_id": 1}]},
    )()

    from openhop_discord_bridge.policy import Outbound, PendingEcho

    bridge._pending_echo = PendingEcho()
    reactions = []

    async def _react(item, emoji):
        reactions.append(emoji)

    bridge._react = _react
    item = Outbound(mesh_channel="Public", author="Josh", text="hello", discord_channel_id=1)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    await bridge._retry_or_fail(item, 0)

    assert bridge.link.sent == [(0, b"hello")], "the message should reach RF on the retry"
    assert reactions and reactions[-1] != "FAILED"


async def _fast_sleep(*_args, **_kwargs):
    return None


# ── 7. config rewrite is atomic ────────────────────────────────────────
def test_persisting_channel_ids_is_atomic_and_leaves_valid_json(tmp_path, monkeypatch):
    from openhop_discord_bridge import config as config_module
    from openhop_discord_bridge.server import Bridge

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "discord_token": TOKEN,
        "channels": [{"mesh_channel": "Public", "discord_channel_id": None,
                      "allowed_user_ids": [123456789012345678]}],
    }))

    # The real writer resolves config_path() from the plugin data dir; point it
    # at the temp file so this exercises the atomic-write path for real.
    monkeypatch.setattr(config_module, "config_path", lambda: cfg)

    Bridge.__new__(Bridge)._persist_channel_ids({"Public": 123456789012345678})

    data = json.loads(cfg.read_text())
    assert data["channels"][0]["discord_channel_id"] == "123456789012345678"
    assert not list(tmp_path.glob("*.tmp")), "no temp file should be left behind"
    assert oct(cfg.stat().st_mode)[-3:] == "600"
