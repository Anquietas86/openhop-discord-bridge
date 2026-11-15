"""Auto-provisioning: channel naming and config rules for creating channels.

The bot creates the category and one text channel per mesh channel, then pins the
resolved ids back into config.json. Names must be valid Discord channel names,
and a mapping without an id is only legal when autoprovisioning is enabled.
"""

import json

import pytest

from openhop_discord_bridge.config import (
    MANIFEST_DEFAULTS,
    ChannelMapping,
    ConfigError,
    load_config,
    normalise_channel_name,
)

TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 40


def cfg_file(tmp_path, **overrides):
    data = dict(MANIFEST_DEFAULTS)
    data["discord_token"] = TOKEN
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Public", "public"),
        ("#hamradio", "hamradio"),
        ("Adelaide Chat", "adelaide-chat"),
        ("Hagger Family", "hagger-family"),
        ("#sa mesh meet 2026", "sa-mesh-meet-2026"),
        ("#alertsa", "alertsa"),
        ("  spaced  ", "spaced"),
        ("Odd//Chars!!", "odd-chars"),
    ],
)
def test_normalise_channel_name(raw, expected):
    assert normalise_channel_name(raw) == expected


def test_normalise_never_empty():
    assert normalise_channel_name("#") == "mesh"
    assert normalise_channel_name("") == "mesh"


def test_resolved_name_uses_prefix():
    mapping = ChannelMapping(mesh_channel="Public")
    assert mapping.resolved_channel_name() == "public"
    assert mapping.resolved_channel_name("mesh-") == "mesh-public"


def test_resolved_name_prefers_explicit_name():
    mapping = ChannelMapping(mesh_channel="Public", discord_channel_name="Mesh Public")
    assert mapping.resolved_channel_name() == "mesh-public"


def test_mapping_without_id_rejected_when_autoprovision_off(tmp_path):
    path = cfg_file(
        tmp_path,
        discord_autoprovision=False,
        channels=[{"mesh_channel": "Public"}],
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "discord_autoprovision" in str(exc.value)


def test_mapping_without_id_allowed_when_autoprovision_on(tmp_path):
    path = cfg_file(
        tmp_path,
        discord_autoprovision=True,
        discord_category_name="💬 | Meshcore",
        channels=[{"mesh_channel": "Public"}, {"mesh_channel": "#hamradio"}],
    )
    cfg = load_config(path)
    assert cfg.discord_autoprovision is True
    assert cfg.discord_category_name == "💬 | Meshcore"
    assert all(m.discord_channel_id is None for m in cfg.channels)
    assert [m.resolved_channel_name() for m in cfg.channels] == ["public", "hamradio"]


def test_explicit_ids_still_work_with_autoprovision(tmp_path):
    path = cfg_file(
        tmp_path,
        discord_autoprovision=True,
        channels=[{"mesh_channel": "Public", "discord_channel_id": 123456789}],
    )
    cfg = load_config(path)
    assert cfg.channels[0].discord_channel_id == 123456789


def test_sender_name_mode_validated(tmp_path):
    path = cfg_file(
        tmp_path,
        discord_autoprovision=True,
        channels=[{"mesh_channel": "Public"}],
        discord_sender_name="nickname",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "display_name" in str(exc.value) and "username" in str(exc.value)


def test_sender_name_mode_default_and_override(tmp_path):
    path = cfg_file(tmp_path, discord_autoprovision=True,
                    channels=[{"mesh_channel": "Public"}])
    assert load_config(path).discord_sender_name == "display_name"
    path2 = cfg_file(tmp_path, discord_autoprovision=True,
                     channels=[{"mesh_channel": "Public"}], discord_sender_name="username")
    assert load_config(path2).discord_sender_name == "username"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("🛰 SARG", "🛰-sarg"),
        ("WarMobile🏎️", "warmobile🏎️"),   # emoji + variation selector
        ("☕", "☕"),
        (".😊.", "😊"),
        ("Adelaide Chat", "adelaide-chat"),
    ],
)
def test_discord_channel_names_keep_emoji(raw, expected):
    """Discord accepts emoji in channel names; dropping them lost intent.

    Verified against the live API: creating "🛰-emoji-probe" returns 201 with the
    name intact, so a mesh channel named with an emoji can keep it.
    """
    assert normalise_channel_name(raw) == expected


def test_per_channel_sender_name_override(tmp_path):
    """A channel may differ from the global default (e.g. none on one channel)."""
    path = cfg_file(
        tmp_path,
        discord_autoprovision=True,
        discord_sender_name="username",
        channels=[
            {"mesh_channel": "Public"},
            {"mesh_channel": "#test", "sender_name": "none"},
            {"mesh_channel": "SARG", "discord_sender_name": "display_name"},
        ],
    )
    cfg = load_config(path)
    assert cfg.discord_sender_name == "username"
    by_mesh = {m.mesh_channel: m.sender_name for m in cfg.channels}
    assert by_mesh == {"Public": None, "#test": "none", "SARG": "display_name"}


def test_per_channel_sender_name_validated(tmp_path):
    path = cfg_file(
        tmp_path,
        discord_autoprovision=True,
        channels=[{"mesh_channel": "Public", "sender_name": "nickname"}],
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "sender_name" in str(exc.value)
