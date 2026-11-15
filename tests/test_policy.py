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
    # An id is optional now (auto-provisioning can create the channel), but the
    # mesh channel is mandatory and a supplied id must be numeric and positive.
    with pytest_raises(ConfigError):
        ChannelMapping.from_dict({"discord_channel_id": 1})
    with pytest_raises(ConfigError):
        ChannelMapping.from_dict({"mesh_channel": "Public", "discord_channel_id": "not-a-number"})
    with pytest_raises(ConfigError):
        ChannelMapping.from_dict({"mesh_channel": "Public", "discord_channel_id": -5})
    mapping = ChannelMapping.from_dict({"mesh_channel": "Public"})
    assert mapping.discord_channel_id is None
    assert mapping.resolved_channel_name() == "public"


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


def test_webhook_username_strips_discord_brand():
    """Discord rejects webhook usernames containing its own name (400/50035)."""
    from openhop_discord_bridge.policy import sanitise_webhook_username as s

    assert s("VK5JPH Discord \U0001f916") == "VK5JPH \U0001f916"
    assert s("discord") == "mesh"
    assert s("Discord Bridge") == "Bridge"
    assert s("clyde bot") == "bot"
    assert s("VK5OG-T1000e") == "VK5OG-T1000e"  # ordinary names untouched


def test_webhook_username_fallback_and_truncation():
    from openhop_discord_bridge.policy import sanitise_webhook_username as s

    assert s("") == "mesh"
    assert s("   ") == "mesh"
    assert s("everyone") == "mesh"
    assert len(s("x" * 200)) == 80


def test_echo_matches_after_node_name_prefix():
    """The node prepends '<node_name>: ', so a literal match never happens."""
    clock = FakeClock(1000.0)
    guard = EchoGuard(ttl_seconds=60, clock=clock)
    guard.remember("Josh Hagger: !test")
    assert guard.is_echo("Josh Hagger: !test")
    assert guard.is_echo("VK5JPH Discord \U0001f916: Josh Hagger: !test")
    assert not guard.is_echo("Ranga: hello")


def test_mesh_sender_label_modes():
    """display_name = what people see; username = the account handle."""
    from openhop_discord_bridge.policy import mesh_sender_label as m

    assert m("Josh Hagger", "anquietas86") == "Josh Hagger"
    assert m("Josh Hagger", "anquietas86", "display_name") == "Josh Hagger"
    assert m("Josh Hagger", "anquietas86", "username") == "anquietas86"
    # a missing display name falls back to the handle rather than "None"
    assert m("", "anquietas86") == "anquietas86"
    assert m("", "") == "discord"


def test_mesh_sender_label_is_capped():
    from openhop_discord_bridge.policy import mesh_sender_label as m

    assert len(m("A very long display name indeed", "handle", "display_name", 10)) == 10
