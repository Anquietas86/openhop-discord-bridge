# 0.8.4 — generic placeholders in the docs

The published repo should read as a tool for anyone, not a write-up of one
installation.

- **README uses generic names**: `VK0ABC Discord` for the companion node,
  "Alex Smith" / "alexsmith" for the display-name and handle examples, and
  `1234567890123456789` for the snowflake in the precision warning (with the
  mangled form corrected to `...456768` — the previous text mixed two different
  ids).
- **Default `discord_category_name` is now `Mesh Bridge`** instead of a name
  specific to the author's server. Existing configs are unaffected: the value is
  stored in `config.json`, which wins over the manifest default.
- **Test fixtures and docstrings** no longer carry the author's callsign, name or
  local channel names, since the whole tree is public.
- **The config table renders non-ASCII defaults properly.** `json.dumps` escapes
  by default, so an emoji default appeared as a literal `\ud83d\udcac`.
- The generator now owns those placeholders, so a rebuild cannot reintroduce the
  personal examples.

# 0.8.3 — documentation for public users

The project was announced in the SA Mesh Users Discord, so the README matters as
much as the code from here.

- **The README's install and configuration sections were duplicated five times.**
  `scripts/gen_config_table.py` located `## Configuration` and `## Discord setup`
  independently; because Discord setup appears first in the document,
  `text[end:]` re-included everything after it and every run appended another copy
  of the whole block. A 990-line README became 309 lines, and the generator is now
  marker-based and idempotent. `tests/test_docs_idempotent.py` fails if a repeat run
  changes the file, or if any section appears more than once.
- **Adds a Troubleshooting section** built from failures that actually occurred:
  the Message Content Intent exit code, an openHop API key pasted where a bot token
  belongs, 403 on autoprovision, edits that need a disable/enable, transmit gated
  three ways, stale role ids, companion reconnects, WAL needing directory write
  access, snowflake precision, and webhook name rejections.
- **Adds `examples/config.example.json`.** Writing it exposed a real bug: a
  blank `discord_channel_id` (`""`, as a settings form renders it) was rejected as
  non-numeric instead of meaning "not resolved yet, autoprovision will fill it in".
  Absent, null and blank are now equivalent, and genuine junk is still rejected.
  The example is asserted against the loader in the test suite.

# 0.8.2 — close the last two review items

- **TX silently used the first channel secret.** Receive tries every candidate
  key found in the repeater DB, but transmit used `candidates[0]`. The same
  channel name exists on several companions; if their keys differ, one may be
  stale, and a transmission under a stale key fails invisibly (it is simply never
  echoed back). Now logs an error when candidates disagree, so the failure is
  visible instead of silent. Picking the key that verifies recent traffic needs a
  decode pass at startup — deliberately not done yet.
- **The rebuild check opened the database on every idle poll** (every 2s on a
  quiet mesh). Now runs about once a minute.

# 0.8.1 — second review pass

A second reviewer (a different vendor's frontier general model, given the first
pass's findings as exclusions) found four defects that survived the first round.

- **`discord_autoprovision` was parsed two different ways.** The validation gate
  used `bool(raw.get(...))` while the runtime field used the strict parser, so
  `"false"` (stringified by a settings UI) passed validation — accepting mappings
  with no `discord_channel_id` — while runtime autoprovision was off. The bridge
  then dropped every mesh message, having "validated" the config. This is the same
  defect class as 0.8.0's `bool("false")` fix, in a line the first pass missed.
- **`provision_channels` cleared the live channel map before rebuilding it.** Both
  the sender loop and the link supervisor read that map during a reconnect, so a
  message could see `idx is None` and be dropped. The map is now built privately
  and swapped in with a single assignment under the lifecycle lock.
- **A missing channel index stranded messages.** The queue entry was discarded with
  only a log line, leaving the user's message showing the queued tick forever.
  Now it re-provisions and retries the lookup once, then marks the message failed.
- **`FileNotFoundError` logged every poll interval** (~43k lines/day for a wrong
  path) while other failures were throttled. Now throttled identically.

The same reviewer explicitly examined the allowlist path and the AES/HMAC decode
path and found no bypass or exploit path, citing the specific guards — recorded
here because "reviewed and sound" is evidence too.

Tests: 129 -> 135 (`tests/test_second_review.py`).

# 0.8.0 — independent review findings

An external reviewer (a different model, given only the source and the threat
model) was asked to assume the code was buggy. It found 20 issues; all are fixed
here. Two were regressions I introduced in 0.7.0.

## Long-running correctness (highest impact)

- **Both background loops died permanently on `ConfigError`.** 0.7.0 made
  `_channel_keys()` raise `ConfigError` when the repeater DB is unreadable, but
  `_sender_loop` and `_link_supervisor` only caught `(CompanionError, OSError)`.
  One DB hiccup killed the task forever: RF transmission stopped while
  mesh -> Discord kept working, so the bridge looked healthy. Both loops now catch
  `Exception`, log, and continue.
- **`_amain` returned exit code 0 when the Discord client died**, so a supervisor
  with `Restart=on-failure` never restarted a dead bridge. Now returns 1.
- **Concurrent reconnects raced**: `CompanionClient.open()`/`close()` were
  unserialised and both the sender loop and the supervisor can reconnect. A second
  open orphaned the first read loop, whose `finally` then marked the *healthy* new
  link dead — permanent reconnect churn plus a leaked socket. Fixed with a
  lifecycle lock and a connection generation counter.
- **A zero-length companion frame raised `IndexError`** in the read loop and tore
  down a healthy link. `split_frames` now rejects it as a `CompanionError`.
- **The packet cursor never recovered** if the repeater DB was rebuilt, restored or
  truncated: `MAX(id)` drops below the cursor and the tail then returns nothing
  forever, silently. Now detects the backwards jump, resets and logs.
- **Failed transmissions were discarded.** A repeater restart ate the queued
  Discord message. Adds bounded retry with backoff (`tx_retries`, default 3).
- **The webhook cache was permanent**: a deleted webhook silenced that channel
  forever. Now invalidated on `NotFound`/`Forbidden` and re-created once.
- **`config.json` was written non-atomically** — a crash mid-write left truncated
  JSON and the plugin could never start again. Now temp file + `fsync` + `os.replace`.
- **One bad message aborted the rest of the batch** (cursor already advanced, so
  the remainder was lost); per-message isolation added, and repeated DB errors are
  rate-limited instead of logging every 2s.
- **`start()` aborted on a repeater that was down at boot.** It now logs and lets
  the supervisor reconnect, so mesh -> Discord works during a repeater outage.

## Security

- **`bool("false")` enabled transmission.** A config round-tripped through a UI
  that stringifies booleans silently turned `discord_to_mesh` on. Booleans are now
  parsed strictly and nonsense values raise `ConfigError`.
- **Mesh text rendered as live Markdown** with embeds enabled, so a mesh node could
  post masked links (`[click](https://evil)`) and attacker-chosen link previews.
  Now escaped (`escape_markdown`, default on) with `suppress_embeds=True`.
- **Webhook usernames were forgeable** — a mesh node could post as "Admin". Adds
  optional `mesh_sender_marker` (e.g. `[mesh] `) so the label is visibly non-Discord.
- **Unvalidated endpoint config**: `companion_port` (0/70000) and an empty
  `companion_host` now fail at load with a named error rather than at runtime.
- **Allowlist typos raised bare `ValueError`** with a traceback; now a `ConfigError`
  naming the field and value.
- **DB path was interpolated into a `file:` URI unescaped**, so a `?` or `#` in the
  path opened the wrong file.
- **`_post_mesh` caught only `discord.HTTPException`**, leaking other exceptions
  into a fire-and-forget task ("Task exception was never retrieved").

Tests: 105 -> 129, coverage 74%, `tests/test_review_findings.py` pins each finding.

# 0.7.0 — public-release hardening (code review)

Findings from a full review before publishing, and their fixes:

- **Latent crash in `BridgeDiscord.__init__`**: `intents or (...)` treated an
  empty `Intents()` as falsy and evaluated a fallback containing the broken
  `Intents.default() | Intents.message_content` expression. Unreachable from
  `server.py` (which always passes populated intents) but fatal for any other
  caller.
- **No inbound throttle** (DoS): a mesh flood became a Discord flood, and every
  message spawned an unbounded `asyncio` task. Adds per-channel
  `inbound_burst` / `inbound_refill_seconds` and `max_concurrent_posts`; drops are
  counted and logged at most once per 50.
- **Unbounded `_pending_echo`** (memory leak): entries were only removed when the
  mesh echoed the transmission back. Now a bounded, TTL'd `PendingEcho`.
- **Mis-split messages**: a body like `http://host:8080/x` was read as a sender
  named `http`; `"Someone:"` was posted as literal text instead of being
  recognised as empty. Splitting now requires an empty or space-led body and
  rejects URL-like heads; empty bodies are skipped instead of provoking a `400`.
- **`_react` ignored message-like objects** (`isinstance(discord.Message)`);
  now duck-typed, which also made the reaction paths testable.
- **Unclear failure when the repeater DB is missing**: now a `ConfigError`
  naming `repeater_db_path` and what the bridge needs from it.
- **`discord_sender_name` validation result was discarded** (ruff F841).
- **Testing**: 105 tests (was 78), coverage 58% -> 74%, with adversarial suites
  for allowlist bypass, `@everyone` in mesh text, floods, malformed packets and
  the intake guard. Protocol client exercised against a fake frame server.
- README gains a **Security model** section stating what is and is not
  guaranteed.

# 0.6.1

- **README install path is now the web dashboard**, matching how it is actually
  done: create the companion on the Companions page, upload the wheel with
  **Plugins -> Install wheel**, configure in Settings, enable. The CLI/API flow
  moved to an "alternative" subsection rather than being the default.
- **Discord app setup moved ahead of Install.** It was after the install steps
  although the token and Message Content intent are needed before the plugin can
  start at all.
- Adds the two install traps seen in practice: apps defaulting to
  `applications.commands` (installs "successfully" with no bot user), and
  `max_clients` not limiting companions.
- 78 tests passing.

# 0.6.0

- **Per-channel `sender_name`**: a mapping may override `discord_sender_name`
  (`display_name`, `username`, `none`) so one channel can send bare while others
  name the speaker. `discord_sender_name` is accepted as an alias inside a
  mapping. Unset means "use the global default".
- Both levels share one validator, so an invalid mode fails at startup with the
  legal values listed.
- README config tables regenerated (23 fields, 9 mapping keys).
- 78 tests passing.

# 0.5.0

- **`discord_sender_name: "none"`** — a third option alongside `display_name` and
  `username`. The body is sent bare, with no speaker prefix; the node's own name
  prefix (`<node_name>: `) still identifies the bridge, so the message remains
  attributable without naming the person. Saves the label's bytes too.
- 76 tests passing.

# 0.4.2

- **Derived Discord channel names keep emoji.** `normalise_channel_name` stripped
  every non-alphanumeric character, so a mesh channel named `🛰 SARG` became
  `#sarg` — silently losing what the operator had put there. Discord accepts
  emoji in channel names (verified: creating `🛰-emoji-probe` returns 201 with the
  name intact), so symbols and combining marks are now preserved:
  `🛰 SARG` -> `#🛰-sarg`, `WarMobile🏎️` -> `#warmobile🏎️`.
- Only characters Discord rejects are still dropped (punctuation, and anything
  that is not a letter, digit, dash, underscore, symbol or combining mark).
- 75 tests passing.

# 0.4.1

- **README: full configuration reference.** Two generated tables (top-level fields
  and `channels[]` entries) covering every key, its type, default and behaviour,
  produced from `openhop-plugin.json` by `scripts/gen_config_table.py`.
- **Fix: `MANIFEST_DEFAULTS` had drifted from the manifest.** Four
  auto-provisioning keys (`discord_autoprovision`, `discord_category_name`,
  `discord_channel_prefix`, `discord_guild_id`) were in the manifest but absent
  from the loader's defaults dict — behaviour was correct via fallbacks, but
  anything reading one and not the other saw different contracts.
- **Tests:** manifest/loader parity, and that every config key appears in the
  README. 70 passing.

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
