"""Payload + render unit tests for ``core.inspectview`` (issue #80).

Pinned by ``${TASK_DIR}/context/prd.md`` § Payload contract. These tests are
the canonical pin for the wire format the HTML template parses; the template
must never drift from the field set and order asserted here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from core import inspectview, policy


# --------------------------------------------------------------------------- #
# Fixtures — minimal policy.yaml that mirrors the canonical promotion chain
# --------------------------------------------------------------------------- #
@pytest.fixture
def policy_path(tmp_path) -> Path:
    """A minimal policy.yaml carrying every layer in the canonical chain."""
    cfg = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0, "access_count": 1, "quality_score": 0.3},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0, "access_count": 2, "quality_score": 0.5},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0, "access_count": 3, "quality_score": 0.6},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0, "access_count": 5, "quality_score": 0.7},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.dump(cfg))
    return p


@pytest.fixture
def engine(policy_path) -> policy.PolicyEngine:
    return policy.PolicyEngine(policy_path=str(policy_path))


def _persisted_item(
    *,
    item_id: str = "m1",
    layer: str = "session",
    stage: str = "stored",
    content: str = "hello",
    tags=None,
    quality_score: float = 0.9,
    access_count: int = 5,
    run_id: str = "run-1",
    session_id: str = "sess-1",
    created_at: str = "2026-05-29T10:00:00+00:00",
    content_hash: str = "deadbeef",
    path: str = "/tmp/wiki/sessions/m1.md",
) -> dict:
    """Return a synthetic item dict in the shape of ``store.iter_layer_items``."""
    return {
        "id": item_id,
        "layer": layer,
        "stage": stage,
        "content": content,
        "tags": list(tags) if tags is not None else ["arch"],
        "quality_score": quality_score,
        "access_count": access_count,
        "run_id": run_id,
        "session_id": session_id,
        "created_at": created_at,
        "content_hash": content_hash,
        "_path": path,
    }


# --------------------------------------------------------------------------- #
# Schema + structural invariants
# --------------------------------------------------------------------------- #
class TestPayloadShape:
    def test_schema_version_pinned_to_one(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["schema_version"] == 1

    def test_generated_at_is_iso8601_utc(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        # ISO-8601 with timezone offset; rejection of naïve timestamps.
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$",
            payload["generated_at"],
        ), payload["generated_at"]

    def test_layers_array_is_canonical_chain(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["layers"] == [
            "transient",
            "ephemeral",
            "working",
            "session",
            "project",
            "global",
        ]

    def test_stages_array_matches_policy(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["stages"] == policy.VALID_STAGES
        assert len(payload["stages"]) == 13

    def test_empty_items_yields_empty_memories(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["memories"] == []


# --------------------------------------------------------------------------- #
# Per-item projection — every AC-relevant field is surfaced
# --------------------------------------------------------------------------- #
class TestItemProjection:
    def test_full_field_set_present(self, engine):
        item = _persisted_item()
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        # AC1 (search): id, content, tags
        assert mem["id"] == "m1"
        assert mem["content"] == "hello"
        assert mem["tags"] == ["arch"]
        # AC2 (viewer): content + preview_truncated flag
        assert mem["preview_truncated"] is False
        # AC3 (lifecycle/layer filter): layer + stage
        assert mem["layer"] == "session"
        assert mem["stage"] == "stored"
        # AC4 (trust): layer + quality_score + access_count
        assert mem["quality_score"] == 0.9
        assert mem["access_count"] == 5
        # AC5 (provenance): run_id, session_id, created_at, content_hash, path
        assert mem["run_id"] == "run-1"
        assert mem["session_id"] == "sess-1"
        assert mem["created_at"] == "2026-05-29T10:00:00+00:00"
        assert mem["content_hash"] == "deadbeef"
        assert mem["path"] == "/tmp/wiki/sessions/m1.md"
        # AC6 (promotion/archive): promotable, next_layer, archived
        assert "promotable" in mem
        assert "next_layer" in mem
        assert mem["archived"] is False

    def test_field_order_is_deterministic(self, engine):
        """The dict insertion order is the same for every item so the
        embedded JSON is byte-for-byte deterministic across runs."""
        a = _persisted_item(item_id="a")
        b = _persisted_item(item_id="b", layer="global", stage="archived")
        payload = inspectview.build_inspect_payload([a, b], engine)
        assert list(payload["memories"][0].keys()) == list(payload["memories"][1].keys())
        # Spot-check the canonical order.
        assert list(payload["memories"][0].keys())[:5] == [
            "id",
            "layer",
            "stage",
            "tags",
            "content",
        ]

    def test_no_leaked_memory_objects(self, engine):
        """The projection MUST NOT leak the original front-matter dict —
        every payload value is a JSON primitive (str, int, float, bool, list,
        None) so the embedded JSON survives ``json.dumps``."""
        item = _persisted_item()
        item["extra_leaked_field"] = object()  # un-serialisable
        payload = inspectview.build_inspect_payload([item], engine)
        # If the projection had leaked extra_leaked_field, json.dumps would
        # raise TypeError. The fact that this serialises proves the field
        # set is whitelisted.
        json.dumps(payload)

    def test_missing_id_falls_back_to_path_stem(self, engine):
        item = _persisted_item(item_id="", path="/tmp/wiki/projects/fallback-id.md")
        item["id"] = ""  # explicit empty
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["id"] == "fallback-id"

    def test_tags_non_string_entries_stripped(self, engine):
        item = _persisted_item(tags=["good", 42, None, "ok"])
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["tags"] == ["good", "ok"]


# --------------------------------------------------------------------------- #
# Truncation behaviour
# --------------------------------------------------------------------------- #
class TestTruncation:
    def test_short_content_not_truncated(self, engine):
        item = _persisted_item(content="abc")
        payload = inspectview.build_inspect_payload([item], engine, preview_width=10)
        mem = payload["memories"][0]
        assert mem["content"] == "abc"
        assert mem["preview_truncated"] is False

    def test_long_content_truncated_with_ellipsis(self, engine):
        item = _persisted_item(content="x" * 500)
        payload = inspectview.build_inspect_payload([item], engine, preview_width=10)
        mem = payload["memories"][0]
        assert mem["content"] == "x" * 10 + "..."
        assert mem["preview_truncated"] is True

    def test_full_flag_bypasses_truncation(self, engine):
        item = _persisted_item(content="x" * 500)
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=10, full=True
        )
        mem = payload["memories"][0]
        assert mem["content"] == "x" * 500
        assert mem["preview_truncated"] is False


# --------------------------------------------------------------------------- #
# promotable / next_layer precomputation — matches PolicyEngine
# --------------------------------------------------------------------------- #
class TestPromotionPrecomputation:
    def test_ephemeral_at_threshold_promotable_to_working(self, engine):
        item = _persisted_item(
            layer="ephemeral",
            quality_score=0.4,  # > 0.3
            access_count=2,     # >= 1
        )
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is True
        assert mem["next_layer"] == "working"
        # And it matches the engine directly.
        assert engine.check_promotion_eligible(item) is True

    def test_session_below_threshold_not_promotable(self, engine):
        item = _persisted_item(
            layer="session",
            quality_score=0.1,  # < 0.6
            access_count=0,
        )
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is False
        assert mem["next_layer"] is None

    def test_global_at_top_has_no_next_layer(self, engine):
        item = _persisted_item(layer="global", quality_score=1.0, access_count=99)
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        # Top of chain — promotion_eligible returns False because
        # promotes_to is None.
        assert mem["promotable"] is False
        assert mem["next_layer"] is None

    def test_unknown_layer_yields_not_promotable(self, engine):
        item = _persisted_item(layer="unknown-layer")
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is False
        assert mem["next_layer"] is None


# --------------------------------------------------------------------------- #
# Archive flag derivation
# --------------------------------------------------------------------------- #
class TestArchiveFlag:
    def test_archived_stage_yields_true(self, engine):
        item = _persisted_item(stage="archived")
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["archived"] is True

    def test_non_archived_stage_yields_false(self, engine):
        item = _persisted_item(stage="stored")
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["archived"] is False


# --------------------------------------------------------------------------- #
# render_html — placeholder substitution
# --------------------------------------------------------------------------- #
class TestRenderHtml:
    def test_substitutes_placeholder_exactly_once(self, engine, tmp_path):
        tpl = tmp_path / "inspect.html"
        tpl.write_text(
            '<script id="inspect-data" type="application/json">'
            "__INSPECT_DATA_JSON__</script>"
        )
        payload = inspectview.build_inspect_payload([], engine)
        out = inspectview.render_html(payload, template_path=tpl)
        assert "__INSPECT_DATA_JSON__" not in out
        # The substituted JSON must be parseable.
        m = re.search(
            r'<script id="inspect-data" type="application/json">(.*?)</script>',
            out,
        )
        assert m is not None
        parsed = json.loads(m.group(1))
        assert parsed["schema_version"] == 1

    def test_default_template_path_uses_importlib_resources(self, engine):
        """No ``template_path`` falls back to ``files('core.templates')`` —
        the warning-free 3.11+ API form pinned by issue #68."""
        out = inspectview.render_html(
            inspectview.build_inspect_payload([], engine),
            template_path=None,
        )
        assert '<script id="inspect-data"' in out
        assert "__INSPECT_DATA_JSON__" not in out


# --------------------------------------------------------------------------- #
# write_inspect_html — file I/O and parent-dir creation
# --------------------------------------------------------------------------- #
class TestWriteInspectHtml:
    def test_creates_parent_directories(self, engine, tmp_path):
        nested = tmp_path / "deep" / "deeper" / "out.html"
        item = _persisted_item()
        written = inspectview.write_inspect_html(
            [item],
            nested,
            engine,
        )
        assert nested.exists()
        assert written.is_absolute()
        text = nested.read_text(encoding="utf-8")
        assert '<script id="inspect-data"' in text
        assert "__INSPECT_DATA_JSON__" not in text

    def test_returns_resolved_absolute_path(self, engine, tmp_path):
        out = tmp_path / "out.html"
        written = inspectview.write_inspect_html([], out, engine)
        assert written == out.resolve()
