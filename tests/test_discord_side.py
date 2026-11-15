"""The intake guard: what the bot must ignore, and how it names webhooks.

Ignoring bot and webhook messages is the first line of loop prevention — if it
regresses, the bridge will happily relay its own posts back onto RF.
"""

import asyncio

import pytest

from openhop_discord_bridge.discord_side import (
    BridgeDiscord,
    build_intents,
    flatten_for_mesh,
)


class FakeHook:
    def __init__(self, name="Mesh Bridge", token=None):
        self.name = name
        self.token = token
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id=1, existing=()):
        self.id = channel_id
        self.name = "public"
        self._existing = list(existing)
        self.created = []
        self.hook = FakeHook()

    async def webhooks(self):
        return self._existing

    async def create_webhook(self, name, reason=""):
        self.created.append(name)
        return self.hook


class FakeBridge:
    def __init__(self):
        self.config = type("C", (), {"webhook_name": "Mesh Bridge"})()
        self.handled = []

    async def handle_discord_message(self, message):
        self.handled.append(message)


def make_client(bridge=None):
    discord = pytest.importorskip("discord")
    # Intents.none() is a flag_value descriptor in discord.py 2.7 (same trap as
    # Intents.message_content); the constructor gives an all-false instance.
    client = BridgeDiscord(bridge or FakeBridge(), intents=discord.Intents())
    return client


def msg(author_bot=False, webhook_id=None):
    author = type("A", (), {"bot": author_bot})()
    return type("M", (), {
        "author": author, "webhook_id": webhook_id, "id": 5,
        "channel": type("C", (), {"id": 9})(),
    })()


def test_bot_messages_are_ignored():
    bridge = FakeBridge()
    client = make_client(bridge)
    asyncio.run(client.on_message(msg(author_bot=True)))
    assert bridge.handled == []


def test_webhook_messages_are_ignored():
    """Our own posts come back as webhooks; relaying them would loop."""
    bridge = FakeBridge()
    client = make_client(bridge)
    asyncio.run(client.on_message(msg(webhook_id=12345)))
    assert bridge.handled == []


def test_human_messages_are_handled():
    bridge = FakeBridge()
    client = make_client(bridge)
    m = msg()
    asyncio.run(client.on_message(m))
    assert bridge.handled == [m]


def test_webhook_is_created_once_and_cached():
    client = make_client()
    channel = FakeChannel(channel_id=7)

    async def scenario():
        first = await client.webhook_for(channel)
        second = await client.webhook_for(channel)
        return first, second

    first, second = asyncio.run(scenario())
    assert first is second, "webhook was recreated instead of cached"
    assert channel.created == ["Mesh Bridge"]


def test_existing_webhook_is_reused():
    client = make_client()
    existing = FakeHook(name="Mesh Bridge", token="t")  # reusable incoming webhook
    channel = FakeChannel(channel_id=8, existing=[existing])

    async def scenario():
        return await client.webhook_for(channel)

    assert asyncio.run(scenario()) is existing
    assert channel.created == []


def test_post_uses_sanitised_username():
    """Discord rejects a webhook username containing its own brand."""
    client = make_client()
    channel = FakeChannel(channel_id=11)

    async def scenario():
        client.get_channel = lambda cid: channel if cid == 11 else None
        await client.post_mesh_message(11, "VK5JPH Discord \U0001f916", "hello")

    asyncio.run(scenario())
    assert channel.hook.sent, "nothing was posted"
    assert channel.hook.sent[0]["username"] == "VK5JPH 🤖"
    mentions = channel.hook.sent[0]["allowed_mentions"]
    assert mentions.everyone is False and mentions.roles is False and mentions.users is False


def test_flatten_for_mesh():
    assert flatten_for_mesh("line one\nline two") == "line one line two"
    assert flatten_for_mesh("  spaced  \n\n  out  ") == "spaced out"


def test_intents_include_message_content():
    intents = build_intents()
    assert intents.message_content is True
