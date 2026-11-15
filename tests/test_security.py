"""Adversarial tests: try to break the bridge the way a stranger's input would.

These stand in for the independent reviewer pass that could not run (the
delegation model never returned a first token). They are deliberately hostile:
a non-allowlisted user trying to transmit, a mesh flood, `@everyone` in mesh
text, empty bodies, malformed packets, and a URL whose colon looks like a sender.
"""

import asyncio
import json

import pytest

from openhop_discord_bridge.config import (
    MANIFEST_DEFAULTS,
    load_config,
)
from openhop_discord_bridge.policy import (
    Outbound,
    PendingEcho,
)
from openhop_discord_bridge.reader import MeshMessage
from openhop_discord_bridge.server import Bridge

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 40
JOSH = 365689347485204482
STRANGER = 111111111111111111


# ── fakes ────────────────────────────────────────────────────────────────
class FakeHook:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.sent.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.hook = FakeHook()

    async def webhooks(self):
        return []

    async def create_webhook(self, name: str, reason: str = ""):
        return self.hook


class FakeDiscord:
    """Just enough of discord.Client for the bridge's own code paths."""

    def __init__(self, channel_id: int) -> None:
        self.channel = FakeChannel(channel_id)
        self.closed = False

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    async def post_mesh_message(self, channel_id, author, text):
        await self.channel.hook.send(content=text, username=author, allowed_mentions="none")

    async def close(self):
        self.closed = True


class FakeAuthor:
    def __init__(self, user_id: int, roles=(), name="user", display="User"):
        self.id = user_id
        self.roles = [type("R", (), {"id": r})() for r in roles]
        self.name = name
        self.display_name = display
        self.bot = False


class FakeMessage:
    def __init__(self, author, channel_id: int, content: str, message_id=1):
        self.author = author
        self.channel = type("C", (), {"id": channel_id})()
        self.clean_content = content
        self.id = message_id
        self.webhook_id = None
        self.reactions: list[str] = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def make_bridge(tmp_path, **overrides):
    data = dict(MANIFEST_DEFAULTS)
    data.update({
        "discord_token": TOKEN,
        "channels": [
            {
                "mesh_channel": "Public",
                "discord_channel_id": "1571423335894749224",
                "mesh_to_discord": True,
                "discord_to_mesh": True,
                "allowed_user_ids": [str(JOSH)],
            }
        ],
    })
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    cfg = load_config(path)
    return Bridge(cfg)


def mesh_msg(text: str, channel: str = "Public") -> MeshMessage:
    return MeshMessage(
        packet_id=1, channel=channel, text=text, device_timestamp=1, transmitted=0
    )


# ── security: who may transmit onto RF ───────────────────────────────────
def test_stranger_cannot_transmit(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.discord = FakeDiscord(1571423335894749224)
    msg = FakeMessage(FakeAuthor(STRANGER), 1571423335894749224, "let me on the air")

    asyncio.run(bridge.handle_discord_message(msg))

    assert len(bridge.queue) == 0, "a non-allowlisted user queued a transmission"
    assert "\u26d4" in msg.reactions, "refusal was not shown to the sender"


def test_allowlisted_user_transmits(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.discord = FakeDiscord(1571423335894749224)
    msg = FakeMessage(FakeAuthor(JOSH, name="anquietas86", display="Josh"), 1571423335894749224, "hello")

    asyncio.run(bridge.handle_discord_message(msg))

    assert len(bridge.queue) == 1
    item = bridge.queue.get()
    assert item.mesh_channel == "Public"
    assert item.text == "anquietas86: hello" or item.text == "Josh: hello"


def test_role_allowlist_transmits_without_user_entry(tmp_path):
    bridge = make_bridge(
        tmp_path,
        channels=[{
            "mesh_channel": "Public",
            "discord_channel_id": "1571423335894749224",
            "mesh_to_discord": True,
            "discord_to_mesh": True,
            "allowed_role_ids": ["42"],
        }],
    )
    bridge.discord = FakeDiscord(1571423335894749224)
    msg = FakeMessage(FakeAuthor(STRANGER, roles=[42]), 1571423335894749224, "via role")
    asyncio.run(bridge.handle_discord_message(msg))
    assert len(bridge.queue) == 1


def test_empty_allowlist_means_nobody(tmp_path):
    bridge = make_bridge(
        tmp_path,
        channels=[{
            "mesh_channel": "Public",
            "discord_channel_id": "1571423335894749224",
            "discord_to_mesh": True,
        }],
    )
    bridge.discord = FakeDiscord(1571423335894749224)
    msg = FakeMessage(FakeAuthor(JOSH), 1571423335894749224, "hello")
    asyncio.run(bridge.handle_discord_message(msg))
    assert len(bridge.queue) == 0


# ── security: mesh text is untrusted ─────────────────────────────────────
def test_mentions_in_mesh_text_are_neutralised(tmp_path):
    """A mesh message containing @everyone must not ping the whole server."""
    bridge = make_bridge(tmp_path)
    fake = FakeDiscord(1571423335894749224)
    bridge.discord = fake

    async def scenario():
        bridge.on_mesh_message(mesh_msg("Attacker: @everyone you are all dumb"))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert fake.channel.hook.sent, "message was not relayed at all"
    assert fake.channel.hook.sent[0]["allowed_mentions"] == "none"


def test_url_in_mesh_text_is_not_read_as_a_sender():
    author, body = Bridge._split_sender("http://example.com:8080/x")
    assert author == "mesh"
    assert body == "http://example.com:8080/x"


def test_empty_body_is_not_posted(tmp_path):
    """Discord 400s on empty content; relaying it would be a logged rejection."""
    bridge = make_bridge(tmp_path)
    fake = FakeDiscord(1571423335894749224)
    bridge.discord = fake
    async def scenario():
        bridge.on_mesh_message(mesh_msg("Someone:"))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert fake.channel.hook.sent == []


# ── DoS: floods must not explode ─────────────────────────────────────────
def test_inbound_flood_is_throttled(tmp_path):
    bridge = make_bridge(tmp_path, inbound_burst=5, inbound_refill_seconds=60.0)
    fake = FakeDiscord(1571423335894749224)
    bridge.discord = fake
    async def scenario():
        for i in range(100):
            bridge.on_mesh_message(mesh_msg(f"Spammer: message {i}"))
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert len(fake.channel.hook.sent) <= 5, "flood was not throttled"
    assert bridge._inbound_dropped >= 90


def test_pending_echo_is_bounded():
    cache = PendingEcho(ttl_seconds=1000, max_entries=3)
    for i in range(50):
        cache.remember(f"msg{i}", Outbound("Public", "a", f"msg{i}"))
    assert len(cache) == 3, "pending-echo map grew without bound"


def test_pending_echo_expires():
    class Clock:
        t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    cache = PendingEcho(ttl_seconds=10, max_entries=100, clock=clock)
    cache.remember("a", Outbound("Public", "x", "a"))
    clock.t = 11
    assert cache.take("a") is None


# ── robustness ───────────────────────────────────────────────────────────
def test_malformed_packets_do_not_raise():
    from openhop_discord_bridge.reader import decrypt_group_text

    secrets = {"Public": [bytes(range(32))]}
    for payload in ("", "zz", "00", "00" * 40, "ab" * 200):
        assert decrypt_group_text(payload, secrets) is None


def test_missing_database_gives_a_clear_error(tmp_path):
    from openhop_discord_bridge.config import ConfigError

    bridge = make_bridge(tmp_path, repeater_db_path=str(tmp_path / "nope.db"))
    with pytest.raises(ConfigError) as exc:
        bridge._channel_keys()
    assert "repeater database" in str(exc.value)


def test_oversized_discord_message_is_truncated_to_budget(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.discord = FakeDiscord(1571423335894749224)
    msg = FakeMessage(FakeAuthor(JOSH, name="josh", display="Josh"), 1571423335894749224, "x" * 5000)
    asyncio.run(bridge.handle_discord_message(msg))
    item = bridge.queue.get()
    total = len((bridge.config.companion_node_name + ": " + item.text).encode())
    assert total <= bridge.config.max_mesh_bytes
