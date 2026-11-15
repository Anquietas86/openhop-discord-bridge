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


class OutboundQueue:
    """Bounded FIFO of transmissions, drained by the sender task.

    Overflow drops the oldest entry rather than the newest: the newest message is
    the one a human is waiting on.
    """

    def __init__(self, maxsize: int = 50) -> None:
        self._q: Deque[Tuple[str, str, str]] = deque(maxlen=max(1, maxsize))

    def put(self, mesh_channel: str, author: str, text: str) -> Optional[Tuple[str, str, str]]:
        dropped = None
        if len(self._q) == self._q.maxlen:
            dropped = self._q.popleft()
        self._q.append((mesh_channel, author, text))
        return dropped

    def get(self) -> Optional[Tuple[str, str, str]]:
        return self._q.popleft() if self._q else None

    def __len__(self) -> int:
        return len(self._q)
