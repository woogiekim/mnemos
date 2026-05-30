"""Payload / render / launcher unit tests for ``core.unifiedview`` (issue #83).

Canonical pin for the unified wire format the ``ui.html`` template parses and
for the thin pywebview launcher. The suite runs under
``filterwarnings=["error"]`` and ``--cov-fail-under=100``; the launcher is
driven to 100% line coverage WITHOUT opening a real window via fake-``webview``
``sys.modules`` injection (both the import-ok and the ImportError branches).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from core import policy, unifiedview


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def policy_path(tmp_path) -> Path:
    """Minimal policy.yaml carrying the canonical promotion chain."""
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


def _item(item_id: str, *, layer: str = "session", content: str = "hello", tags=None) -> dict:
    return {
        "id": item_id,
        "layer": layer,
        "stage": "stored",
        "content": content,
        "tags": list(tags) if tags else [],
    }


@pytest.fixture
def items() -> list[dict]:
    """A small store: two domains (agent, project) + policy themes."""
    return [
        _item("m1", layer="global", content="a", tags=["agent:backend", "constraint:no-network"]),
        _item("m2", layer="global", content="b", tags=["agent:backend", "decision:use-sqlite"]),
        _item("m3", layer="session", content="c", tags=["agent:frontend"]),
        _item("m4", layer="project", content="d", tags=["preference:tdd"]),
    ]


# --------------------------------------------------------------------------- #
# build_unified_payload — shape + schema stamp
# --------------------------------------------------------------------------- #
class TestUnifiedPayloadShape:
    def test_schema_version_stamp_is_one(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        assert payload["schema_version"] == 1

    def test_top_level_keys_present(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        assert set(payload.keys()) >= {
            "schema_version",
            "generated_at",
            "graph",
            "memory",
            "policy_cohesion",
        }
        assert isinstance(payload["generated_at"], str) and payload["generated_at"]

    def test_payload_is_json_serialisable_no_leaked_objects(self, items, engine):
        """No dataclass instances leak — every value is plain JSON-able."""
        payload = unifiedview.build_unified_payload(items, engine)
        # json.dumps with default=None raises on any non-JSON-able object.
        text = json.dumps(payload, ensure_ascii=False)
        assert "PolicyCohesion" not in text
        assert "Domain(" not in text
        roundtrip = json.loads(text)
        assert roundtrip["schema_version"] == 1

    def test_empty_store_produces_valid_payload(self, engine):
        payload = unifiedview.build_unified_payload([], engine)
        assert payload["schema_version"] == 1
        assert payload["memory"]["memories"] == []
        assert payload["policy_cohesion"]["clusters"] == []
        ed = payload["graph"]["edge_density"]
        assert ed["total_edges"] == 0
        assert ed["shown_edges"] == 0


# --------------------------------------------------------------------------- #
# build_unified_payload — reuse of the existing builders (purity)
# --------------------------------------------------------------------------- #
class TestUnifiedPayloadReuse:
    def test_graph_section_reuses_graphview(self, items, engine, monkeypatch):
        sentinel = {
            "schema_version": 1,
            "domains": [],
            "relationships": [],
            "generated_at": "X",
            "memories": {},
        }
        called = {}

        def fake_build_graph(arg_items, *, preview_width, full):
            called["graph"] = (preview_width, full)
            return dict(sentinel)

        monkeypatch.setattr(unifiedview.graphview, "build_graph_payload", fake_build_graph)
        payload = unifiedview.build_unified_payload(items, engine, preview_width=99, full=True)
        assert called["graph"] == (99, True)
        # graph section is the reused payload + edge_density (no reimplementation).
        assert "edge_density" in payload["graph"]
        assert payload["graph"]["domains"] == []

    def test_memory_section_reuses_inspectview(self, items, engine, monkeypatch):
        called = {}

        def fake_build_inspect(arg_items, eng, preview_width=240, full=False):
            called["memory"] = (preview_width, full, eng is engine)
            return {"schema_version": 1, "memories": [], "layers": [], "stages": []}

        monkeypatch.setattr(unifiedview.inspectview, "build_inspect_payload", fake_build_inspect)
        unifiedview.build_unified_payload(items, engine, preview_width=42, full=True)
        assert called["memory"] == (42, True, True)

    def test_policy_cohesion_section_reuses_cohesion(self, items, engine, monkeypatch):
        called = {}

        def fake_aggregate(arg_items, eng):
            called["policy"] = eng is engine
            return []

        monkeypatch.setattr(unifiedview.cohesion, "aggregate_policy_cohesion", fake_aggregate)
        payload = unifiedview.build_unified_payload(items, engine)
        assert called["policy"] is True
        assert payload["policy_cohesion"]["clusters"] == []

    def test_policy_clusters_are_dicts_with_expected_fields(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        clusters = payload["policy_cohesion"]["clusters"]
        assert clusters, "expected at least one policy cluster from the fixture"
        first = clusters[0]
        assert set(first.keys()) == {
            "theme",
            "member_ids",
            "layers",
            "recurrence",
            "suggested_layer",
        }


# --------------------------------------------------------------------------- #
# _cap_edges_by_node — math + purity
# --------------------------------------------------------------------------- #
def _edge(src: str, tgt: str, weight: float) -> dict:
    return {"source_id": src, "target_id": tgt, "kind": "shares_tag", "weight": weight}


class TestCapEdgesByNode:
    def test_threshold_drops_low_weight_edges(self):
        rels = [_edge("a", "b", 0.1), _edge("a", "c", 0.9), _edge("b", "c", 0.5)]
        shown, total, shown_count = unifiedview._cap_edges_by_node(rels, 99, 0.5)
        # 0.1 edge dropped by the threshold; 0.9 + 0.5 survive.
        weights = sorted(e["weight"] for e in shown)
        assert weights == [0.5, 0.9]
        assert total == 2
        assert shown_count == 2

    def test_per_node_top_n_cap_limits_degree(self):
        # Node "hub" connects to 5 leaves; cap to top-2 by weight.
        rels = [_edge("hub", f"leaf{i}", w) for i, w in enumerate([0.1, 0.2, 0.3, 0.4, 0.5])]
        shown, total, shown_count = unifiedview._cap_edges_by_node(rels, 2, 0.0)
        assert total == 5
        # hub keeps its top-2 incident edges (weights 0.5 and 0.4).
        hub_edges = [e for e in shown if "hub" in (e["source_id"], e["target_id"])]
        kept_weights = sorted(e["weight"] for e in hub_edges)
        assert kept_weights == [0.4, 0.5]
        assert shown_count == len(shown)
        # No node exceeds the per-node degree cap.
        degree: dict[str, int] = {}
        for e in shown:
            for node in (e["source_id"], e["target_id"]):
                degree[node] = degree.get(node, 0) + 1
        assert all(d <= 2 for d in degree.values()), degree

    def test_does_not_mutate_input(self):
        rels = [_edge("a", "b", 0.1), _edge("a", "c", 0.9)]
        snapshot = json.dumps(rels)
        unifiedview._cap_edges_by_node(rels, 1, 0.5)
        assert json.dumps(rels) == snapshot, "input relationships were mutated"

    def test_zero_or_negative_cap_disables_cap(self):
        rels = [_edge("a", f"b{i}", 0.5) for i in range(10)]
        shown, total, shown_count = unifiedview._cap_edges_by_node(rels, 0, 0.0)
        assert total == 10
        assert shown_count == 10
        assert len(shown) == 10

    def test_edge_density_summary_in_payload(self, items, engine):
        payload = unifiedview.build_unified_payload(
            items, engine, max_edges_per_node=2, edge_weight_threshold=0.0
        )
        ed = payload["graph"]["edge_density"]
        assert ed["max_edges_per_node"] == 2
        assert ed["edge_weight_threshold"] == 0.0
        assert ed["shown_edges"] == len(payload["graph"]["relationships"])
        assert ed["total_edges"] >= ed["shown_edges"]

    def test_build_payload_never_mutates_graphview_relationships(self, items, engine):
        """The capped copy must not mutate graphview's own output."""
        raw = unifiedview.graphview.build_graph_payload(items)
        before = json.dumps(raw["relationships"])
        unifiedview.build_unified_payload(items, engine, max_edges_per_node=1)
        after = unifiedview.graphview.build_graph_payload(items)
        assert json.dumps(after["relationships"]) == before


# --------------------------------------------------------------------------- #
# preview_width / full branches
# --------------------------------------------------------------------------- #
class TestPreviewBranches:
    def test_full_embeds_entire_content(self, engine):
        long = "x" * 500
        items = [_item("m1", layer="global", content=long, tags=["agent:backend"])]
        payload = unifiedview.build_unified_payload(items, engine, full=True)
        contents = [m["content"] for m in payload["memory"]["memories"]]
        assert any(c == long for c in contents)

    def test_preview_width_truncates(self, engine):
        long = "z" * 500
        items = [_item("m1", layer="global", content=long, tags=["agent:backend"])]
        payload = unifiedview.build_unified_payload(items, engine, preview_width=10)
        contents = [m["content"] for m in payload["memory"]["memories"]]
        assert any(c == "z" * 10 + "..." for c in contents)


# --------------------------------------------------------------------------- #
# Cross-filter shared-id invariant
# --------------------------------------------------------------------------- #
class TestUnifiedDisplayTitlePropagation:
    """#85 — ``display_title`` is emitted by ``build_inspect_payload`` and
    must round-trip into the unified payload's memory section unchanged."""

    def test_display_title_present_on_every_memory(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        memories = payload["memory"]["memories"]
        assert memories, "fixture expected to produce at least one memory"
        for mem in memories:
            assert "display_title" in mem
            assert isinstance(mem["display_title"], str)

    def test_display_title_derives_from_first_content_line(self, engine):
        item = _item(
            "m1",
            layer="global",
            content="# Recurring theme: ports-and-adapters\n\nbody",
            tags=["agent:backend"],
        )
        payload = unifiedview.build_unified_payload([item], engine)
        first = payload["memory"]["memories"][0]
        assert first["display_title"] == "Recurring theme: ports-and-adapters"


class TestCrossFilterIdSpace:
    def test_graph_member_ids_overlap_memory_ids(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        graph_member_ids = {
            mid
            for d in payload["graph"]["domains"]
            for mid in d["member_ids"]
        }
        memory_ids = {m["id"] for m in payload["memory"]["memories"]}
        assert graph_member_ids, "graph carries no member ids"
        assert memory_ids, "memory carries no ids"
        # The cross-filter target id-space is shared/consistent.
        assert graph_member_ids <= memory_ids


# --------------------------------------------------------------------------- #
# render_html + write_unified_html
# --------------------------------------------------------------------------- #
class TestRenderAndWrite:
    def test_render_html_substitutes_placeholder(self, items, engine):
        payload = unifiedview.build_unified_payload(items, engine)
        html = unifiedview.render_html(payload)
        assert "__UI_DATA_JSON__" not in html, "placeholder left unsubstituted"
        assert '<script id="ui-data"' in html
        assert '<canvas id="graph"' in html
        # The embedded JSON is the payload.
        assert '"schema_version": 1' in html or '"schema_version":1' in html

    def test_render_html_accepts_template_override(self, items, engine, tmp_path):
        tpl = tmp_path / "alt.html"
        tpl.write_text('<x>__UI_DATA_JSON__</x>', encoding="utf-8")
        payload = unifiedview.build_unified_payload(items, engine)
        html = unifiedview.render_html(payload, template_path=tpl)
        assert html.startswith("<x>") and "__UI_DATA_JSON__" not in html

    def test_write_unified_html_writes_valid_file(self, items, engine, tmp_path):
        out = tmp_path / "ui.html"
        written = unifiedview.write_unified_html(items, out, engine)
        assert written == out.resolve()
        text = out.read_text(encoding="utf-8")
        assert text
        assert '<script id="ui-data"' in text
        assert '<canvas id="graph"' in text

    def test_write_unified_html_creates_parent_dirs(self, items, engine, tmp_path):
        out = tmp_path / "nested" / "deep" / "ui.html"
        written = unifiedview.write_unified_html(items, out, engine)
        assert written.exists()


# --------------------------------------------------------------------------- #
# launch_app — 100% coverage via fake-webview injection (no GUI, no loop)
# --------------------------------------------------------------------------- #
class TestLaunchApp:
    @staticmethod
    def _fake_webview(captured: dict) -> types.ModuleType:
        """A fake ``webview`` module whose ``start()`` inspects the live file.

        ``start()`` mirrors the blocking event loop: at the moment it runs the
        temp file must exist and hold the passed HTML. We capture both the URL
        handed to ``create_window`` and the file's existence/content as seen
        *during the session* so the test can assert the file:// + temp-file
        mechanism without opening a real window.
        """
        fake = types.ModuleType("webview")

        def create_window(*args, **kwargs):
            captured["create_window_args"] = args
            captured["create_window_kwargs"] = kwargs
            captured["url"] = kwargs.get("url", args[1] if len(args) > 1 else None)

        def start():
            url = captured["url"]
            # file://<abspath> -> filesystem path
            path = Path(url[len("file://"):])
            captured["existed_during_start"] = path.exists()
            captured["content_during_start"] = (
                path.read_text(encoding="utf-8") if path.exists() else None
            )
            captured["path"] = path

        fake.create_window = MagicMock(side_effect=create_window)
        fake.start = MagicMock(side_effect=start)
        return fake

    def test_happy_path_loads_file_url_and_starts(self, monkeypatch):
        captured: dict = {}
        fake = self._fake_webview(captured)
        monkeypatch.setitem(sys.modules, "webview", fake)

        html = "<html>hi #83</html>"
        unifiedview.launch_app(html, title="My UI")

        # (a) create_window is called with a file:// url (and the right title).
        assert captured["create_window_args"][0] == "My UI"
        assert captured["url"].startswith("file://")
        # No inline html= is passed anymore.
        assert "html" not in captured["create_window_kwargs"]
        # (b) the file the URL points at existed and held the HTML during start().
        assert captured["existed_during_start"] is True
        assert captured["content_during_start"] == html
        # (c) start() was called.
        fake.start.assert_called_once_with()
        # (d) the temp file is cleaned up after launch_app returns.
        assert not captured["path"].exists()

    def test_default_title(self, monkeypatch):
        captured: dict = {}
        fake = self._fake_webview(captured)
        monkeypatch.setitem(sys.modules, "webview", fake)

        unifiedview.launch_app("<html/>")
        assert captured["create_window_args"][0] == "mnemos"
        assert captured["url"].startswith("file://")

    def test_missing_extra_raises_pywebview_not_installed(self, monkeypatch):
        # A None entry in sys.modules makes `import webview` raise ImportError.
        monkeypatch.setitem(sys.modules, "webview", None)
        with pytest.raises(unifiedview.PywebviewNotInstalled) as exc:
            unifiedview.launch_app("<html/>")
        assert "mnemos[ui]" in str(exc.value)

    def test_pywebview_not_installed_is_runtime_error(self):
        assert issubclass(unifiedview.PywebviewNotInstalled, RuntimeError)

    def test_ready_callback_receives_window_before_start(self, monkeypatch):
        """Issue #95 — ``launch_app(ready=...)`` invokes the callback with
        the window object returned by ``create_window`` AFTER that call
        and BEFORE ``webview.start()`` begins blocking. The default
        ``ready=None`` keeps the existing call-sites identical."""
        captured: dict = {}
        fake = types.ModuleType("webview")
        sentinel = object()  # stand-in for the pywebview Window instance.

        # ``create_window`` returns the sentinel; ``start`` flips a flag.
        ordering: list[str] = []
        create_window_mock = MagicMock(side_effect=lambda *a, **kw: (
            ordering.append("create_window") or sentinel
        ))
        start_mock = MagicMock(side_effect=lambda: ordering.append("start"))
        fake.create_window = create_window_mock
        fake.start = start_mock
        monkeypatch.setitem(sys.modules, "webview", fake)

        ready_mock = MagicMock(
            side_effect=lambda w: ordering.append("ready") or captured.setdefault("window", w)
        )
        unifiedview.launch_app("<html/>", ready=ready_mock)

        # (a) ready fired once with the sentinel returned by create_window.
        ready_mock.assert_called_once_with(sentinel)
        assert captured["window"] is sentinel
        # (b) call order: create_window -> ready -> start.
        assert ordering == ["create_window", "ready", "start"], ordering
        # (c) start() ran exactly once (event loop entered).
        start_mock.assert_called_once_with()

    def test_default_ready_is_none_no_callback_fired(self, monkeypatch):
        """Omitting ``ready`` keeps the existing behavior — nothing extra runs."""
        captured: dict = {}
        fake = self._fake_webview(captured)
        monkeypatch.setitem(sys.modules, "webview", fake)
        # No ready kwarg — exercises the ``ready is None`` branch.
        unifiedview.launch_app("<html/>")
        assert captured["url"].startswith("file://")
