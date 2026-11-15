"""Regression tests for the second review pass (gpt-5.1), v0.8.1.

Findings 1 and 3 are the same defect class the first reviewer caught — a
stringified boolean and a message-stranded failure path — which the first round of
fixes missed in these two places. That is why a foreign model reviews this repo.
"""

import asyncio
import json
import pathlib

import pytest

from openhop_discord_bridge.config import ConfigError, load_config
from openhop_discord_bridge.protocol import CompanionClient

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 40


# ── finding 1: autoprovision must not be parsed two different ways ──────
@pytest.mark.parametrize("flag", ["false", False, 0])
def test_stringified_autoprovision_cannot_let_broken_config_pass(tmp_path, flag):
    """`"false"` used to satisfy the validation gate while runtime said False."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "discord_token": TOKEN,
        "discord_autoprovision": flag,
        "channels": [{"mesh_channel": "Public"}],  # no discord_channel_id
    }))
    with pytest.raises(ConfigError, match="discord_channel_id"):
        load_config(cfg)


def test_autoprovision_true_still_allows_unresolved_channels(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "discord_token": TOKEN,
        "discord_autoprovision": True,
        "channels": [{"mesh_channel": "Public"}],
    }))
    loaded = load_config(cfg)
    assert loaded.discord_autoprovision is True
    assert loaded.channels[0].discord_channel_id is None


# ── finding 2: channel map is swapped, never emptied in place ──────────
@pytest.mark.asyncio
async def test_provisioning_never_exposes_a_half_built_channel_map():
    """A reader must never see `channels` empty or partial mid-provision."""
    client = CompanionClient.__new__(CompanionClient)
    client._lifecycle = asyncio.Lock()
    client.channels = {"Public": 0, "Adelaide Chat": 1}

    observed = []

    async def fake_request(frame, expect):
        # The old, complete map must remain readable for the whole operation.
        observed.append(dict(client.channels))
        return object()

    client.request = fake_request
    import openhop_discord_bridge.protocol as proto

    proto_cmd = proto.cmd_set_channel

    await client.provision_channels({"Public": b"k" * 16, "Adelaide Chat": b"k" * 16})

    assert all(len(snapshot) == 2 for snapshot in observed), f"partial map seen: {observed}"
    assert client.channels == {"Public": 0, "Adelaide Chat": 1}
    assert proto_cmd is not None


# ── finding 3: a missing channel index must fail visibly, not silently ─
@pytest.mark.asyncio
async def test_missing_channel_index_marks_the_message_failed():
    from openhop_discord_bridge.policy import Outbound, OutboundQueue, PendingEcho
    from openhop_discord_bridge.server import REACT_FAILED, Bridge

    class NoChannels:
        channels = {}
        connected = True

        async def open(self):
            return None

        async def provision_channels(self, keys):
            raise RuntimeError("companion has no channels")

        async def send_channel(self, *a, **k):
            raise AssertionError("must not attempt to send without an index")

    bridge = Bridge.__new__(Bridge)
    bridge.link = NoChannels()
    bridge.discord = None
    bridge.config = type("C", (), {"tx_retries": 1, "link_check_seconds": 5})()
    bridge.queue = OutboundQueue(maxsize=4)
    bridge._pending_echo = PendingEcho()
    bridge._channel_keys = lambda: {}

    reactions = []

    async def _react(item, emoji):
        reactions.append(emoji)

    bridge._react = _react
    bridge.queue.put(Outbound(mesh_channel="Public", author="Josh", text="hi", discord_channel_id=1))

    task = asyncio.create_task(bridge._sender_loop())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if reactions:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert REACT_FAILED in reactions, "user must see a failure tick, not a permanent queued tick"


# ── found while writing the public example config ──────────────────────
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_channel_id_means_unresolved_not_invalid(blank):
    """A form that renders an empty string must not be rejected as non-numeric."""
    from openhop_discord_bridge.config import ChannelMapping

    mapping = ChannelMapping.from_dict(
        {"mesh_channel": "Public", "discord_channel_id": blank}
    )
    assert mapping.discord_channel_id is None


def test_junk_channel_id_is_still_rejected():
    from openhop_discord_bridge.config import ChannelMapping

    with pytest.raises(ConfigError, match="non-numeric"):
        ChannelMapping.from_dict(
            {"mesh_channel": "Public", "discord_channel_id": "not-an-id"}
        )


def test_example_config_validates_against_the_loader(tmp_path):
    """examples/config.example.json must be copy-paste usable."""
    data = json.loads((pathlib.Path(__file__).resolve().parents[1]
                       / "examples" / "config.example.json").read_text())
    data["discord_token"] = TOKEN
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(data))
    loaded = load_config(cfg_path)
    assert len(loaded.channels) == 2
    assert loaded.discord_autoprovision is True
