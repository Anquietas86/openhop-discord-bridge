"""Entry point: wire the database tailer, the companion link and Discord together.

Runs as a supervised foreground process under the openHop plugin manager (or as
a plain systemd unit while developing), so all output goes to stdout.
"""

from __future__ import annotations

import asyncio
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
    OutboundQueue,
    TokenBucket,
    render_prefix,
    truncate_mesh_text,
)
from .protocol import CompanionClient, CompanionError
from .reader import MeshMessage, PacketTailer, load_channel_secrets

logger = logging.getLogger("openhop_discord_bridge")


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
        asyncio.create_task(
            self.tailer.run(self.on_mesh_message, cfg.poll_interval_seconds),
            name="packet-tailer",
        )

        intents = build_intents()
        self.discord = BridgeDiscord(self, intents=intents)
        self._sender_task = asyncio.create_task(self._sender_loop(), name="mesh-sender")

        logger.info(
            "bridge starting: %d channel(s) %s -> companion %s:%s",
            len(keys),
            ", ".join(f"{m.mesh_channel}#{m.discord_channel_id}" for m in cfg.channels),
            cfg.companion_host,
            cfg.companion_port,
        )

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
            try:
                await message.add_reaction("\u26d4")
            except discord.HTTPException:
                pass
            return

        text = flatten_for_mesh(message.clean_content) or "[attachment]"
        template = mapping.prefix_template or self.config.mesh_prefix_template
        prefix = render_prefix(template, message.author.display_name)
        payload = truncate_mesh_text(prefix, text, self.config.max_mesh_bytes)

        if not self.bucket.try_take(mapping.mesh_channel):
            wait = self.bucket.retry_after(mapping.mesh_channel)
            logger.warning("rate-limited TX to %s (%.1fs)", mapping.mesh_channel, wait)
            try:
                await message.add_reaction("\u23f3")
            except discord.HTTPException:
                pass
            return

        self.echo.remember(payload)
        self.queue.put(mapping.mesh_channel, message.author.display_name, payload)
        try:
            await message.add_reaction("\U0001f4e1")
        except discord.HTTPException:
            pass

    async def _sender_loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                await asyncio.sleep(0.25)
                continue
            mesh_channel, _author, payload = item
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
            except (CompanionError, OSError):
                logger.exception("TX to %s failed", mesh_channel)


async def _amain(config: Config) -> None:
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
    discord_task = asyncio.create_task(bridge.discord.connect(config.discord_token, reconnect=True))
    done, _pending = await asyncio.wait(
        {waiter, discord_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if discord_task in done and not discord_task.cancelled():
        exc = discord_task.exception()
        if exc is not None:
            logger.error("Discord client stopped: %s", exc)
    await bridge.stop()


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
        asyncio.run(_amain(config))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - log, then let the supervisor restart us
        logger.exception("bridge crashed")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
