"""Entry point: wire the database tailer, the companion link and Discord together.

Runs as a supervised foreground process under the openHop plugin manager (or as
a plain systemd unit while developing), so all output goes to stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from typing import Dict, Optional

import discord

from .config import ChannelMapping, Config, ConfigError, load_config
from .discord_side import BridgeDiscord, build_intents, flatten_for_mesh
from .policy import (
    EchoGuard,
    Outbound,
    OutboundQueue,
    TokenBucket,
    mesh_sender_label,
    mesh_text_budget,
    render_prefix,
    truncate_mesh_text,
)
from .protocol import CompanionClient, CompanionError
from .reader import MeshMessage, PacketTailer, load_channel_secrets

logger = logging.getLogger("openhop_discord_bridge")

# Reaction vocabulary. The message itself is the only progress UI a Discord user
# gets, so each state is distinct: queued is not the same as transmitted, and
# "heard back" is the only one that proves the mesh actually carried it.
REACT_QUEUED = "\U0001f4e1"   # 📡 accepted into the queue
REACT_SENT = "\u2705"         # ✅ node accepted it — on air
REACT_HEARD = "\U0001f4fb"    # 📻 came back through the packet log
REACT_FAILED = "\u274c"       # ❌ transmission failed
REACT_REFUSED = "\u26d4"      # ⛔ not on the allowlist
REACT_THROTTLED = "\u23f3"    # ⏳ rate limited


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.echo = EchoGuard(config.echo_ttl_seconds)
        self.bucket = TokenBucket(config.rate_capacity, config.rate_refill_seconds)
        self.queue = OutboundQueue()
        self.link = CompanionClient(
            config.companion_host,
            config.companion_port,
            app_name=config.companion_app_name,
        )
        self.tailer: Optional[PacketTailer] = None
        self.secrets: Dict[str, list] = {}
        self.discord: Optional[BridgeDiscord] = None
        self._ready_done = False
        self._pending_echo: dict[str, Outbound] = {}
        self._sender_task: Optional[asyncio.Task] = None

    # ── mesh keys ──────────────────────────────────────────────────────
    def _channel_keys(self) -> Dict[str, bytes]:
        """16-byte AES keys for every mapped channel, in mapping order."""
        self.secrets = load_channel_secrets(self.config.repeater_db_path)
        keys: Dict[str, bytes] = {}
        missing = []
        for mapping in self.config.channels:
            candidates = self.secrets.get(mapping.mesh_channel)
            if not candidates:
                missing.append(mapping.mesh_channel)
                continue
            keys[mapping.mesh_channel] = candidates[0][:16]
        if missing:
            raise ConfigError(
                "no channel secret in the repeater database for: "
                + ", ".join(repr(m) for m in missing)
                + " — the bridge cannot read or send those channels"
            )
        return keys

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        cfg = self.config
        keys = self._channel_keys()

        await self.link.open()
        # Idempotent provisioning: the node persists what it is given, so the
        # bridge never needs a repeater restart or a direct database write.
        await self.link.provision_channels(keys)
        node_channels = await self.link.read_channels(limit=len(keys) + 1)
        for name, idx in node_channels.items():
            if name not in keys:
                logger.warning("companion exposes unexpected channel %r at %d", name, idx)

        self.tailer = PacketTailer(
            cfg.repeater_db_path,
            {name: self.secrets[name] for name in keys},
            wanted_channels=keys.keys(),
            start_from_latest=cfg.start_from_latest,
        )
        # The tailer starts only once Discord is connected: with
        # autoprovisioning the channel ids do not exist until then.

        intents = build_intents()
        self.discord = BridgeDiscord(self, intents=intents)
        self._sender_task = asyncio.create_task(self._sender_loop(), name="mesh-sender")
        asyncio.create_task(self._link_supervisor(), name="link-supervisor")

        logger.info(
            "bridge starting: %d channel(s) %s -> companion %s:%s",
            len(keys),
            ", ".join(f"{m.mesh_channel}#{m.discord_channel_id}" for m in cfg.channels),
            cfg.companion_host,
            cfg.companion_port,
        )

    async def on_discord_ready(self) -> None:
        """First ready: resolve/create channels, then start tailing packets."""
        if self._ready_done:
            return
        self._ready_done = True
        try:
            await self.provision_discord()
        except Exception:  # noqa: BLE001 - a permission problem must not kill the bridge
            logger.exception("Discord channel provisioning failed")
        if self.tailer is not None:
            asyncio.create_task(
                self.tailer.run(self.on_mesh_message, self.config.poll_interval_seconds),
                name="packet-tailer",
            )

    async def provision_discord(self) -> None:
        """Reconcile every mapping against Discord, creating channels if allowed.

        Runs on every start, not just when autoprovisioning, because a stored id
        can be *wrong* rather than absent: Discord snowflakes exceed float64
        precision, so any editor that parses JSON numbers as floats rewrites
        ...749224 as ...749200. A mapping is therefore verified against the live
        guild and re-resolved by name when the id does not exist.
        """
        cfg = self.config
        if self.discord is None:
            return
        guild = (
            self.discord.get_guild(cfg.discord_guild_id)
            if cfg.discord_guild_id
            else (self.discord.guilds[0] if self.discord.guilds else None)
        )
        if guild is None:
            logger.error("provisioning: the bot is not in any guild")
            return

        category = discord.utils.get(guild.categories, name=cfg.discord_category_name)
        if category is None and cfg.discord_autoprovision:
            category = await guild.create_category(
                cfg.discord_category_name, reason="MeshCore bridge setup"
            )
            logger.info("created category %r", cfg.discord_category_name)

        resolved = {}
        for mapping in cfg.channels:
            live = (
                self.discord.get_channel(mapping.discord_channel_id)
                if mapping.discord_channel_id
                else None
            )
            if live is not None:
                resolved[mapping.mesh_channel] = live.id
                continue

            name = mapping.resolved_channel_name(cfg.discord_channel_prefix)
            target = discord.utils.get(category.text_channels, name=name) if category else None
            if target is None:
                target = discord.utils.get(guild.text_channels, name=name)

            if target is None and cfg.discord_autoprovision and category is not None:
                try:
                    target = await guild.create_text_channel(
                        name, category=category, reason="MeshCore bridge setup"
                    )
                    logger.info("created #%s under %r", name, cfg.discord_category_name)
                except discord.Forbidden:
                    logger.error(
                        "cannot create #%s: the bot needs Manage Channels on the %r "
                        "category. Mesh channel %r stays unmapped.",
                        name, cfg.discord_category_name, mapping.mesh_channel,
                    )
                    continue
                except discord.HTTPException:
                    logger.exception("Discord refused to create #%s", name)
                    continue

            if target is None:
                logger.error(
                    "mesh channel %r maps to stored id %s, which does not exist, and "
                    "no live #%s was found. Leaving it unmapped.",
                    mapping.mesh_channel, mapping.discord_channel_id, name,
                )
                continue

            if mapping.discord_channel_id is not None:
                logger.warning(
                    "mesh channel %r had a stale id %s (float-rounded?); "
                    "re-resolved to #%s (%s)",
                    mapping.mesh_channel, mapping.discord_channel_id, target.name, target.id,
                )
            mapping.discord_channel_id = target.id
            resolved[mapping.mesh_channel] = target.id
            logger.info("mapped mesh %r -> #%s (%s)", mapping.mesh_channel, target.name, target.id)

        self._persist_channel_ids(resolved)

    def _persist_channel_ids(self, resolved: dict) -> None:
        """Write resolved ids back, touching only the channels array."""
        if not resolved:
            return
        from .config import config_path

        path = config_path()
        try:
            data = json.loads(path.read_text())
            for entry in data.get("channels") or []:
                new_id = resolved.get(entry.get("mesh_channel"))
                if new_id:
                    # Store as a STRING: Discord snowflakes exceed float64
                    # precision, and any settings UI that round-trips this JSON
                    # through JavaScript silently rewrites an integer id's low
                    # digits (...749224 -> ...749200). A quoted id cannot lose
                    # precision in that path.
                    entry["discord_channel_id"] = str(new_id)
                if isinstance(entry.get("allowed_user_ids"), list):
                    entry["allowed_user_ids"] = [str(x) for x in entry["allowed_user_ids"]]
                if isinstance(entry.get("allowed_role_ids"), list):
                    entry["allowed_role_ids"] = [str(x) for x in entry["allowed_role_ids"]]
            path.write_text(json.dumps(data, indent=2) + "\n")
            path.chmod(0o600)
            logger.info("persisted %d resolved channel id(s) to %s", len(resolved), path)
        except (OSError, ValueError):
            logger.exception("could not persist resolved channel ids")

    async def start_discord(self) -> None:
        """Log in and connect. ``connect()`` takes no token — ``start(token)`` does."""
        if self.discord is None:
            return
        await self.discord.start(self.config.discord_token)

    async def stop(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.link.close()
        if self.discord is not None and not self.discord.is_closed():
            await self.discord.close()

    # ── mesh -> Discord ────────────────────────────────────────────────
    def on_mesh_message(self, message: MeshMessage) -> None:
        mapping = self.config.mapping_for_mesh(message.channel)
        if mapping is None or not mapping.mesh_to_discord:
            return
        if self.echo.is_echo(message.text):
            logger.debug("suppressed echo of our own transmission: %r", message.text[:40])
            confirmed = self._pending_echo.pop(
                message.text.split(": ", 1)[1] if ": " in message.text else message.text, None
            )
            if confirmed is not None:
                logger.info("delivery confirmed on the mesh for %s", confirmed.mesh_channel)
                asyncio.create_task(self._react(confirmed, REACT_HEARD), name="react-heard")
            return
        text = message.text.strip()
        if not text:
            return
        asyncio.create_task(self._post_mesh(message, mapping, text), name="mesh-post")

    async def _post_mesh(self, message: MeshMessage, mapping: ChannelMapping, text: str) -> None:
        if self.discord is None:
            return
        author = text.split(":", 1)[0] if ":" in text[:32] else "mesh"
        body = text.split(":", 1)[1].strip() if author != "mesh" else text
        try:
            await self.discord.post_mesh_message(mapping.discord_channel_id, author, body)
        except discord.HTTPException:
            logger.exception("Discord rejected a mesh message for %s", mapping.mesh_channel)

    # ── Discord -> mesh ────────────────────────────────────────────────
    async def handle_discord_message(self, message: discord.Message) -> None:
        mapping = self.config.mapping_for_discord(message.channel.id)
        if mapping is None or not mapping.discord_to_mesh:
            return
        role_ids = [role.id for role in getattr(message.author, "roles", [])]
        if not mapping.permits(message.author.id, role_ids):
            logger.info(
                "refused TX from %s (%s): not on the allowlist for %s",
                message.author,
                message.author.id,
                mapping.mesh_channel,
            )
            await self._react(message, REACT_REFUSED)
            return

        text = flatten_for_mesh(message.clean_content) or "[attachment]"
        template = mapping.prefix_template or self.config.mesh_prefix_template
        label = mesh_sender_label(
            message.author.display_name,
            getattr(message.author, "name", ""),
            mapping.sender_name or self.config.discord_sender_name,
            self.config.discord_sender_name_max_chars,
        )
        # With discord_sender_name=none there is no speaker label: send the body
        # bare rather than rendering "{author}: " into ": hello". The node still
        # prefixes its own name, so the message remains attributable to the bridge.
        prefix = render_prefix(template, label) if label else ""
        # The node prepends "<node_name>: " itself, so our own text must fit the
        # remainder or the channel payload overruns its byte budget.
        budget = mesh_text_budget(self.config.max_mesh_bytes, self.config.companion_node_name)
        payload = truncate_mesh_text(prefix, text, budget)

        if not self.bucket.try_take(mapping.mesh_channel):
            wait = self.bucket.retry_after(mapping.mesh_channel)
            logger.warning("rate-limited TX to %s (%.1fs)", mapping.mesh_channel, wait)
            await self._react(message, REACT_THROTTLED)
            return

        self.echo.remember(payload)
        item = Outbound(
            mesh_channel=mapping.mesh_channel,
            author=label,
            text=payload,
            discord_channel_id=message.channel.id,
            message_id=message.id,
        )
        dropped = self.queue.put(item)
        if dropped is not None:
            logger.warning("outbound queue full; dropped a message for %s", dropped.mesh_channel)
        self._pending_echo[payload] = item
        await self._react(message, REACT_QUEUED)

    async def _react(self, ref, emoji: str) -> None:
        """React to the originating Discord message (best effort, never fatal)."""
        if self.discord is None or ref is None:
            return
        try:
            if isinstance(ref, discord.Message):
                await ref.add_reaction(emoji)
                return
            channel = self.discord.get_channel(getattr(ref, "discord_channel_id", None))
            message_id = getattr(ref, "message_id", None)
            if channel is None or message_id is None:
                return
            await channel.get_partial_message(message_id).add_reaction(emoji)
        except (discord.HTTPException, AttributeError):
            logger.debug("could not react %s", emoji, exc_info=True)

    async def _link_supervisor(self) -> None:
        """Keep the companion socket alive across repeater restarts.

        Without this the bridge holds a half-closed handle after the repeater
        restarts: inbound keeps working (it reads the database) while every
        transmission fails, which is the worst possible failure shape because the
        bridge still looks healthy.
        """
        while True:
            await asyncio.sleep(self.config.link_check_seconds)
            if self.link.connected:
                continue
            logger.warning("companion link is down — reconnecting")
            try:
                await self.link.close()
                await self.link.open()
                await self.link.provision_channels(self._channel_keys())
                logger.info("companion link restored on %s:%s",
                            self.config.companion_host, self.config.companion_port)
            except (CompanionError, OSError):
                logger.warning("companion reconnect failed; will retry", exc_info=True)

    async def _sender_loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                await asyncio.sleep(0.25)
                continue
            mesh_channel, payload = item.mesh_channel, item.text
            idx = self.link.channels.get(mesh_channel)
            if idx is None:
                logger.error("no companion channel index for %s; dropping", mesh_channel)
                continue
            try:
                if not self.link.connected:
                    await self.link.open()
                    await self.link.provision_channels(self._channel_keys())
                await self.link.send_channel(idx, payload.encode("utf-8"), int(time.time()))
                logger.info("TX %s (%dB): %s", mesh_channel, len(payload.encode()), payload)
                await self._react(item, REACT_SENT)
            except (CompanionError, OSError):
                logger.exception("TX to %s failed", mesh_channel)
                await self._react(item, REACT_FAILED)


async def _amain(config: Config) -> int:
    bridge = Bridge(config)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    await bridge.start()
    waiter = asyncio.create_task(stop.wait(), name="shutdown-waiter")
    discord_task = asyncio.create_task(bridge.start_discord(), name="discord-client")
    done, _pending = await asyncio.wait(
        {waiter, discord_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if discord_task in done and not discord_task.cancelled():
        exc = discord_task.exception()
        if exc is not None:
            text = str(exc).lower()
            if "privileged intents" in text or "disallowed intents" in text:
                logger.error(
                    "Discord refused the gateway: the MESSAGE CONTENT intent is "
                    "disabled on the application. Enable it at "
                    "https://discord.com/developers/applications -> your app -> "
                    "Bot -> Privileged Gateway Intents -> Message Content Intent, "
                    "then restart this plugin. Without it the bot connects but can "
                    "never see message text, so there is nothing to bridge."
                )
                await bridge.stop()
                return 3
            logger.error("Discord client stopped: %s", exc)
    await bridge.stop()
    return 0


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout)
        logger.error("configuration error: %s", exc)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain(config))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - log, then let the supervisor restart us
        logger.exception("bridge crashed")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
