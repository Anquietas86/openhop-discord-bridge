"""Generate the README configuration table from the manifest defaults.

The table is written from `openhop-plugin.json`, so a key cannot be documented
with the wrong default, and a test asserts every key appears in the README.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
manifest = json.loads((ROOT / "openhop-plugin.json").read_text())["config"]["defaults"]

DOCS = {
    "discord_token": (
        "string",
        "Bot token from Developer Portal > Bot > Reset Token. **Two dots, ~70+ chars.** "
        "`DISCORD_BOT_TOKEN` overrides it; keep the file `0600`.",
    ),
    "companion_host": (
        "string",
        "Host of the companion frame server. Loopback by default because the bridge "
        "runs on the repeater host.",
    ),
    "companion_port": (
        "int",
        "Port of the bridge's **own** companion. Never point this at a shared "
        "companion (Hermes, Home Assistant, a base station).",
    ),
    "companion_app_name": (
        "string",
        "Name sent in the `APP_START` handshake; shows up in the repeater log.",
    ),
    "repeater_db_path": (
        "string",
        "Repeater SQLite database, opened `mode=ro`. Group text is read from the "
        "`packets` table; there is no chat API.",
    ),
    "channels": (
        "list",
        "Channel mappings, one object per mesh channel — see the mapping table below.",
    ),
    "poll_interval_seconds": ("float", "How often the packet table is polled."),
    "link_check_seconds": (
        "float",
        "Companion health check. After a repeater restart the link is "
        "half-closed while inbound keeps working, so this reconnects and "
        "re-provisions.",
    ),
    "max_mesh_bytes": (
        "int",
        "Channel payload budget (128 for MeshCore group text). `companion_node_name` "
        "length + 2 is subtracted automatically, because the node prefixes its own "
        "name to every message.",
    ),
    "mesh_prefix_template": (
        "string",
        "Template for the speaker label, `{author}` is substituted. Costs payload "
        "bytes on every transmission.",
    ),
    "discord_sender_name": (
        "string",
        "`display_name` (what people see — \"Alex Smith\"), `username` (the "
        "handle — \"alexsmith\"), or `none` (no speaker at all; the node prefix "
        "still identifies the bridge).",
    ),
    "discord_sender_name_max_chars": (
        "int",
        "Cap on that label, since it spends the byte budget.",
    ),
    "companion_node_name": (
        "string",
        "Must match the companion's `node_name` in the repeater config. Used for the "
        "byte-budget overhead and as the prefix stripped when detecting echoes.",
    ),
    "webhook_name": (
        "string",
        "Webhook created per Discord channel. Mesh senders appear as themselves via "
        "this webhook's `username`, not as the bot.",
    ),
    "rate_capacity": ("int", "Token-bucket burst per channel: how many Discord messages can go out back-to-back."),
    "rate_refill_seconds": ("float", "Seconds per token refill, per channel."),
    "inbound_burst": (
        "int",
        "Mesh -> Discord burst per channel. A mesh flood must not become a Discord "
        "flood; Discord rate-limits webhooks at about 5 posts per 2s.",
    ),
    "inbound_refill_seconds": ("float", "Seconds per token refill for inbound traffic, per channel."),
    "tx_retries": (
        "int",
        "Retries for a failed transmission before the message is abandoned, so a "
        "brief repeater restart does not silently eat a Discord message.",
    ),
    "escape_markdown": (
        "bool",
        "Escape markdown in mesh text. Mesh input is attacker-controlled, so "
        "without this a node can render masked links and fake formatting.",
    ),
    "mesh_sender_marker": (
        "str",
        "Prefix added to webhook usernames (e.g. `[mesh] `) so a mesh node cannot "
        "pass itself off as a Discord member. Empty by default.",
    ),
    "max_concurrent_posts": (
        "int",
        "Cap on concurrent Discord posts, so a burst cannot spawn unlimited tasks.",
    ),
    "echo_ttl_seconds": (
        "float",
        "How long a transmitted payload is remembered, so the bridge's own message "
        "coming back through the packet log is suppressed rather than reposted.",
    ),
    "start_from_latest": (
        "bool",
        "`true` ignores history on first start (no backfill into Discord); `false` "
        "replays the existing packet table.",
    ),
    "log_level": ("string", "`DEBUG`, `INFO`, `WARNING` or `ERROR`."),
    "discord_autoprovision": (
        "bool",
        "Create the category and a channel per mapping on start. Needs **Manage "
        "Channels**.",
    ),
    "discord_category_name": (
        "string",
        "Category to create or reuse for the bridge's channels.",
    ),
    "discord_channel_prefix": (
        "string",
        "Prefix for channel names derived from mesh channel names "
        "(`mesh-` gives `mesh-public`).",
    ),
    "discord_guild_id": (
        "string",
        "Guild id. Null means \"the only guild the bot is in\". Quote it — see the "
        "snowflake note below.",
    ),
}

MAPPING_DOCS = [
    ("mesh_channel", "string", "yes", "MeshCore channel name, e.g. `Public` or `#hamradio`."),
    ("discord_channel_id", "string", "unless autoprovision", "Discord channel id. **Quote it as a string** — see below."),
    ("discord_channel_name", "string", "no", "Explicit Discord name when auto-provisioning; defaults to the mesh name."),
    ("mesh_to_discord", "bool", "no (`true`)", "Mirror mesh traffic into Discord."),
    ("discord_to_mesh", "bool", "no (`false`)", "Transmit Discord messages onto the mesh."),
    ("allowed_user_ids", "list[string]", "no", "Who may transmit. **Empty means nobody** — enabling `discord_to_mesh` alone transmits nothing."),
    ("allowed_role_ids", "list[string]", "no", "Role-based alternative to naming users."),
    ("prefix_template", "string", "no", "Per-channel label template overriding `mesh_prefix_template`."),
    ("sender_name", "string", "no", "Per-channel override of `discord_sender_name` (`display_name`, `username`, `none`). `discord_sender_name` is accepted as an alias."),
]

missing = [k for k in manifest if k not in DOCS]
assert not missing, f"undocumented keys: {missing}"

lines = ["| Key | Type | Default | Notes |", "| --- | --- | --- | --- |"]
for key in manifest:
    kind, note = DOCS[key]
    default = manifest[key]
    shown = "(empty)" if default in ("", [], None) else f"`{json.dumps(default, ensure_ascii=False)}`"
    lines.append(f"| `{key}` | {kind} | {shown} | {note} |")
table = "\n".join(lines)

mlines = ["| Key | Type | Required | Notes |", "| --- | --- | --- | --- |"]
for key, kind, req, note in MAPPING_DOCS:
    mlines.append(f"| `{key}` | {kind} | {req} | {note} |")
mapping_table = "\n".join(mlines)

section = f"""## Configuration

`$OPENHOP_PLUGIN_DATA/config.json`, mode `0600`. The bot token may instead come
from `DISCORD_BOT_TOKEN`, which keeps it out of any file you might share.

### Top-level fields

{table}

### Channel mapping (`channels[]` entries)

{mapping_table}

> **Quote every id.** Discord snowflakes are ~1.57e18 and exceed float64
> precision. An openHop settings save round-trips this JSON through JavaScript,
> which rewrites `1234567890123456789` as `1234567890123456768` — silently
> breaking every mapping. Stored as strings they cannot be damaged. Existing
> integer ids are converted on the next start.

Minimal example:

```json
{{
  "discord_token": "",
  "companion_port": 5003,
  "channels": [
    {{
      "mesh_channel": "Public",
      "discord_channel_id": "1234567890123456789",
      "mesh_to_discord": true,
      "discord_to_mesh": false
    }}
  ]
}}
```

Unmapped channels are ignored in both directions. `mesh_to_discord` and
`discord_to_mesh` are independent, so a channel can be mirrored read-only.
"""

readme = ROOT / "README.md"
text = readme.read_text()

BEGIN = "<!-- config-table:start (generated by scripts/gen_config_table.py) -->"
END = "<!-- config-table:end -->"
body = section.rstrip()

# Idempotent replacement. The previous version located `## Configuration` and
# `## Discord setup` independently; because Discord setup comes FIRST in the
# document, `text[end:]` re-included everything after it and each run appended
# another copy of the install/config block (five runs produced a 990-line README).
if BEGIN in text and END in text:
    pre = text[: text.index(BEGIN)]
    post = text[text.index(END) + len(END) :]
    text = pre + BEGIN + "\n\n" + body + "\n\n" + END + post
else:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == "## Configuration"]
    if not starts:
        raise SystemExit("no '## Configuration' heading to replace")
    start = starts[-1]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    text = (
        "".join(lines[:start])
        + BEGIN + "\n\n" + body + "\n\n" + END + "\n\n"
        + "".join(lines[end:])
    )

readme.write_text(text)
print(f"wrote table with {len(manifest)} fields and {len(MAPPING_DOCS)} mapping keys")
