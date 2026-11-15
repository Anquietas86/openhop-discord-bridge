<picture>
  <source media="(prefers-color-scheme: dark)" srcset="branding/lockup-dark.svg">
  <img src="branding/lockup-light.svg" alt="openHop Discord Bridge — two-way MeshCore ↔ Discord bridge" width="620">
</picture>

# openHop Discord Bridge

Two-way bridge between **MeshCore channels** and **Discord channels**, packaged as
an [openHop Repeater](https://github.com/openhop) plugin.

Mesh senders appear in Discord as themselves (posted through a webhook, not the
bot account). Discord messages in a mapped channel are transmitted onto the mesh
through the bridge's **own dedicated companion identity**, with an explicit
allowlist deciding who may transmit.

```
 mesh channel ──► repeater.db packets (type=5, AES-128-ECB)
                        │  tail by id, decrypt per channel secret
                        ▼
                 bridge process ──────────────► Discord webhook
                 (on the repeater host)  username = mesh sender
                        ▲                             │
                        │ CMD_SEND_CHANNEL_TXT_MSG    │ message in mapped channel
                        └─────────────────────────────┘
                   companion TCP :5003          (bot ignores its own webhooks)
```

## Why a dedicated companion

The Repeater's REST API has no channel-chat endpoint and no way to send a group
text. Channel history has to come out of the node's SQLite `packets` table
(`type=5` GRP_TXT, decryptable with the channel secret), and transmitting has to
ride the companion protocol. Sharing an existing companion (`Hermes`, `Home
Assistant`, a base station) splits acks and events between two clients and makes
attribution ambiguous, so the bridge gets its own identity, its own port and its
own channel list.

## How it works

| Direction | Mechanism |
| --- | --- |
| mesh -> Discord | Read-only `packets` tail (`mode=ro`), HMAC-validated AES-128-ECB decrypt, dedupe on `(device timestamp, text)`, post via webhook with `username` = sender |
| Discord -> mesh | `CMD_SEND_CHANNEL_TXT_MSG` over the companion TCP socket, prefixed with the author, truncated to the mesh byte budget |

Channel provisioning rides `CMD_SET_CHANNEL`, which the node persists itself — the
bridge never edits `config.json` or the database, so **no repeater restart** is
needed to add or re-index its channels.

### Loop and abuse guards

- Webhook posts are ignored on intake (`webhook_id` set), so mesh traffic cannot
  bounce back through Discord.
- Every transmitted payload is remembered for `echo_ttl_seconds`, so the bridge's
  own transmission arriving back through the packet log is dropped.
- `(device_timestamp, text)` dedupe: each group text is logged once per hop,
  otherwise every message would appear ~4x.
- A per-channel token bucket throttles Discord -> mesh: a Discord argument must
  not become a sustained LoRa flood.
- `allowed_user_ids` / `allowed_role_ids` gate transmission. **Empty means
  nobody** — bridging a Discord channel onto RF is never an implicit grant.

## Reaction vocabulary

The bridge reacts to the Discord message it came from, so transmission state is
visible without reading logs:

| Reaction | Meaning |
| --- | --- |
| 📡 | queued for transmission |
| ✅ | transmitted — the node accepted it, it is on air |
| 📻 | heard back on the mesh — the relay carried it (delivery receipt) |
| ❌ | transmission failed |
| ⛔ | refused — sender is not on that channel's allowlist |
| ⏳ | rate limited (token bucket empty) |

`✅` is applied after the companion accepts the frame, not when the message is
queued, so it means on-air rather than accepted-by-the-bridge. `📻` is derived
from the bridge hearing its own transmission return through the packet log.

## Install

```bash
# On the repeater host, create the companion identity first (its own port):
#   POST /api/create_identity  {"name":"Discord Bridge","type":"companion",
#     "settings":{"node_name":"Discord Bridge","tcp_port":5003,"bind_address":"127.0.0.1"}}

python -m build --wheel
# then install the wheel through the plugin manager:
#   POST /api/plugins/install {"wheel_path":"/tmp/openhop_discord_bridge-0.1.0-py3-none-any.whl"}
#   POST /api/plugins/settings {"id":"discord.bridge","config":{...}}
#   POST /api/plugins/enable   {"id":"discord.bridge"}
```

A fresh companion starts with no channels; the bridge provisions them from the
repeater database at startup, so only the identity needs creating by hand.

## Configuration

`$OPENHOP_PLUGIN_DATA/config.json`, mode `0600`. The bot token may instead come
from `DISCORD_BOT_TOKEN`, which keeps it out of any file you might share.

### Top-level fields

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `discord_token` | string | (empty) | Bot token from Developer Portal > Bot > Reset Token. **Two dots, ~70+ chars.** `DISCORD_BOT_TOKEN` overrides it; keep the file `0600`. |
| `companion_host` | string | `"127.0.0.1"` | Host of the companion frame server. Loopback by default because the bridge runs on the repeater host. |
| `companion_port` | int | `5003` | Port of the bridge's **own** companion. Never point this at a shared companion (Hermes, Home Assistant, a base station). |
| `companion_app_name` | string | `"discord-bridge"` | Name sent in the `APP_START` handshake; shows up in the repeater log. |
| `repeater_db_path` | string | `"/var/lib/openhop_repeater/repeater.db"` | Repeater SQLite database, opened `mode=ro`. Group text is read from the `packets` table; there is no chat API. |
| `channels` | list | (empty) | Channel mappings, one object per mesh channel — see the mapping table below. |
| `poll_interval_seconds` | float | `2.0` | How often the packet table is polled. |
| `max_mesh_bytes` | int | `128` | Channel payload budget (128 for MeshCore group text). `companion_node_name` length + 2 is subtracted automatically, because the node prefixes its own name to every message. |
| `mesh_prefix_template` | string | `"{author}: "` | Template for the speaker label, `{author}` is substituted. Costs payload bytes on every transmission. |
| `webhook_name` | string | `"Mesh Bridge"` | Webhook created per Discord channel. Mesh senders appear as themselves via this webhook's `username`, not as the bot. |
| `rate_capacity` | int | `3` | Token-bucket burst per channel: how many Discord messages can go out back-to-back. |
| `rate_refill_seconds` | float | `12.0` | Seconds per token refill, per channel. |
| `echo_ttl_seconds` | float | `120` | How long a transmitted payload is remembered, so the bridge's own message coming back through the packet log is suppressed rather than reposted. |
| `start_from_latest` | bool | `true` | `true` ignores history on first start (no backfill into Discord); `false` replays the existing packet table. |
| `log_level` | string | `"INFO"` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `companion_node_name` | string | `"Discord Bridge"` | Must match the companion's `node_name` in the repeater config. Used for the byte-budget overhead and as the prefix stripped when detecting echoes. |
| `discord_autoprovision` | bool | `false` | Create the category and a channel per mapping on start. Needs **Manage Channels**. |
| `discord_category_name` | string | `"\ud83d\udcac | Meshcore"` | Category to create or reuse for the bridge's channels. |
| `discord_channel_prefix` | string | (empty) | Prefix for channel names derived from mesh channel names (`mesh-` gives `mesh-public`). |
| `discord_guild_id` | string | (empty) | Guild id. Null means "the only guild the bot is in". Quote it — see the snowflake note below. |
| `link_check_seconds` | float | `15.0` | Companion health check. After a repeater restart the link is half-closed while inbound keeps working, so this reconnects and re-provisions. |
| `discord_sender_name` | string | `"display_name"` | `display_name` (what people see — "Josh Hagger"), `username` (the handle — "anquietas86"), or `none` (no speaker at all; the node prefix still identifies the bridge). |
| `discord_sender_name_max_chars` | int | `24` | Cap on that label, since it spends the byte budget. |

### Channel mapping (`channels[]` entries)

| Key | Type | Required | Notes |
| --- | --- | --- | --- |
| `mesh_channel` | string | yes | MeshCore channel name, e.g. `Public` or `#hamradio`. |
| `discord_channel_id` | string | unless autoprovision | Discord channel id. **Quote it as a string** — see below. |
| `discord_channel_name` | string | no | Explicit Discord name when auto-provisioning; defaults to the mesh name. |
| `mesh_to_discord` | bool | no (`true`) | Mirror mesh traffic into Discord. |
| `discord_to_mesh` | bool | no (`false`) | Transmit Discord messages onto the mesh. |
| `allowed_user_ids` | list[string] | no | Who may transmit. **Empty means nobody** — enabling `discord_to_mesh` alone transmits nothing. |
| `allowed_role_ids` | list[string] | no | Role-based alternative to naming users. |
| `prefix_template` | string | no | Per-channel label template overriding `mesh_prefix_template`. |
| `sender_name` | string | no | Per-channel override of `discord_sender_name` (`display_name`, `username`, `none`). `discord_sender_name` is accepted as an alias. |

> **Quote every id.** Discord snowflakes are ~1.57e18 and exceed float64
> precision. An openHop settings save round-trips this JSON through JavaScript,
> which rewrites `1571423335894749224` as `1571423335894749200` — silently
> breaking every mapping. Stored as strings they cannot be damaged. Existing
> integer ids are converted on the next start.

Minimal example:

```json
{
  "discord_token": "",
  "companion_port": 5003,
  "channels": [
    {
      "mesh_channel": "Public",
      "discord_channel_id": "1571423335894749224",
      "mesh_to_discord": true,
      "discord_to_mesh": false
    }
  ]
}
```

Unmapped channels are ignored in both directions. `mesh_to_discord` and
`discord_to_mesh` are independent, so a channel can be mirrored read-only.

## Discord setup

1. New application + bot (**not** an existing assistant bot), enable the
   **Message Content** intent.
2. Invite it with View Channels, Read Message History, Manage Webhooks.
3. Map each mesh channel to a Discord channel by ID.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## Limitations

- Group text carries no sender public key; the `Name: body` prefix is
  self-reported, so mirrored messages are unauthenticated.
- Mesh has no attachments, edits or reactions — Discord-only concepts are dropped
  (an attachment-only message becomes `[attachment]`).
- A message longer than the byte budget is truncated on a word boundary with `…`.

## Branding

| File | Use |
| --- | --- |
| `branding/logo.svg` | primary mark — vector, works on light and dark |
| `branding/logo-512.png` / `-128.png` / `-64.png` | Discord avatar, plugin icon, favicon |
| `branding/lockup-light.svg` / `lockup-dark.svg` | README header, light and dark themes |

The mark is a chat bubble carrying a mesh constellation: the silhouette reads
as Discord at any size, the nodes read as a mesh. Ink bounding box is centred to
within a pixel of the canvas, and nothing in the mark depends on the background
colour, so it survives both themes.


## Licence

MIT
