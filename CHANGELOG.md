# 0.2.0

- **Auto-provisioning** (`discord_autoprovision`): on first ready the bot ensures
  `discord_category_name` (default `💬 | Meshcore`) exists, creates one
  text channel per mapping under it, grants itself View Channel / Read History /
  Manage Webhooks / Send Messages on the category, and writes the resolved ids
  back into `config.json` so later starts reuse them.
- Mappings may omit `discord_channel_id` when auto-provisioning is on; the name
  is derived from the mesh channel (`#hamradio` -> `hamradio`,
  `Adelaide Chat` -> `adelaide-chat`) with an optional `discord_channel_prefix`.
- Requires **Manage Channels** on the bot's role. Without it provisioning logs a
  clear failure and the bridge keeps running with the ids it already has.
- The packet tailer now starts only after Discord is ready, so mappings exist
  before any message is routed.
- Tests: 55 passing.

# 0.1.3

- Fix: the repeater prepends `<node_name>: ` to every channel message it
  transmits, so the bridge was double-prefixing (`Discord Bridge: [Josh] hi`) and
  overrunning the channel byte budget by `len(node_name) + 2`. The outgoing
  budget now subtracts that overhead (`mesh_text_budget`), and the default prefix
  is `{author}: ` instead of `[{author}] `.
- New config `companion_node_name` (default `Discord Bridge`) feeds that maths —
  set it to match the companion's `node_name` if you rename it.
- Tests: 41 passing.

# 0.1.2

- Fix: `Client.connect()` is keyword-only and takes no token on discord.py 2.7,
  so passing the token raised `TypeError` after the companion link was already
  up. Login now goes through `Client.start(token)`.
- Disabled Message Content intent is reported as an actionable error naming the
  portal path, with exit code 3, instead of discord.py's raw gateway text.
- Tests: 39 passing; a signature test pins the connect/start contract.

# 0.1.1

- Fix: `Intents.default() | Intents.message_content` raised
  `AttributeError: 'flag_value' object has no attribute 'value'` on discord.py
  2.7. Class-level access to a flag is a descriptor, not an `Intents`; the
  intent is now set on an instance (`build_intents()`).
- Fix: a dotless 64-char hex secret in `discord_token` failed only as a bare
  `401` at login. Token shape is now validated at startup and the error names
  the usual mix-up (openHop API key instead of bot token).
- Tests: 38 passing, including regressions for both faults.

# 0.1.0

- Initial release.
- Two-way bridging between configured MeshCore channels and Discord channels.
- Dedicated companion identity with self-provisioning channels over
  `CMD_SET_CHANNEL` (persisted by the node; no repeater restart).
- Read-only `packets` tail with HMAC-validated AES-128-ECB decryption.
- Loop guards: webhook intake filtering, outgoing echo memory, per-hop dedupe.
- Per-channel token bucket and explicit Discord user/role TX allowlist.
- Byte-budget truncation with word-boundary preference.
