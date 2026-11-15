"""Discord side of the bridge: intake, and posting as webhooks.

Mesh senders appear as themselves in Discord because posts go out through a
webhook with ``username`` overridden, not through the bot account. The bot is
only a reader and an identity; the mesh channel membership is what people see.

Messages posted by any webhook (ours included) are ignored on intake — that is
the first line of defence against a mesh -> Discord -> mesh loop.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import discord

logger = logging.getLogger(__name__)

MESH_SAFE_NEWLINES = " / "


def flatten_for_mesh(text: str) -> str:
    """Group text is one line on a 128-byte channel; newlines waste the budget."""
    return " ".join(part.strip() for part in text.replace("\r", "").split("\n") if part.strip())


class BridgeDiscord(discord.Client):
    def __init__(self, bridge, intents: Optional[discord.Intents] = None) -> None:
        super().__init__(intents=intents or (discord.Intents.default() | discord.Intents.message_content))
        self.bridge = bridge
        self._webhooks: Dict[int, discord.Webhook] = {}

    async def on_ready(self) -> None:
        logger.info("Discord connected as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        try:
            await self.bridge.handle_discord_message(message)
        except Exception:  # noqa: BLE001 - one bad message must not kill the client
            logger.exception("failed handling Discord message %s", message.id)

    async def webhook_for(self, channel: discord.abc.GuildChannel) -> discord.Webhook:
        cached = self._webhooks.get(channel.id)
        if cached is not None:
            return cached
        name = self.bridge.config.webhook_name
        hooks = await channel.webhooks()
        hook = next((h for h in hooks if h.name == name and h.token), None)
        if hook is None:
            hook = await channel.create_webhook(name=name, reason="MeshCore mesh bridge")
            logger.info("created webhook %r in #%s", name, getattr(channel, "name", channel.id))
        self._webhooks[channel.id] = hook
        return hook

    async def post_mesh_message(self, channel_id: int, author: str, text: str) -> None:
        channel = self.get_channel(channel_id)
        if channel is None:
            logger.warning("Discord channel %s is not visible to the bot", channel_id)
            return
        hook = await self.webhook_for(channel)
        await hook.send(
            content=text,
            username=author[:80] or "mesh",
            allowed_mentions=discord.AllowedMentions.none(),
            wait=False,
        )
