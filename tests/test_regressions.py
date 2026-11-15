"""Tests for the two bugs found on the first live deployment.

1. discord.py 2.7: ``Intents.default() | Intents.message_content`` raises
   AttributeError because class-level access yields a flag_value descriptor.
2. A dotless 64-char hex secret (an openHop API key) reached the token field and
   produced only a bare 401 at login.
"""

import pytest

from openhop_discord_bridge.config import ConfigError, MANIFEST_DEFAULTS, load_config

GOOD_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 40  # synthetic, 72 chars, two dots


def write_cfg(tmp_path, **overrides):
    import json

    data = dict(MANIFEST_DEFAULTS)
    data["channels"] = [{"mesh_channel": "Public", "discord_channel_id": 123456789}]
    data["discord_token"] = GOOD_TOKEN
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_build_intents_sets_message_content():
    discord = pytest.importorskip("discord")
    from openhop_discord_bridge.discord_side import build_intents

    intents = build_intents()
    assert isinstance(intents, discord.Intents)
    assert intents.message_content is True
    assert intents.guilds is True
    # The regression: ORing the class attribute must never be reintroduced.
    with pytest.raises(AttributeError):
        _ = discord.Intents.default() | discord.Intents.message_content


def test_discord_client_token_api():
    """connect() takes no token; start() does. Using the wrong one is a TypeError.

    Pinned because discord.py's signatures are easy to mix up and the failure
    only appears at runtime, after the companion link is already up.
    """
    import inspect

    discord = pytest.importorskip("discord")
    connect_params = inspect.signature(discord.Client.connect).parameters
    start_params = inspect.signature(discord.Client.start).parameters
    assert "token" not in connect_params
    assert "token" in start_params


def test_valid_token_shape_accepted(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.discord_token == GOOD_TOKEN


def test_dotless_hex_token_rejected_with_helpful_message(tmp_path):
    bad = "1bfcc6495038b271" * 4  # 64 hex chars, like an openHop API key
    with pytest.raises(ConfigError) as exc:
        load_config(write_cfg(tmp_path, discord_token=bad))
    message = str(exc.value)
    assert "openHop API key" in message
    assert "Reset Token" in message


def test_token_with_one_dot_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write_cfg(tmp_path, discord_token="abc.def"))
    assert "two dots" in str(exc.value)


def test_env_token_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", GOOD_TOKEN + "X")
    cfg = load_config(write_cfg(tmp_path, discord_token="placeholder"))
    assert cfg.discord_token.endswith("X")


def test_string_channel_ids_are_accepted(tmp_path):
    """Ids may arrive as strings; a JSON editor that quotes them must still work."""
    from openhop_discord_bridge.config import ChannelMapping

    m = ChannelMapping.from_dict(
        {"mesh_channel": "Public", "discord_channel_id": "1571423335894749224"}
    )
    assert m.discord_channel_id == 1571423335894749224


def test_snowflake_survives_json_roundtrip_without_float(tmp_path):
    """A float-parsed id is knowingly lossy; the loader must at least not invent one."""
    import json

    from openhop_discord_bridge.config import ChannelMapping

    true_id = 1571423335894749224
    assert json.loads(json.dumps({"id": true_id}))["id"] == true_id
    # What a lossy editor produces, and why re-resolution by name is needed:
    lossy = int(float(true_id))
    assert lossy != true_id
    assert ChannelMapping.from_dict(
        {"mesh_channel": "Public", "discord_channel_id": lossy}
    ).discord_channel_id == lossy
