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
