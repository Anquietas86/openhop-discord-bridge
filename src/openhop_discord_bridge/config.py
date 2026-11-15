"""Configuration for the Discord bridge plugin.

Everything comes from the plugin's own ``config.json`` (``$OPENHOP_PLUGIN_DATA``),
with two environment overrides so the bot token never has to live in a file that
gets pasted into a chat window: ``DISCORD_BOT_TOKEN`` and ``BRIDGE_CONFIG_PATH``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_DEFAULTS: Dict[str, Any] = {
    "discord_token": "",
    "companion_host": "127.0.0.1",
    "companion_port": 5003,
    "companion_app_name": "discord-bridge",
    "repeater_db_path": "/var/lib/openhop_repeater/repeater.db",
    "channels": [],
    "poll_interval_seconds": 2.0,
    "link_check_seconds": 15.0,
    "max_mesh_bytes": 128,
    "mesh_prefix_template": "{author}: ",
    "discord_sender_name": "display_name",
    "discord_sender_name_max_chars": 24,
    "companion_node_name": "Discord Bridge",
    "webhook_name": "Mesh Bridge",
    "rate_capacity": 3,
    "inbound_burst": 12,
    "inbound_refill_seconds": 2.0,
    "max_concurrent_posts": 8,
    "rate_refill_seconds": 12.0,
    "echo_ttl_seconds": 120,
    "tx_retries": 3,
    "escape_markdown": True,
    "mesh_sender_marker": "",
    "start_from_latest": True,
    "log_level": "INFO",
    "discord_autoprovision": False,
    "discord_category_name": "\U0001f4ac | Meshcore",
    "discord_channel_prefix": "",
    "discord_guild_id": None,
}


SENDER_MODES = ("display_name", "username", "none")


def _as_bool(value, field: str, default: bool) -> bool:
    """Strict boolean.

    ``bool("false")`` is True, so a config round-tripped through a UI that
    stringifies booleans would silently ENABLE transmission on a channel the
    operator believed was disabled. Only real booleans (or literal "true"/"false")
    are accepted.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    raise ConfigError(f"{field} must be true or false, not {value!r}")


def _as_int(value, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be an integer, not {value!r}") from None


def _as_id_list(value, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{field} must be a list of ids, not {type(value).__name__}")
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            raise ConfigError(f"{field} contains a non-numeric id: {item!r}") from None
    return out


def _sender_mode(value) -> Optional[str]:
    """Validate a sender-label mode, or None when unset."""
    if value is None or value == "":
        return None
    mode = str(value).lower()
    if mode not in SENDER_MODES:
        raise ConfigError(
            "sender_name must be one of "
            + ", ".join(repr(m) for m in SENDER_MODES)
            + f", not {mode!r}"
        )
    return mode


def normalise_channel_name(name: str) -> str:
    """Discord channel name for a mesh channel: lowercase, spaces to dashes.

    Emoji and other symbols are PRESERVED. Discord accepts them in channel names
    (``#🛰-sarg`` is valid), and stripping them silently geed the mapping of a
    channel someone had deliberately named with an emoji. Only characters Discord
    rejects are dropped: punctuation, and anything that is not a letter, digit,
    dash, underscore, symbol or combining mark.
    """
    import unicodedata

    cleaned = name.strip().lstrip("#").strip().casefold()
    out = []
    for ch in cleaned:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch.isspace() or ch in "./\\":
            out.append("-")
        else:
            # Emoji live in the Symbol categories; variation selectors and
            # combining marks are Marks. Keep both so sequences stay intact.
            category = unicodedata.category(ch)
            if category.startswith(("S", "M")):
                out.append(ch)
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:100] or "mesh"


class ConfigError(Exception):
    """Raised when the bridge is misconfigured in a way that cannot be papered over."""


@dataclass
class ChannelMapping:
    """One mesh channel paired with one Discord channel."""

    mesh_channel: str
    discord_channel_id: Optional[int] = None
    mesh_to_discord: bool = True
    discord_to_mesh: bool = True
    allowed_role_ids: List[int] = field(default_factory=list)
    allowed_user_ids: List[int] = field(default_factory=list)
    prefix_template: Optional[str] = None
    discord_channel_name: Optional[str] = None
    sender_name: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ChannelMapping":
        if not isinstance(raw, dict):
            raise ConfigError(f"channel mapping must be an object, got {type(raw).__name__}")
        mesh = str(raw.get("mesh_channel") or "").strip()
        if not mesh:
            raise ConfigError("channel mapping is missing 'mesh_channel'")
        cid = raw.get("discord_channel_id")
        if cid is None or (isinstance(cid, str) and not cid.strip()):
            # Absent, null, or blank all mean "not resolved yet": autoprovisioning
            # fills it in by name. A settings form that renders an empty string
            # would otherwise be rejected as non-numeric.
            cid = None
        else:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                raise ConfigError(
                    f"channel mapping for {mesh!r} has a non-numeric 'discord_channel_id'"
                ) from None
            if cid <= 0:
                raise ConfigError(f"channel mapping for {mesh!r} has an invalid discord_channel_id")
        return cls(
            mesh_channel=mesh,
            discord_channel_id=cid,
            mesh_to_discord=_as_bool(raw.get("mesh_to_discord"), f"{mesh!r}.mesh_to_discord", True),
            discord_to_mesh=_as_bool(raw.get("discord_to_mesh"), f"{mesh!r}.discord_to_mesh", True),
            allowed_role_ids=_as_id_list(raw.get("allowed_role_ids"), f"{mesh!r}.allowed_role_ids"),
            allowed_user_ids=_as_id_list(raw.get("allowed_user_ids"), f"{mesh!r}.allowed_user_ids"),
            prefix_template=raw.get("prefix_template"),
            discord_channel_name=raw.get("discord_channel_name"),
            sender_name=_sender_mode(raw.get("sender_name") or raw.get("discord_sender_name")),
        )

    def resolved_channel_name(self, prefix: str = "") -> str:
        """Discord channel name for this mapping, if one has to be created."""
        if self.discord_channel_name:
            return normalise_channel_name(prefix + self.discord_channel_name)
        return normalise_channel_name(prefix + self.mesh_channel)

    def permits(self, author_id: int, role_ids: List[int]) -> bool:
        """Whether a Discord author may transmit on this mapping.

        An empty allowlist means "nobody": bridging Discord into RF needs an
        explicit grant, never an implicit one.
        """
        if author_id in self.allowed_user_ids:
            return True
        return any(rid in self.allowed_role_ids for rid in role_ids)


@dataclass
class Config:
    discord_token: str
    companion_host: str
    companion_port: int
    companion_app_name: str
    repeater_db_path: str
    channels: List[ChannelMapping]
    poll_interval_seconds: float
    link_check_seconds: float
    max_mesh_bytes: int
    mesh_prefix_template: str
    discord_sender_name: str
    discord_sender_name_max_chars: int
    companion_node_name: str
    webhook_name: str
    rate_capacity: int
    inbound_burst: int
    inbound_refill_seconds: float
    max_concurrent_posts: int
    rate_refill_seconds: float
    echo_ttl_seconds: float
    tx_retries: int
    escape_markdown: bool
    mesh_sender_marker: str
    start_from_latest: bool
    log_level: str
    discord_autoprovision: bool = False
    discord_category_name: str = "\U0001f4ac | Meshcore"
    discord_channel_prefix: str = ""
    discord_guild_id: Optional[int] = None

    def mapping_for_mesh(self, name: str) -> Optional[ChannelMapping]:
        for m in self.channels:
            if m.mesh_channel == name:
                return m
        return None

    def mapping_for_discord(self, channel_id: int) -> Optional[ChannelMapping]:
        for m in self.channels:
            if m.discord_channel_id == channel_id:
                return m
        return None

    @property
    def mesh_channel_names(self) -> List[str]:
        return [m.mesh_channel for m in self.channels]


def _data_dir() -> Path:
    env = os.environ.get("OPENHOP_PLUGIN_DATA")
    if env:
        return Path(env)
    return Path(os.environ.get("BRIDGE_STATE_DIR", "."))


def config_path() -> Path:
    override = os.environ.get("BRIDGE_CONFIG_PATH")
    if override:
        return Path(override)
    return _data_dir() / "config.json"


def load_config(path: Optional[Path] = None) -> Config:
    path = path or config_path()
    raw: Dict[str, Any] = dict(MANIFEST_DEFAULTS)
    if path.exists():
        try:
            file_data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        raw.update(file_data)

    token = os.environ.get("DISCORD_BOT_TOKEN") or raw.get("discord_token") or ""
    if not token:
        raise ConfigError(
            "no Discord bot token: set 'discord_token' in config.json or DISCORD_BOT_TOKEN"
        )

    # A bot token is three base64url segments separated by two dots. Catching the
    # wrong kind of secret here turns a bare 401 at login into an actionable
    # message: openHop API keys and most other secrets are dotless 64-char hex.
    token = token.strip()
    if token.count(".") != 2 or len(token) < 59:
        shape = "64-char hex (looks like an openHop API key)" if (
            len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token)
        ) else f"{len(token)} chars with {token.count('.')} dot(s)"
        raise ConfigError(
            "discord_token does not look like a bot token: it is "
            f"{shape}. A bot token has two dots and is ~70+ characters — copy it "
            "from Developer Portal > your app > Bot > Reset Token, not the "
            "application ID, client secret or a repeater API key."
        )

    mappings = [ChannelMapping.from_dict(item) for item in raw.get("channels") or []]
    if not mappings:
        raise ConfigError("no channel mappings: 'channels' is empty, so there is nothing to bridge")

    # MUST go through _as_bool: bool("false") is True, and this value gates
    # validation. Parsed differently from the Config field, a stringified flag
    # would let a mapping with no discord_channel_id pass validation while
    # runtime autoprovision was False — the bridge would then silently drop
    # every mesh message, having "validated" the config.
    autoprovision = _as_bool(raw.get("discord_autoprovision"), "discord_autoprovision", False)
    for m in mappings:
        if m.discord_channel_id is None and not autoprovision:
            raise ConfigError(
                f"channel mapping for {m.mesh_channel!r} has no discord_channel_id "
                "and discord_autoprovision is off"
            )

    seen_discord = set()
    seen_mesh = set()
    for m in mappings:
        if m.discord_channel_id is not None and m.discord_channel_id in seen_discord:
            raise ConfigError(f"Discord channel {m.discord_channel_id} is mapped twice")
        if m.mesh_channel in seen_mesh:
            raise ConfigError(f"mesh channel {m.mesh_channel!r} is mapped twice")
        if m.discord_channel_id is not None:
            seen_discord.add(m.discord_channel_id)
        seen_mesh.add(m.mesh_channel)

    sender_mode = _sender_mode(raw.get("discord_sender_name") or "display_name") or "display_name"

    port = _as_int(raw.get("companion_port"), "companion_port", 5003)
    if not 1 <= port <= 65535:
        raise ConfigError(f"companion_port must be 1-65535, not {port}")
    host = str(raw.get("companion_host") or "").strip()
    if not host:
        raise ConfigError("companion_host must not be empty")

    max_bytes = int(raw.get("max_mesh_bytes") or 128)
    if not 16 <= max_bytes <= 200:
        raise ConfigError("max_mesh_bytes must be between 16 and 200")

    return Config(
        discord_token=token,
        companion_host=host,
        companion_port=port,
        companion_app_name=str(raw.get("companion_app_name") or "discord-bridge"),
        repeater_db_path=str(
            raw.get("repeater_db_path") or "/var/lib/openhop_repeater/repeater.db"
        ),
        channels=mappings,
        poll_interval_seconds=max(0.5, float(raw.get("poll_interval_seconds") or 2.0)),
        link_check_seconds=max(5.0, float(raw.get("link_check_seconds") or 15.0)),
        max_mesh_bytes=max_bytes,
        mesh_prefix_template=str(raw.get("mesh_prefix_template") or "{author}: "),
        discord_sender_name=sender_mode,
        discord_sender_name_max_chars=max(4, int(raw.get("discord_sender_name_max_chars") or 24)),
        companion_node_name=str(raw.get("companion_node_name") or "Discord Bridge"),
        webhook_name=str(raw.get("webhook_name") or "Mesh Bridge"),
        rate_capacity=max(1, int(raw.get("rate_capacity") or 3)),
        inbound_burst=max(1, int(raw.get("inbound_burst") or 12)),
        inbound_refill_seconds=max(0.0, float(raw.get("inbound_refill_seconds") or 2.0)),
        max_concurrent_posts=max(1, int(raw.get("max_concurrent_posts") or 8)),
        rate_refill_seconds=max(0.0, float(raw.get("rate_refill_seconds") or 12.0)),
        echo_ttl_seconds=max(0.0, float(raw.get("echo_ttl_seconds") or 120)),
        tx_retries=max(0, _as_int(raw.get("tx_retries"), "tx_retries", 3)),
        escape_markdown=_as_bool(raw.get("escape_markdown"), "escape_markdown", True),
        mesh_sender_marker=str(raw.get("mesh_sender_marker") or ""),
        start_from_latest=_as_bool(raw.get("start_from_latest"), "start_from_latest", True),
        log_level=str(raw.get("log_level") or "INFO").upper(),
        discord_autoprovision=autoprovision,
        discord_category_name=str(raw.get("discord_category_name") or "\U0001f4ac | Meshcore"),
        discord_channel_prefix=str(raw.get("discord_channel_prefix") or ""),
        discord_guild_id=(
            _as_int(raw.get("discord_guild_id"), "discord_guild_id", 0) or None
        ),
    )
