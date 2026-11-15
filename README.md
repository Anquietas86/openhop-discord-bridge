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

## Discord setup

Do this first — the plugin cannot start without a bot token and the intent.

1. **New application** at <https://discord.com/developers/applications> — a
   dedicated one, not an assistant bot you already use. The bridge posts as
   webhooks, but it logs in as this bot.
2. **Bot** tab → copy the token. It is shown once; a valid token has **two dots**
   and is ~70+ characters. An openHop API key (64 hex chars, no dots) is not a
   token and fails with a bare `401`.
3. Same tab → **Privileged Gateway Intents** → enable **Message Content
   Intent**. Without it the gateway refuses the connection.
4. **Installation** tab → Guild Install → scopes must include **`bot`**, with
   permissions `536939584` (View Channels, Read Message History, Manage
   Webhooks, Add Reactions). Apps default to `applications.commands` only, in
   which case the install says "added successfully" while adding **no bot user**
   at all.
5. Invite it to your server. It only appears in the member list once a channel
   grants it View Channel.
6. Note the **channel IDs** you want to bridge — Settings → Advanced →
   Developer Mode, then right-click a channel → Copy Channel ID. They also show
   up in the API and can be quoted as strings (see the snowflake warning below).

## Install

Everything below happens in the openHop **web dashboard** — no shell needed.

### 1. Create the bridge's companion identity

**Companions** page → add a companion:

| Field | Value |
| --- | --- |
| Node Name | `VK5JPH Discord 🤖` — this is what appears on the mesh |
| TCP Port | `5003` (any free port; must be unique per companion) |
| Bind Address | `127.0.0.1` — the bridge runs on the repeater host, so loopback is enough |

**Do not reuse an existing companion.** A companion has one client; sharing one
with Hermes, Home Assistant or a base station splits its traffic and makes
attribution ambiguous. Companions do not accept client logins, so `max_clients`
does not limit how many you can have.

A new companion starts with **no channels**. The bridge provisions them from the
repeater database on first start — only the identity needs creating by hand.

### 2. Install the plugin wheel

1. Download `openhop_discord_bridge-<version>-py3-none-any.whl` from
   [Releases](https://github.com/Anquietas86/openhop-discord-bridge/releases).
2. **Plugins** page → **Install wheel** → select the `.whl`. Only `.whl` files are
   accepted; a `.tar.gz` or a checkout will be refused.
3. Open the plugin and paste your configuration into **Settings** (see
   [Configuration](#configuration)), then **Enable** it.

### 3. Verify

The plugin log should show, in order:

```
companion 127.0.0.1:5003 ready
provisioned channel Public at index 0        # ... one line per channel
Discord connected as <bot name>#1234
packet tail starting at id <n>
```

If it stops at the first line, the companion port or bind address is wrong. If
it never reaches `Discord connected`, the token or the Message Content intent is
the problem — the log says which.

### Command line / API alternative

For automation or a rebuild script, the same steps without the UI:

```bash
# 1. companion identity (hot-activates, no repeater restart)
curl -s -X POST http://127.0.0.1:8000/api/create_identity \
  -H "X-API-Key: $OPENHOP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"Discord Bridge","type":"companion",
       "settings":{"node_name":"VK5JPH Discord 🤖","tcp_port":5003,"bind_address":"127.0.0.1"}}'

# 2. install + configure + enable
curl -s -X POST http://127.0.0.1:8000/api/plugins/install \
  -H "X-API-Key: $OPENHOP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"wheel_path":"/tmp/openhop_discord_bridge-0.6.0-py3-none-any.whl"}'
curl -s -X POST http://127.0.0.1:8000/api/plugins/settings \
  -H "X-API-Key: $OPENHOP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"id":"discord.bridge","restart":true,"config":{ }}'
curl -s -X POST http://127.0.0.1:8000/api/plugins/enable \
  -H "X-API-Key: $OPENHOP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"id":"discord.bridge"}'
```

The API key is on the **Configuration** page of the repeater dashboard. Note the
manager returns HTTP 200 with `{"success": false}` in the body when a call is
refused, so check the JSON field and not just the status code.

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
