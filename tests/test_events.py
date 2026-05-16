"""Tests for core/events.py — EventBus."""
from __future__ import annotations

import pytest

from core.events import EventBus, POST_CAPTURE, POST_PROMOTE, POST_ARCHIVE, POST_FORGET, VALID_EVENTS


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestEventBusInit:
    def test_new_bus_has_no_handlers(self):
        bus = EventBus()
        assert bus.handler_count(POST_CAPTURE) == 0
        assert bus.handler_count(POST_PROMOTE) == 0

    def test_valid_events_constant(self):
        for event in (POST_CAPTURE, POST_PROMOTE, POST_ARCHIVE, POST_FORGET):
            assert event in VALID_EVENTS


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------

class TestSubscribeUnsubscribe:
    def test_subscribe_registers_handler(self):
        bus = EventBus()
        handler = lambda p: None  # noqa: E731
        bus.subscribe(POST_PROMOTE, handler)
        assert bus.handler_count(POST_PROMOTE) == 1

    def test_subscribe_multiple_handlers_same_event(self):
        bus = EventBus()
        bus.subscribe(POST_PROMOTE, lambda p: None)
        bus.subscribe(POST_PROMOTE, lambda p: None)
        assert bus.handler_count(POST_PROMOTE) == 2

    def test_subscribe_same_handler_twice(self):
        """Subscribing the same handler twice results in two registrations."""
        bus = EventBus()
        handler = lambda p: None  # noqa: E731
        bus.subscribe(POST_PROMOTE, handler)
        bus.subscribe(POST_PROMOTE, handler)
        assert bus.handler_count(POST_PROMOTE) == 2

    def test_subscribe_different_events_are_independent(self):
        bus = EventBus()
        bus.subscribe(POST_CAPTURE, lambda p: None)
        bus.subscribe(POST_PROMOTE, lambda p: None)
        assert bus.handler_count(POST_CAPTURE) == 1
        assert bus.handler_count(POST_PROMOTE) == 1

    def test_unsubscribe_removes_first_matching_registration(self):
        bus = EventBus()
        handler = lambda p: None  # noqa: E731
        bus.subscribe(POST_PROMOTE, handler)
        bus.subscribe(POST_PROMOTE, handler)  # two registrations
        bus.unsubscribe(POST_PROMOTE, handler)
        assert bus.handler_count(POST_PROMOTE) == 1

    def test_unsubscribe_noop_when_not_registered(self):
        bus = EventBus()
        handler = lambda p: None  # noqa: E731
        # Should not raise
        bus.unsubscribe(POST_PROMOTE, handler)
        assert bus.handler_count(POST_PROMOTE) == 0

    def test_subscribe_accepts_arbitrary_event_names(self):
        """EventBus should accept custom event names beyond VALID_EVENTS."""
        bus = EventBus()
        bus.subscribe("custom:my-event", lambda p: None)
        assert bus.handler_count("custom:my-event") == 1


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------

class TestEmit:
    def test_emit_calls_registered_handler(self):
        bus = EventBus()
        received: list[dict] = []
        bus.subscribe(POST_PROMOTE, received.append)

        payload = {"item_id": "abc", "content_preview": "hello", "from_layer": "session", "to_layer": "project"}
        bus.emit(POST_PROMOTE, payload)

        assert received == [payload]

    def test_emit_calls_all_handlers_in_order(self):
        bus = EventBus()
        order: list[str] = []
        bus.subscribe(POST_PROMOTE, lambda p: order.append("first"))
        bus.subscribe(POST_PROMOTE, lambda p: order.append("second"))

        bus.emit(POST_PROMOTE, {})

        assert order == ["first", "second"]

    def test_emit_does_not_call_handlers_for_other_events(self):
        bus = EventBus()
        called: list[bool] = []
        bus.subscribe(POST_CAPTURE, lambda p: called.append(True))

        bus.emit(POST_PROMOTE, {"item_id": "xyz"})

        assert called == []

    def test_emit_with_no_handlers_does_not_raise(self):
        bus = EventBus()
        bus.emit(POST_PROMOTE, {"item_id": "no-handlers"})  # should not raise

    def test_emit_handler_error_does_not_stop_fan_out(self):
        """A handler that raises must not prevent subsequent handlers from running."""
        bus = EventBus()
        results: list[str] = []

        def bad_handler(payload: dict) -> None:
            raise RuntimeError("boom")

        def good_handler(payload: dict) -> None:
            results.append("ok")

        bus.subscribe(POST_PROMOTE, bad_handler)
        bus.subscribe(POST_PROMOTE, good_handler)

        # Should not raise — bad_handler error is swallowed
        bus.emit(POST_PROMOTE, {})
        assert results == ["ok"]

    def test_emit_passes_payload_unchanged(self):
        bus = EventBus()
        received: list[dict] = []
        bus.subscribe(POST_PROMOTE, received.append)

        payload = {"item_id": "id-001", "from_layer": "working", "to_layer": "session", "extra": 42}
        bus.emit(POST_PROMOTE, payload)

        assert received[0] is payload  # same dict object, not a copy

    def test_emit_snapshot_prevents_mutation_during_dispatch(self):
        """Handlers added during emission are not called in the current dispatch."""
        bus = EventBus()
        call_count: list[int] = [0]

        def first_handler(payload: dict) -> None:
            call_count[0] += 1
            # Add a new handler mid-dispatch — should NOT be called this round
            bus.subscribe(POST_PROMOTE, lambda p: call_count.__setitem__(0, call_count[0] + 10))

        bus.subscribe(POST_PROMOTE, first_handler)
        bus.emit(POST_PROMOTE, {})

        # Only the original handler ran (+1), not the newly-added one (+10)
        assert call_count[0] == 1

    def test_emit_multiple_events_independently(self):
        bus = EventBus()
        capture_calls: list[dict] = []
        promote_calls: list[dict] = []

        bus.subscribe(POST_CAPTURE, capture_calls.append)
        bus.subscribe(POST_PROMOTE, promote_calls.append)

        bus.emit(POST_CAPTURE, {"item_id": "cap-1", "layer": "session"})
        bus.emit(POST_PROMOTE, {"item_id": "promo-1", "from_layer": "session", "to_layer": "project"})

        assert len(capture_calls) == 1
        assert capture_calls[0]["item_id"] == "cap-1"
        assert len(promote_calls) == 1
        assert promote_calls[0]["item_id"] == "promo-1"


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_removes_all_handlers(self):
        bus = EventBus()
        bus.subscribe(POST_PROMOTE, lambda p: None)
        bus.subscribe(POST_CAPTURE, lambda p: None)
        bus.clear()
        assert bus.handler_count(POST_PROMOTE) == 0
        assert bus.handler_count(POST_CAPTURE) == 0

    def test_clear_single_event(self):
        bus = EventBus()
        bus.subscribe(POST_PROMOTE, lambda p: None)
        bus.subscribe(POST_CAPTURE, lambda p: None)
        bus.clear(POST_PROMOTE)
        assert bus.handler_count(POST_PROMOTE) == 0
        assert bus.handler_count(POST_CAPTURE) == 1

    def test_clear_noop_for_unknown_event(self):
        bus = EventBus()
        bus.clear("nonexistent-event")  # should not raise


# ---------------------------------------------------------------------------
# handler_count
# ---------------------------------------------------------------------------

class TestHandlerCount:
    def test_zero_for_unregistered_event(self):
        bus = EventBus()
        assert bus.handler_count("ghost-event") == 0

    def test_increments_with_each_subscribe(self):
        bus = EventBus()
        for i in range(5):
            bus.subscribe(POST_PROMOTE, lambda p: None)
            assert bus.handler_count(POST_PROMOTE) == i + 1


# ---------------------------------------------------------------------------
# Integration with gateway (EventBus wired into MemoryGateway)
# ---------------------------------------------------------------------------

class TestEventBusGatewayIntegration:
    """Verify that gateway.event_bus is accessible and emits events correctly."""

    @pytest.fixture
    def repo_root(self, tmp_path):
        import yaml
        wiki = tmp_path / "wiki"
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (wiki / d).mkdir(parents=True)
        agent = tmp_path / ".agent"
        for d in ["runs", "sessions", "state", "reports", "tools"]:
            (agent / d).mkdir(parents=True)
        (agent / "workflows" / "hooks").mkdir(parents=True)
        policy = {
            "layers": {
                "ephemeral": {"path_template": ".agent/runs/{run_id}/scratch/", "promotes_to": "working",
                               "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0}},
                "working": {"path_template": ".agent/runs/{run_id}/working/", "promotes_to": "session",
                             "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0}},
                "session": {"path_template": ".agent/sessions/{session_id}/", "promotes_to": "project",
                             "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0}},
                "project": {"path_template": "wiki/projects/", "promotes_to": "global",
                             "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0}},
                "global": {"path_template": "wiki/global/", "promotes_to": None,
                            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0}},
            },
            "forget": {"requires_archived": True},
            "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
        }
        (wiki / "policy.yaml").write_text(yaml.dump(policy))
        (wiki / "log.md").write_text("# Log\n")
        (wiki / "log.jsonl").write_text("")
        return tmp_path

    @pytest.fixture
    def gateway(self, repo_root):
        from core.gateway import MemoryGateway
        return MemoryGateway(repo_root=str(repo_root))

    def test_gateway_exposes_event_bus(self, gateway):
        assert isinstance(gateway.event_bus, EventBus)

    def test_capture_emits_post_capture_event(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_CAPTURE, received.append)

        gateway.capture(layer="global", content="hello world", run_id="r1")

        assert len(received) == 1
        assert "item_id" in received[0]
        assert received[0]["layer"] == "global"
        assert received[0]["content_preview"] == "hello world"

    def test_capture_preview_is_truncated_to_60_chars(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_CAPTURE, received.append)

        long_content = "x" * 200
        gateway.capture(layer="global", content=long_content, run_id="r1")

        assert len(received[0]["content_preview"]) == 60

    def test_promote_emits_post_promote_event(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_PROMOTE, received.append)

        item_id = gateway.capture(layer="project", content="Promote me", run_id="r1")
        gateway.promote(item_id=item_id, run_id="r1")

        assert len(received) == 1
        evt = received[0]
        assert evt["item_id"] == item_id
        assert evt["from_layer"] == "project"
        assert evt["to_layer"] == "global"
        assert "content_preview" in evt

    def test_promote_event_content_preview(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_PROMOTE, received.append)

        item_id = gateway.capture(layer="project", content="Architecture decision: use SQLite", run_id="r1")
        gateway.promote(item_id=item_id, run_id="r1")

        assert received[0]["content_preview"] == "Architecture decision: use SQLite"

    def test_auto_promote_emits_post_promote_event(self, gateway):
        """_auto_promote_if_eligible must emit post-promote event (previously silent)."""
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_PROMOTE, received.append)

        item_id = gateway.capture(layer="project", content="Auto-promote me", quality_score=0.9, run_id="r1")
        # Reading triggers auto-promotion with zero thresholds
        gateway.read(item_id)

        assert len(received) >= 1
        assert any(e["item_id"] == item_id for e in received)

    def test_archive_emits_post_archive_event(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_ARCHIVE, received.append)

        item_id = gateway.capture(layer="global", content="Archive me", run_id="r1")
        gateway.archive(item_id=item_id)

        assert len(received) == 1
        assert received[0]["item_id"] == item_id

    def test_forget_emits_post_forget_event(self, gateway):
        received: list[dict] = []
        gateway.event_bus.subscribe(POST_FORGET, received.append)

        item_id = gateway.capture(layer="global", content="Forget me", run_id="r1")
        gateway.archive(item_id=item_id)
        gateway.forget(item_id=item_id)

        assert len(received) == 1
        assert received[0]["item_id"] == item_id

    def test_multiple_subscribers_all_notified(self, gateway):
        """All registered subscribers receive the event."""
        results_a: list[dict] = []
        results_b: list[dict] = []
        gateway.event_bus.subscribe(POST_PROMOTE, results_a.append)
        gateway.event_bus.subscribe(POST_PROMOTE, results_b.append)

        item_id = gateway.capture(layer="project", content="Fan-out test", run_id="r1")
        gateway.promote(item_id=item_id, run_id="r1")

        assert len(results_a) == 1
        assert len(results_b) == 1
        assert results_a[0]["item_id"] == item_id
        assert results_b[0]["item_id"] == item_id
