"""CLI integration tests for ``mnemos ui`` (issue #83).

Mirrors ``tests/test_cli_inspect.py``'s fixture shape. The default path launches
a pywebview desktop window — tests MUST mock the launcher so no GUI opens and no
event loop blocks (``filterwarnings=["error"]`` + 100% coverage). The ``--output``
path is headless and must never import pywebview.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


# --------------------------------------------------------------------------- #
# Fixtures — mirror test_cli_inspect.py exactly
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo_root(tmp_path):
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy_cfg = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy_cfg))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


def _capture_one(runner, cli, layer, content, tag=None):
    args = ["capture", "--layer", layer, "--content", content]
    if tag:
        args += ["--tag", tag]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    return result


def _read_payload(html_path: Path) -> dict:
    text = html_path.read_text(encoding="utf-8")
    m = re.search(
        r'<script id="ui-data" type="application/json">(.*?)</script>',
        text,
        re.DOTALL,
    )
    assert m, f"ui-data script block missing in {html_path}"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# --output — headless, writes HTML, never imports webview
# --------------------------------------------------------------------------- #
class TestUiOutputPath:
    def test_output_writes_html_with_required_tokens(
        self, runner, cli_with_repo, tmp_path
    ):
        _capture_one(runner, cli_with_repo, "global", "hello", tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert out.stat().st_size > 0
        text = out.read_text(encoding="utf-8")
        assert 'id="ui-data"' in text
        assert '<canvas id="graph"' in text
        assert f"[mnemos] wrote {out.resolve()}" in result.output

    def test_output_path_does_not_import_webview(
        self, runner, cli_with_repo, tmp_path
    ):
        sys.modules.pop("webview", None)
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert "webview" not in sys.modules, "--output path must not import webview"


# --------------------------------------------------------------------------- #
# Default path — launches pywebview window (mocked, no GUI block)
# --------------------------------------------------------------------------- #
class TestUiDefaultLaunchPath:
    def test_default_invokes_launch_app(
        self, runner, cli_with_repo, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "hi", tag="agent:backend")
        calls = []
        monkeypatch.setattr(
            "core.unifiedview.launch_app",
            lambda html, *, title="mnemos": calls.append((title, len(html))),
        )
        result = runner.invoke(cli_with_repo, ["ui"])
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        title, html_len = calls[0]
        assert title == "mnemos UI"
        assert html_len > 0

    def test_missing_extra_default_exits_non_zero_with_hint(
        self, runner, cli_with_repo, monkeypatch
    ):
        from core.unifiedview import PywebviewNotInstalled

        def boom(html, *, title="mnemos"):
            raise PywebviewNotInstalled(
                "pywebview is not installed. Install the UI extra with: "
                "pip install 'mnemos[ui]'  — or render to a file with: "
                "mnemos ui --output ./mnemos-ui.html"
            )

        monkeypatch.setattr("core.unifiedview.launch_app", boom)
        result = runner.invoke(cli_with_repo, ["ui"])
        assert result.exit_code != 0
        assert "mnemos[ui]" in result.output


# --------------------------------------------------------------------------- #
# Flag plumbing — edge-density knobs reach the payload
# --------------------------------------------------------------------------- #
class TestUiFlagPlumbing:
    def test_edge_density_flags_reach_payload(self, runner, cli_with_repo, tmp_path):
        _capture_one(runner, cli_with_repo, "global", "a", tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(
            cli_with_repo,
            [
                "ui",
                "--output",
                str(out),
                "--max-edges-per-node",
                "3",
                "--edge-weight-threshold",
                "0.2",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        ed = payload["graph"]["edge_density"]
        assert ed["max_edges_per_node"] == 3
        assert ed["edge_weight_threshold"] == 0.2

    def test_layer_filter_restricts_payload(self, runner, cli_with_repo, tmp_path):
        _capture_one(runner, cli_with_repo, "global", "G", tag="agent:backend")
        _capture_one(runner, cli_with_repo, "session", "S", tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(
            cli_with_repo, ["ui", "--output", str(out), "--layer", "global"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        layers_seen = {m["layer"] for m in payload["memory"]["memories"]}
        assert layers_seen == {"global"}

    def test_limit_caps_source_items(self, runner, cli_with_repo, tmp_path):
        for i in range(3):
            _capture_one(runner, cli_with_repo, "global", f"item-{i}", tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out), "--limit", "1"])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert len(payload["memory"]["memories"]) == 1

    def test_full_flag_embeds_full_content(self, runner, cli_with_repo, tmp_path):
        long = "y" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out), "--full"])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        contents = [m["content"] for m in payload["memory"]["memories"]]
        assert any(c == long for c in contents)

    def test_preview_width_truncates(self, runner, cli_with_repo, tmp_path):
        long = "z" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(
            cli_with_repo, ["ui", "--output", str(out), "--preview-width", "10"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        contents = [m["content"] for m in payload["memory"]["memories"]]
        assert any(c == "z" * 10 + "..." for c in contents)


# --------------------------------------------------------------------------- #
# Empty store
# --------------------------------------------------------------------------- #
class TestUiEmptyStore:
    def test_empty_store_writes_valid_payload(self, runner, cli_with_repo, tmp_path):
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out)])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert payload["schema_version"] == 1
        assert payload["memory"]["memories"] == []
        assert payload["policy_cohesion"]["clusters"] == []


# --------------------------------------------------------------------------- #
# #85 — display_title propagates to the unified Memory tab
# --------------------------------------------------------------------------- #
class TestUiDisplayTitle:
    def test_payload_emits_display_title_for_every_memory(
        self, runner, cli_with_repo, tmp_path
    ):
        _capture_one(
            runner, cli_with_repo, "global", "Recurring theme: ports-and-adapters",
            tag="agent:backend",
        )
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out)])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        memories = payload["memory"]["memories"]
        assert memories, "expected at least one memory"
        titles = [m["display_title"] for m in memories]
        assert all(isinstance(t, str) for t in titles)
        assert any(t == "Recurring theme: ports-and-adapters" for t in titles)

    def test_rendered_html_binds_display_title_as_primary_heading(
        self, runner, cli_with_repo, tmp_path
    ):
        _capture_one(
            runner, cli_with_repo, "global", "Recurring theme: ports-and-adapters",
            tag="agent:backend",
        )
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out)])
        assert result.exit_code == 0, result.output
        html = out.read_text(encoding="utf-8")
        # mem-id heading is now bound to mem.display_title; id has a pill.
        assert "mem-id-pill" in html, "secondary id pill class missing"
        assert "mem.display_title" in html, "display_title not referenced"
        # The #83 lesson: __UI_DATA_JSON__ MUST appear only once (the
        # load-bearing script tag) — never reintroduced in a comment.
        assert html.count("__UI_DATA_JSON__") == 0, (
            "placeholder leaked into rendered HTML — substitution went wrong"
        )

    def test_ui_template_only_contains_placeholder_in_load_bearing_tag(self):
        """The template SOURCE must mention __UI_DATA_JSON__ exactly once,
        inside the load-bearing ``<script id="ui-data" …>`` tag. The #83
        regression was caused by a second mention inside a comment that
        re-fired the substitution; this test pins the lesson."""
        from importlib.resources import files
        tpl = files("core.templates").joinpath("ui.html").read_text("utf-8")
        assert tpl.count("__UI_DATA_JSON__") == 1
        # And it lives inside the load-bearing script tag.
        assert (
            '<script id="ui-data" type="application/json">__UI_DATA_JSON__</script>'
            in tpl
        )


# --------------------------------------------------------------------------- #
# Additive only — existing commands still respond
# --------------------------------------------------------------------------- #
class TestUiAdditiveOnly:
    def test_existing_inspect_command_unaffected(self, runner, cli_with_repo, tmp_path):
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output

    def test_existing_graph_command_unaffected(self, runner, cli_with_repo, tmp_path):
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# #86 — Memory-tab Domain sidebar
# --------------------------------------------------------------------------- #
class TestUiDomainSidebar:
    """The Memory tab gains a left-hand sidebar that lists every domain
    plus a pinned "All memories" row. The sidebar reuses the existing
    cross-filter function (no new mechanism)."""

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "alpha", tag="agent:backend")
        _capture_one(runner, cli, "global", "beta", tag="agent:frontend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    def test_sidebar_aside_and_role_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert 'id="domain-sidebar"' in html
        assert 'role="list"' in html

    def test_pinned_all_memories_row_attribute(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Pinned row carries data-domain-row="__all__" as the stable
        # marker the UI uses (and tests/probes look for).
        assert 'data-domain-row="__all__"' in html

    def test_sidebar_funnels_through_existing_cross_filter(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The sidebar row click calls applyCrossFilter(d.member_ids, …)
        # — the SAME function the existing graph node click + policy
        # cluster click both funnel through. Pinning the literal
        # substring makes the contract regression-detectable.
        assert "applyCrossFilter(d.member_ids," in html
        # And clearFilter() is still wired (pinned-row "All memories"
        # uses it).
        assert "clearFilter()" in html

    def test_sidebar_does_not_break_existing_columns(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # All three Memory-tab columns must keep min-width: 0 so the
        # flex row stays scrollable inside the #83 absolute-positioned
        # .tab box.
        assert "#memory-list-col" in html
        assert "#drilldown" in html
        # The sidebar fixes width via flex: 0 0 220px.
        assert "220px" in html


# --------------------------------------------------------------------------- #
# #86 — Obsidian-style interactive Graph tab
# --------------------------------------------------------------------------- #
class TestUiInteractiveGraph:
    """The Graph canvas gains a pointer state machine (pan / drag-pin /
    wheel zoom / hover tooltip / Esc), backed by a rAF + dirty-flag
    loop. All DOM mutations go through createElement + textContent —
    the rendered HTML must not contain any unsafe DOM-assignment sink."""

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "alpha", tag="agent:backend")
        _capture_one(runner, cli, "global", "beta", tag="agent:frontend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    def test_screen_to_world_helper_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert "screenToWorld" in html

    def test_zoom_clamp_constants_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The clamp is exposed via named constants AND their literal
        # values; assert both forms so a stylistic rewrite of one form
        # still keeps the contract pinned.
        assert "ZOOM_MIN" in html
        assert "ZOOM_MAX" in html
        assert "0.2" in html
        assert "5.0" in html

    def test_pointer_capture_used(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # setPointerCapture is the contract for drag continuity even
        # when the cursor leaves the canvas mid-drag.
        assert "setPointerCapture" in html

    def test_pointer_and_wheel_handlers_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The listener strings are stable substrings the test pins so
        # a regression that removes the pointer state machine is
        # caught immediately.
        assert "'pointerdown'" in html
        assert "'wheel'" in html

    def test_tooltip_built_via_create_element_and_text_content(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Tooltip must be a <div> created with createElement and
        # populated via textContent only. Assert the literal
        # createElement('div') seed AND textContent presence.
        assert "createElement('div')" in html or 'createElement("div")' in html
        assert "textContent" in html

    def test_escape_key_handled(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Esc clears focus + filter (or blurs search input). Pin the
        # literal "Escape" key string so the contract is regression-
        # detectable.
        assert "Escape" in html

    def test_inner_html_assignment_sink_is_absent(
        self, runner, cli_with_repo, tmp_path
    ):
        """The rendered HTML must not contain any unsafe DOM-mutation
        sink. The tooltip and the result-list reset both go through
        createElement / removeChild instead."""
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert "innerHTML" not in html


# --------------------------------------------------------------------------- #
# #86 — Regression guards re-asserted in the same module
# --------------------------------------------------------------------------- #
class TestUi86RegressionGuards:
    """The #83 layout fix, the #85 display_title contract, and the
    template-source __UI_DATA_JSON__ singleton must remain green after
    the #86 changes."""

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "x", tag="agent:backend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    def test_canvas_graph_tag_still_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert '<canvas id="graph"' in html

    def test_ui_data_id_still_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert 'id="ui-data"' in html

    def test_mem_id_pill_still_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert "mem-id-pill" in html

    def test_display_title_binding_still_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert "mem.display_title" in html

    def test_template_source_placeholder_singleton(self):
        """The template SOURCE must mention __UI_DATA_JSON__ exactly once
        (the load-bearing script tag). The #83 regression was caused by
        a second mention re-firing the substitution; pinning the count
        in #86 re-asserts that lesson."""
        from importlib.resources import files
        tpl = files("core.templates").joinpath("ui.html").read_text("utf-8")
        assert tpl.count("__UI_DATA_JSON__") == 1
        assert (
            '<script id="ui-data" type="application/json">__UI_DATA_JSON__</script>'
            in tpl
        )
