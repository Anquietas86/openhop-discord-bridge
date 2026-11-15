"""Direction-agnostic bridge policy: prefixes, truncation, echo and rate limits.

All of this is deliberately synchronous and free of Discord/protocol imports so
it can be unit tested without a network or a bot token.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple

ELLIPSIS = "\u2026"

# Discord rejects webhook usernames containing its own brand names (400, code
# 50035, "Username cannot contain \"discord\""), and reserves a couple of words.
FORBIDDEN_USERNAME_PARTS = ("discord", "clyde")
RESERVED_USERNAMES = {"everyone", "here", "mesh"}


def sanitise_webhook_username(name: str, fallback: str = "mesh") -> str:
    """Make a mesh sender name acceptable as a Discord webhook username.

    A mesh node called "VK5JPH Discord" would otherwise fail every post with a
    400, so the brand word is stripped rather than the message dropped.
    """
    out = name or ""
    for bad in FORBIDDEN_USERNAME_PARTS:
        while bad in out.lower():
            index = out.lower().index(bad)
            out = out[:index] + out[index + len(bad):]
    out = " ".join(out.split()).strip(" -_|\u00b7:,")
    if not out or out.lower() in RESERVED_USERNAMES:
        return fallback
    return out[:80]


def truncate_mesh_text(prefix: str, body: str, max_bytes: int) -> str:
    """Fit ``prefix + body`` into ``max_bytes`` UTF-8 bytes.

    The mesh limit applies to the payload, so the prefix spends the budget
    first; the body is cut on a word boundary where one exists nearby, and an
    ellipsis marks the loss. Nothing is truncated mid-character.
    """
    prefix = prefix or ""
    budget = max_bytes - len(prefix.encode("utf-8"))
    if budget <= 0:
        return prefix.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    raw = body.encode("utf-8")
    if len(raw) <= budget:
        return prefix + body

    ellipsis_cost = len(ELLIPSIS.encode("utf-8"))
    cut = budget - ellipsis_cost
    if cut <= 0:
        return prefix + raw[:budget].decode("utf-8", "ignore")
    candidate = raw[:cut].decode("utf-8", "ignore")
    # Prefer a word boundary if one is reasonably close to the cut.
    space = candidate.rfind(" ")
    if space >= cut * 0.6:
        candidate = candidate[:space]
    return prefix + candidate.rstrip() + ELLIPSIS


def mesh_text_budget(max_bytes: int, node_name: str) -> int:
    """Bytes available for our own text once the node prepends ``"<name>: "``.

    The repeater writes the companion's node name in front of every channel
    message it transmits, so the payload we build is not the whole payload.
    Ignoring this overflows the channel's byte budget by ``len(name) + 2``.
    """
    overhead = len(node_name.encode("utf-8")) + 2
    return max(16, max_bytes - overhead)


SENDER_NAME_MODES = ("display_name", "username")


def mesh_sender_label(
    display_name: str, username: str, mode: str = "display_name", max_len: int = 24
) -> str:
    """How a Discord speaker is labelled on the mesh.

    ``display_name`` is the server nickname / global display name people see
    (e.g. "Josh Hagger"); ``username`` is the account handle (e.g.
    "anquietas86"). The name spends the mesh byte budget, so it is capped.
    """
    chosen = username if (mode or "").lower() == "username" else display_name
    label = " ".join((chosen or "").split())
    if not label:
        label = " ".join((username or "").split()) or "discord"
    return label[:max_len]


def render_prefix(template: str, author: str) -> str:
    try:
        return template.format(author=author)
    except (KeyError, IndexError, ValueError):
        return f"[{author}] "


@dataclass
class EchoGuard:
    """Remembers text the bridge itself transmitted, so it is not echoed back.

    Mesh traffic from the bridge's own companion comes back through the packet
    log like any other transmission; without this the bridge would repost its
    own Discord messages into Discord.
    """

    ttl_seconds: float = 120.0
    max_entries: int = 200
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        self._entries: "OrderedDict[str, float]" = OrderedDict()

    def remember(self, text: str) -> None:
        self._entries[text] = self.clock()
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def is_echo(self, text: str) -> bool:
        """True if this text is our own transmission coming back.

        The node prepends "<node_name>: " to what we send, so the round trip is
        never a literal match for the payload we remembered: compare the text
        after that prefix too. Matching on the remainder also survives renaming
        the companion, which a hardcoded prefix would not.
        """
        if self._matches(text):
            return True
        if ": " in text:
            return self._matches(text.split(": ", 1)[1])
        return False

    def _matches(self, text: str) -> bool:
        sent_at = self._entries.get(text)
        if sent_at is None:
            return False
        if self.clock() - sent_at > self.ttl_seconds:
            del self._entries[text]
            return False
        return True


class TokenBucket:
    """Per-key token bucket, used to keep Discord chatter off the air.

    A radio channel is shared spectrum: a Discord argument must not turn into a
    sustained LoRa flood, so transmissions are rationed per mesh channel.
    """

    def __init__(
        self,
        capacity: int = 3,
        refill_seconds: float = 12.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = max(1, capacity)
        self.refill_seconds = max(0.0, refill_seconds)
        self._clock = clock
        self._state: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last)

    def _bucket(self, key: str) -> Tuple[float, float]:
        now = self._clock()
        tokens, last = self._state.get(key, (float(self.capacity), now))
        if self.refill_seconds > 0:
            tokens = min(
                float(self.capacity), tokens + (now - last) / self.refill_seconds
            )
        return tokens, now

    def try_take(self, key: str) -> bool:
        tokens, now = self._bucket(key)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        return True

    def retry_after(self, key: str) -> float:
        tokens, _ = self._bucket(key)
        if tokens >= 1.0 or self.refill_seconds <= 0:
            return 0.0
        return (1.0 - tokens) * self.refill_seconds


@dataclass(frozen=True)
class Outbound:
    """One queued transmission, with the Discord message it came from.

    The message reference is what lets the sender loop tick the original message
    once the transmission actually succeeds, instead of guessing at queue time.
    """

    mesh_channel: str
    author: str
    text: str
    discord_channel_id: int | None = None
    message_id: int | None = None


class OutboundQueue:
    """Bounded FIFO of transmissions, drained by the sender task.

    Overflow drops the oldest entry rather than the newest: the newest message is
    the one a human is waiting on.
    """

    def __init__(self, maxsize: int = 50) -> None:
        self._q: Deque[Outbound] = deque(maxlen=max(1, maxsize))

    def put(self, item: Outbound) -> Optional[Outbound]:
        dropped = None
        if len(self._q) == self._q.maxlen:
            dropped = self._q.popleft()
        self._q.append(item)
        return dropped

    def get(self) -> Optional[Outbound]:
        return self._q.popleft() if self._q else None

    def __len__(self) -> int:
        return len(self._q)
