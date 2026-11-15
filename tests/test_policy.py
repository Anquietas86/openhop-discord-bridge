from openhop_discord_bridge.config import ConfigError, ChannelMapping
from openhop_discord_bridge.policy import (
    EchoGuard,
    OutboundQueue,
    TokenBucket,
    render_prefix,
    truncate_mesh_text,
)


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_truncate_leaves_short_text_alone():
    assert truncate_mesh_text("[Josh] ", "hello", 128) == "[Josh] hello"


def test_truncate_respects_byte_budget():
    out = truncate_mesh_text("[Josh] ", "x" * 400, 128)
    assert len(out.encode()) <= 128
    assert out.endswith("\u2026")


def test_truncate_respects_word_boundary_and_multibyte():
    out = truncate_mesh_text("[Josh] ", "the quick brown fox jumps over the lazy dog " * 5, 60)
    assert len(out.encode()) <= 60
    assert not out.endswith(" \u2026")


def test_truncate_when_prefix_alone_is_too_long():
    out = truncate_mesh_text("[a very long author name] ", "body", 10)
    assert len(out.encode()) <= 10


def test_render_prefix_falls_back_on_bad_template():
    assert render_prefix("{oops}", "Josh") == "[Josh] "
    assert render_prefix("[{author}] ", "Josh") == "[Josh] "


def test_echo_guard_expires():
    clock = FakeClock(1000.0)
    guard = EchoGuard(ttl_seconds=10, clock=clock)
    guard.remember("hello")
    assert guard.is_echo("hello")
    clock.t = 1005.0
    assert guard.is_echo("hello")
    clock.t = 1011.0
    assert not guard.is_echo("hello")
    assert not guard.is_echo("never sent")


def test_token_bucket_rations_and_refills():
    clock = FakeClock(0.0)
    bucket = TokenBucket(capacity=2, refill_seconds=10.0, clock=clock)
    assert bucket.try_take("Public")
    assert bucket.try_take("Public")
    assert not bucket.try_take("Public")
    assert bucket.retry_after("Public") > 0
    clock.t = 10.0
    assert bucket.try_take("Public")


def test_token_bucket_is_per_key():
    bucket = TokenBucket(capacity=1, refill_seconds=0.0)
    assert bucket.try_take("Public")
    assert bucket.try_take("#other")


def test_outbound_queue_drops_oldest():
    q = OutboundQueue(maxsize=2)
    q.put("Public", "a", "one")
    q.put("Public", "a", "two")
    dropped = q.put("Public", "a", "three")
    assert dropped == ("Public", "a", "one")
    assert len(q) == 2
    assert q.get() == ("Public", "a", "two")


def test_mapping_permits_requires_explicit_grant():
    mapping = ChannelMapping(mesh_channel="Public", discord_channel_id=123)
    assert not mapping.permits(1, [2, 3])
    by_user = ChannelMapping(mesh_channel="Public", discord_channel_id=123, allowed_user_ids=[7])
    assert by_user.permits(7, [])
    by_role = ChannelMapping(mesh_channel="Public", discord_channel_id=123, allowed_role_ids=[42])
    assert by_role.permits(9, [42])
    assert not by_role.permits(9, [43])


def test_mapping_from_dict_validation():
    with pytest_raises(ConfigError):
        ChannelMapping.from_dict({"discord_channel_id": 1})
    with pytest_raises(ConfigError):
        ChannelMapping.from_dict({"mesh_channel": "Public"})


class pytest_raises:
    """Tiny stand-in so this module does not import pytest for two assertions."""

    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"{self.exc.__name__} not raised")
        return issubclass(exc_type, self.exc)


def test_mesh_budget_subtracts_node_name_prefix():
    """The repeater prepends "<node_name>: " itself; our text must fit the rest."""
    from openhop_discord_bridge.policy import mesh_text_budget

    assert mesh_text_budget(128, "Discord Bridge") == 128 - 16
    assert mesh_text_budget(128, "") == 126
    assert mesh_text_budget(20, "A very long companion node name") == 16  # floor


def test_payload_plus_node_prefix_stays_within_budget():
    from openhop_discord_bridge.policy import mesh_text_budget, truncate_mesh_text

    for body in ("short", "x" * 500, "the quick brown fox " * 20):
        budget = mesh_text_budget(128, "Discord Bridge")
        payload = truncate_mesh_text("Josh: ", body, budget)
        total = len(("Discord Bridge: " + payload).encode())
        assert total <= 128, (total, payload[:40])
