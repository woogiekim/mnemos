"""Tests for core.graphview — domain-relationship graph-view payload + render.

Derived from {TASK_DIR}/context/design-spec.md (issue #68 Stage 2). The
test-writer reads the design spec, not the implementation. Every behavior
asserted here is pinned by Sections 3, 4, 7, and 9 of design-spec.md.

The repo enforces ``--cov-fail-under=100`` and ``filterwarnings=["error"]``,
so every line of ``core/graphview.py`` must be exercised here with no warnings
emitted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core import cohesion, graphview
from core.graphview import (
    build_graph_payload,
    render_html,
    write_graph_html,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _item(item_id, *, tags=None, layer="session", content=""):
    """Build a projected item dict (matches core.cohesion item shape)."""
    d = {"id": item_id, "layer": layer, "content": content}
    if tags is not None:
        d["tags"] = list(tags)
    return d


@pytest.fixture
def small_items():
    """A small set of projected memory items spanning two namespaces."""
    return [
        _item("mem-001", tags=["agent:backend", "decision:auth"], layer="session",
              content="Authentication decision: use OAuth2 with PKCE"),
        _item("mem-002", tags=["agent:backend"], layer="session",
              content="Backend uses fastapi"),
        _item("mem-003", tags=["agent:designer"], layer="project",
              content="Designer agent emits design-spec.md"),
        _item("mem-004", tags=["decision:auth", "constraint:security"], layer="global",
              content="Auth must use refresh tokens"),
    ]


@pytest.fixture
def long_content_items():
    """An item whose content is longer than the default preview width."""
    long = "x" * 500
    return [_item("mem-long", tags=["agent:backend"], layer="session", content=long)]


# --------------------------------------------------------------------------- #
# T01 — empty-graph path
# --------------------------------------------------------------------------- #
class TestBuildGraphPayloadEmpty:
    def test_empty_items_returns_v1_payload_with_empty_collections(self):
        payload = build_graph_payload([])
        assert payload["schema_version"] == 1
        assert payload["domains"] == []
        assert payload["relationships"] == []
        assert payload["memories"] == {}
        # generated_at is always present (delegated from DomainGraph.to_dict()).
        assert isinstance(payload["generated_at"], str)
        assert len(payload["generated_at"]) > 0


# --------------------------------------------------------------------------- #
# T02 — domain/edge round-trip
# --------------------------------------------------------------------------- #
class TestBuildGraphPayloadRoundTrip:
    def test_domain_count_matches_build_domain_graph(self, small_items):
        graph = cohesion.build_domain_graph(small_items)
        payload = build_graph_payload(small_items)
        assert len(payload["domains"]) == len(graph.domains)

    def test_relationship_count_matches_build_domain_graph(self, small_items):
        graph = cohesion.build_domain_graph(small_items)
        payload = build_graph_payload(small_items)
        assert len(payload["relationships"]) == len(graph.relationships)

    def test_domains_passthrough_from_to_dict(self, small_items):
        graph = cohesion.build_domain_graph(small_items)
        payload = build_graph_payload(small_items)
        # Pass-through invariant: each domain dict in payload must equal the
        # corresponding entry in graph.to_dict() (same field set, same values).
        # ``generated_at`` is regenerated per call (the cohesion module stamps
        # ``datetime.now(tz=utc)`` at build time), so we only assert format
        # rather than equality across two separate builds.
        graph_dict = graph.to_dict()
        assert payload["domains"] == graph_dict["domains"]
        assert payload["relationships"] == graph_dict["relationships"]
        assert payload["schema_version"] == graph_dict["schema_version"]
        assert isinstance(payload["generated_at"], str)
        assert isinstance(graph_dict["generated_at"], str)


# --------------------------------------------------------------------------- #
# T03 — memories map keying & schema
# --------------------------------------------------------------------------- #
class TestMemoriesMap:
    def test_memories_keyed_by_member_ids_union(self, small_items):
        payload = build_graph_payload(small_items)
        # Union of every member_ids across every domain
        expected_ids: set = set()
        for d in payload["domains"]:
            expected_ids.update(d["member_ids"])
        assert set(payload["memories"].keys()) == expected_ids

    def test_memory_entry_has_exact_field_set(self, small_items):
        payload = build_graph_payload(small_items)
        assert payload["memories"], "fixture must produce at least one memory"
        for mid, entry in payload["memories"].items():
            assert set(entry.keys()) == {"id", "layer", "tags", "content_preview"}
            assert entry["id"] == mid

    def test_memory_entry_carries_layer_and_tags(self, small_items):
        payload = build_graph_payload(small_items)
        entry = payload["memories"]["mem-001"]
        assert entry["layer"] == "session"
        assert "agent:backend" in entry["tags"]
        assert "decision:auth" in entry["tags"]


# --------------------------------------------------------------------------- #
# T04 / T05 — preview truncation and --full bypass
# --------------------------------------------------------------------------- #
class TestPreviewTruncation:
    def test_preview_width_truncates_with_ellipsis(self, long_content_items):
        payload = build_graph_payload(long_content_items, preview_width=10)
        entry = payload["memories"]["mem-long"]
        assert entry["content_preview"] == "x" * 10 + "..."

    def test_default_preview_width_is_240(self, long_content_items):
        payload = build_graph_payload(long_content_items)
        entry = payload["memories"]["mem-long"]
        assert entry["content_preview"] == "x" * 240 + "..."

    def test_full_bypasses_preview_width(self, long_content_items):
        payload = build_graph_payload(long_content_items, preview_width=10, full=True)
        entry = payload["memories"]["mem-long"]
        assert entry["content_preview"] == "x" * 500

    def test_short_content_not_padded_or_truncated(self):
        items = [_item("mem-short", tags=["agent:x"], layer="session", content="ab")]
        payload = build_graph_payload(items, preview_width=240)
        assert payload["memories"]["mem-short"]["content_preview"] == "ab"


# --------------------------------------------------------------------------- #
# T06 — schema-version guard
# --------------------------------------------------------------------------- #
class TestSchemaVersionGuard:
    def test_mismatch_raises_value_error_with_schema_version_in_message(
        self, small_items, monkeypatch
    ):
        """When build_domain_graph returns a graph with schema_version != 1,
        build_graph_payload must raise ValueError with 'schema_version' in the
        message text.
        """
        original = cohesion.build_domain_graph

        def fake_build(items):
            g = original(items)
            # DomainGraph is frozen; rebuild with the bumped version.
            return cohesion.DomainGraph(
                schema_version=99,
                domains=g.domains,
                relationships=g.relationships,
                generated_at=g.generated_at,
            )

        monkeypatch.setattr(graphview.cohesion, "build_domain_graph", fake_build)
        with pytest.raises(ValueError, match="schema_version"):
            build_graph_payload(small_items)


# --------------------------------------------------------------------------- #
# T07 / T08 / T09 — render_html invariants
# --------------------------------------------------------------------------- #
class TestRenderHtml:
    def test_includes_graph_data_script_with_parseable_json(self, small_items):
        payload = build_graph_payload(small_items)
        html = render_html(payload)
        m = re.search(
            r'<script id="graph-data" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert m is not None, "graph-data script block missing from rendered HTML"
        parsed = json.loads(m.group(1))
        assert parsed == payload

    def test_includes_canvas_renderer_script(self, small_items):
        """Anti-regression: the vendored renderer must remain in the template."""
        payload = build_graph_payload(small_items)
        html = render_html(payload)
        # Stable substring from the template skeleton — pinned in design-spec §8.
        assert '<canvas id="graph"' in html
        assert "getElementById('graph-data')" in html

    def test_placeholder_substituted_exactly_once(self, small_items):
        payload = build_graph_payload(small_items)
        html = render_html(payload)
        assert "__GRAPH_DATA_JSON__" not in html

    def test_template_path_override_is_respected(self, tmp_path):
        custom = tmp_path / "tmpl.html"
        custom.write_text("BEFORE __GRAPH_DATA_JSON__ AFTER", encoding="utf-8")
        payload = build_graph_payload([])
        html = render_html(payload, template_path=custom)
        # Substitution happened against the custom template, default template
        # contents are not present.
        assert html.startswith("BEFORE ")
        assert html.endswith(" AFTER")
        assert "__GRAPH_DATA_JSON__" not in html
        # The embedded JSON in this custom template can still be parsed back.
        middle = html[len("BEFORE "):-len(" AFTER")]
        assert json.loads(middle) == payload


# --------------------------------------------------------------------------- #
# T10 / T11 — write_graph_html roundtrip + mkdir-parents branch
# --------------------------------------------------------------------------- #
class TestWriteGraphHtml:
    def test_writes_file_returns_absolute_path_and_contents_match(
        self, small_items, tmp_path
    ):
        out = tmp_path / "g.html"
        returned = write_graph_html(small_items, out)
        assert returned == out.resolve()
        contents = out.read_text(encoding="utf-8")
        # The file must contain a graph-data block whose embedded JSON is
        # well-formed and carries our items' domains. Calling
        # build_graph_payload again would stamp a fresh ``generated_at`` so
        # we cannot equality-compare strings across two builds; instead we
        # inspect the embedded payload.
        m = re.search(
            r'<script id="graph-data" type="application/json">(.*?)</script>',
            contents,
            re.DOTALL,
        )
        assert m is not None
        embedded = json.loads(m.group(1))
        assert embedded["schema_version"] == 1
        assert len(embedded["domains"]) >= 1

    def test_creates_missing_parent_directories(self, small_items, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "g.html"
        assert not nested.parent.exists()
        write_graph_html(small_items, nested)
        assert nested.exists()
        # The parents-mkdir branch was exercised; output remains valid HTML.
        contents = nested.read_text(encoding="utf-8")
        assert '<script id="graph-data" type="application/json">' in contents

    def test_passes_preview_width_and_full_through(self, long_content_items, tmp_path):
        out = tmp_path / "g.html"
        write_graph_html(long_content_items, out, preview_width=10)
        contents = out.read_text(encoding="utf-8")
        m = re.search(
            r'<script id="graph-data" type="application/json">(.*?)</script>',
            contents,
            re.DOTALL,
        )
        parsed = json.loads(m.group(1))
        assert parsed["memories"]["mem-long"]["content_preview"] == "x" * 10 + "..."

        out2 = tmp_path / "g2.html"
        write_graph_html(long_content_items, out2, full=True)
        contents2 = out2.read_text(encoding="utf-8")
        m2 = re.search(
            r'<script id="graph-data" type="application/json">(.*?)</script>',
            contents2,
            re.DOTALL,
        )
        parsed2 = json.loads(m2.group(1))
        assert parsed2["memories"]["mem-long"]["content_preview"] == "x" * 500

    def test_template_path_override_propagates(self, small_items, tmp_path):
        custom = tmp_path / "tmpl.html"
        custom.write_text("X __GRAPH_DATA_JSON__ Y", encoding="utf-8")
        out = tmp_path / "g.html"
        write_graph_html(small_items, out, template_path=custom)
        contents = out.read_text(encoding="utf-8")
        assert contents.startswith("X ")
        assert contents.endswith(" Y")


# --------------------------------------------------------------------------- #
# Schema-version footer / generated_at integrity
# --------------------------------------------------------------------------- #
class TestPayloadIntegrity:
    def test_generated_at_is_isoformat_utc(self, small_items):
        payload = build_graph_payload(small_items)
        ga = payload["generated_at"]
        # Either "+00:00" suffix or "Z" — design-spec just requires a
        # parseable iso string from DomainGraph.to_dict() (which uses
        # datetime.now(tz=utc).isoformat()).
        assert "+00:00" in ga or ga.endswith("Z")
