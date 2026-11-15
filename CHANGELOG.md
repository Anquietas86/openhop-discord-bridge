# 0.1.0

- Initial release.
- Two-way bridging between configured MeshCore channels and Discord channels.
- Dedicated companion identity with self-provisioning channels over
  `CMD_SET_CHANNEL` (persisted by the node; no repeater restart).
- Read-only `packets` tail with HMAC-validated AES-128-ECB decryption.
- Loop guards: webhook intake filtering, outgoing echo memory, per-hop dedupe.
- Per-channel token bucket and explicit Discord user/role TX allowlist.
- Byte-budget truncation with word-boundary preference.
