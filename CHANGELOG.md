# 0.4.0

- **Reaction vocabulary** on the originating Discord message, so transmission
  state is visible without reading logs:

  | Reaction | Meaning |
  | --- | --- |
  | 📡 | queued for transmission |
  | ✅ | transmitted — the node accepted it |
  | 📻 | heard back on the mesh — the relay carried it |
  | ❌ | transmission failed |
  | ⛔ | refused (sender not on the channel's allowlist) |
  | ⏳ | rate limited |

  `✅` is applied by the sender loop *after* the node accepts the frame, not at
  queue time, so it means "on air" rather than "accepted by the bridge". `📻`
  comes from the echo the bridge already detects, reused as a delivery receipt.
- The outbound queue now carries a reference to the Discord message
  (`Outbound` dataclass), which is what makes late reactions possible.
- 68 tests passing.

# 0.3.2

- **Channel and user ids are stored as strings** in `config.json`. Discord
  snowflakes are ~1.57e18 and exceed float64 precision, so an openHop settings
  save (which round-trips the JSON through JavaScript) silently rewrites
  `...749224` as `...749200` — corrupting every mapping while the operator only
  changed an unrelated field. Quoted ids cannot lose precision in that path.
  Existing integer ids are converted on the next start and accepted on load.
- 67 tests passing.

# 0.3.1

- **Mappings are reconciled against Discord on every start**, not only when
  auto-provisioning. A stored id can be *wrong* rather than missing: Discord
  snowflakes exceed float64 precision, so any editor that parses JSON numbers as
  floats rewrites `...749224` as `...749200`. Such a mapping now logs a warning,
  re-resolves the channel by name, and persists the corrected id.
- A mapping whose stored id does not exist and whose name cannot be found is
  logged and left unmapped instead of silently posting to a void.
- `discord_channel_id` accepts a string as well as a number, so a JSON editor
  that quotes ids is harmless.
- 66 tests passing.

# 0.3.0

- **`discord_sender_name`**: label Discord speakers on the mesh by `display_name`
  (default, what people see — "Josh Hagger") or `username` (the handle —
  "anquietas86"). Capped by `discord_sender_name_max_chars` (default 24) because
  the label spends the mesh byte budget.
- 64 tests passing.

# 0.2.4

Two bugs found on the first real Discord -> mesh round trip:

- **Webhook username rejected**: Discord refuses a webhook `username` containing
  its own brand (`400 / 50035: Username cannot contain "discord"`). A companion
  node named `VK5JPH Discord` therefore failed *every* post. Names are now
  sanitised (`sanitise_webhook_username`), stripping the brand word and reserved
  words rather than dropping the message.
- **Echo guard never matched**: the node prepends `<node_name>: ` to what we send,
  so the round trip text was never equal to the remembered payload and the bridge
  tried to repost its own transmission. Comparison now also tests the text after
  that prefix, which also survives renaming the companion.
- 60 tests passing.

# 0.2.3

- **Companion link is now supervised.** After the repeater restarted, the plugin
  kept a half-closed handle in `CLOSE-WAIT`: inbound kept working because it reads
  the database, while every transmission would have failed. A periodic supervisor
  (`link_check_seconds`, default 15) reconnects and re-provisions the channels.
- `CompanionClient.connected` now keys off the read loop's EOF flag rather than
  `is_closing()`, because a half-closed socket reports `is_closing() == False`.
- 57 tests passing.

# 0.2.2

- Provisioned channels are created with **no permission overwrites**, inheriting
  the category. The previous behaviour stamped a member overwrite for the bot on
  each new channel, and a member overwrite on the child shadows the category's
  member overwrite instead of merging with it — so a scoped "Manage Channels on
  the category" grant stopped applying inside those channels, leaving channels the
  bot could create but never delete. The bot role already holds View Channel,
  Read Message History, Manage Webhooks and Send Messages, so inheritance is
  sufficient.
- 55 tests passing.

# 0.2.1

- Provisioning no longer aborts on the first refusal. A channel the bot cannot
  create (typical cause: Manage Channels not granted on the category) is logged
  with the exact category and mesh channel that stay unmapped, and the remaining
  channels are still created and persisted.
- 55 tests passing.

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
