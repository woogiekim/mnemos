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
# #90 — Memory-first tab order + default-active Memory tab
# --------------------------------------------------------------------------- #
class TestUi90MemoryFirstTab:
    """Issue #90 swaps the tab order so Memory is the first tab in DOM AND
    the default-active tab on load. Graph stays second, Policy Cohesion third.
    The JS ``tabs = { graph, memory, policy }`` map and the ``showTab(...)``
    function are unchanged — only the DOM order and the initial ``aria-selected`` /
    ``class="tab active"`` placement move.
    """

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "alpha", tag="agent:backend")
        _capture_one(runner, cli, "global", "beta", tag="agent:frontend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    def test_memory_button_is_first_in_nav(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        pos_btn_memory = html.find('id="btn-memory"')
        pos_btn_graph = html.find('id="btn-graph"')
        pos_btn_policy = html.find('id="btn-policy"')
        assert pos_btn_memory != -1
        assert pos_btn_graph != -1
        assert pos_btn_policy != -1
        assert pos_btn_memory < pos_btn_graph < pos_btn_policy, (
            "nav button order must be Memory → Graph → Policy "
            f"(got memory@{pos_btn_memory}, graph@{pos_btn_graph}, "
            f"policy@{pos_btn_policy})"
        )

    def test_memory_button_is_aria_selected_true_on_load(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert (
            '<button id="btn-memory" role="tab" aria-selected="true"' in html
        ), "Memory button must be aria-selected=true on initial render"
        assert (
            '<button id="btn-graph" role="tab" aria-selected="false"' in html
        ), "Graph button must be aria-selected=false on initial render"
        assert (
            '<button id="btn-policy" role="tab" aria-selected="false"' in html
        ), "Policy button must be aria-selected=false on initial render"

    def test_tab_memory_section_appears_before_tab_graph_section(
        self, runner, cli_with_repo, tmp_path
    ):
        """The hard-gate inline structural check — the position of
        ``id="tab-memory"`` must be less than the position of
        ``id="tab-graph"`` in the rendered HTML string."""
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        pos_tab_memory = html.find('id="tab-memory"')
        pos_tab_graph = html.find('id="tab-graph"')
        pos_tab_policy = html.find('id="tab-policy"')
        assert pos_tab_memory != -1
        assert pos_tab_graph != -1
        assert pos_tab_policy != -1
        assert pos_tab_memory < pos_tab_graph, (
            "#tab-memory must appear before #tab-graph in DOM order "
            f"(got memory@{pos_tab_memory}, graph@{pos_tab_graph})"
        )
        assert pos_tab_graph < pos_tab_policy, (
            "#tab-graph must appear before #tab-policy in DOM order"
        )

    def test_memory_section_carries_tab_active_class_on_load(
        self, runner, cli_with_repo, tmp_path
    ):
        """``class="tab active"`` moves from #tab-graph onto #tab-memory so the
        Memory tab is the visible panel on first load. The Graph section keeps
        just ``class="tab"`` and is hidden until the user clicks the Graph
        button."""
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert (
            '<section id="tab-memory" class="tab active"' in html
        ), "Memory section must carry class='tab active' on initial render"
        assert (
            '<section id="tab-graph" class="tab"' in html
        ), "Graph section must carry class='tab' (no 'active') on initial render"
        # And the active class must NOT appear on the Graph section anymore.
        assert (
            '<section id="tab-graph" class="tab active"' not in html
        ), "Graph section must not be the active tab on initial render"

    def test_initial_active_tab_is_memory_and_sidebar_is_visible(
        self, runner, cli_with_repo, tmp_path
    ):
        """Structural mirror of the pywebview probe (no real pywebview required).
        Asserts ``active_tab_initial == 'tab-memory'`` AND that the Memory
        sidebar is present inside the same active section — i.e. when the
        page loads, the user immediately sees the domain sidebar without
        having to click anything.
        """
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Active tab on initial render is exactly #tab-memory.
        # Find the unique "tab active" section and check its id.
        marker = 'class="tab active"'
        pos = html.find(marker)
        assert pos != -1, "no .tab.active section in rendered HTML"
        # Walk backward to the <section id="...".
        section_open = html.rfind("<section", 0, pos)
        assert section_open != -1
        section_head = html[section_open:pos + len(marker)]
        assert 'id="tab-memory"' in section_head, (
            f"the .tab.active section must be #tab-memory; got: {section_head!r}"
        )
        # And the #domain-sidebar lives inside that same active section
        # (i.e. appears AFTER the opening <section id="tab-memory" ...>
        # and BEFORE the next </section>).
        sidebar_pos = html.find('id="domain-sidebar"', section_open)
        next_section_close = html.find("</section>", section_open)
        assert sidebar_pos != -1
        assert next_section_close != -1
        assert section_open < sidebar_pos < next_section_close, (
            "#domain-sidebar must be inside the initially-active #tab-memory "
            "section so it is visible on first load (offsetParent !== null)"
        )

    def test_showtab_memory_still_targets_memory_tab(
        self, runner, cli_with_repo, tmp_path
    ):
        """Cross-filter regression guard: the graph-node click handler calls
        ``showTab('memory')`` after applyCrossFilter; the JS ``tabs`` map and
        ``showTab`` function are unchanged by #90, so this contract still holds.
        """
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert 'showTab("memory")' in html or "showTab('memory')" in html, (
            "showTab('memory') call must remain wired for the graph→memory "
            "cross-filter flow"
        )


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


class TestFuturisticWhiteTheme91:
    """Issue #91 — the rendered template carries the new white-themed,
    futuristic visual language. These assertions are intentionally light
    structural substring checks that lock in the load-bearing theme tokens
    (palette accent, glass header, pill-shaped tabs, tabular numerics).
    They do not pin exhaustive CSS — only enough that an accidental revert
    of the #91 theme is caught at test time."""

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "x", tag="agent:backend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    def test_palette_accent_token_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The deep electric-blue accent is the futuristic primary.
        assert "--accent: #2563eb" in html

    def test_near_white_app_background_token_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Slate-50-family near-white background.
        assert "--bg: #f8fafc" in html

    def test_header_glass_backdrop_filter_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Subtle glass effect on the top bar.
        assert "backdrop-filter" in html

    def test_tab_buttons_are_pill_shaped(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Pill-shaped nav buttons — full-radius pills.
        assert "border-radius: 9999px" in html

    def test_tabular_numerics_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Counts use monospaced figures so columns align.
        assert "font-variant-numeric: tabular-nums" in html


# --------------------------------------------------------------------------- #
# #92 — Content readability: drill-down full, 2-line wrap, per-row expand
# --------------------------------------------------------------------------- #
class TestUi92ContentReadability:
    """Issue #92 stops the aggressive ``...`` truncation experience:

    1. Drill-down ``#dd-content`` renders the FULL memory content via
       ``mem.content_full`` (always present, additive payload field).
    2. List rows render a 2-line ``-webkit-line-clamp: 2`` preview with a
       soft bottom fade instead of a single-line ellipsis.
    3. Each row gains a "Show more" toggle button that expands an inline
       ``.mem-content-full`` block to show the untruncated content.
    4. ``--preview-width`` default raised 240 -> 480 so even the preview
       isn't truncated as eagerly before the 2-line wrap kicks in.
    """

    def _render(self, runner, cli, out_path):
        _capture_one(
            runner, cli, "global",
            "This is a fairly long memory content body. " * 8,
            tag="agent:backend",
        )
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    # ---- Payload contract -------------------------------------------------
    def test_payload_emits_content_full_per_memory(
        self, runner, cli_with_repo, tmp_path
    ):
        out = tmp_path / "ui.html"
        self._render(runner, cli_with_repo, out)
        payload = _read_payload(out)
        memories = payload["memory"]["memories"]
        assert memories, "expected at least one memory"
        for mem in memories:
            assert "content_full" in mem, "content_full missing from memory"
            assert isinstance(mem["content_full"], str)

    def test_full_flag_makes_content_equal_content_full(
        self, runner, cli_with_repo, tmp_path
    ):
        """In ``--full`` mode the list preview already shows the
        untruncated content (``mem.content == mem.content_full``), so a
        per-row expand toggle adds nothing visible — but the toggle
        still works because the JS reads ``mem.content_full || mem.content``
        and the 2-line clamp keeps the row visually compact until the
        operator opts in. The drill-down panel reads the same field, so
        the FULL content always renders there regardless of ``--full``."""
        long = "y" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "ui.html"
        result = runner.invoke(cli_with_repo, ["ui", "--output", str(out), "--full"])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        memories = payload["memory"]["memories"]
        # Find our captured memory.
        target = next((m for m in memories if m["content_full"] == long), None)
        assert target is not None, "captured memory missing from payload"
        # ``--full`` makes content == content_full (no truncation).
        assert target["content"] == long
        assert target["content_full"] == long

    # ---- CSS hooks (rendered HTML) ---------------------------------------
    def test_rendered_html_carries_mem_content_preview_css(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert ".mem-content-preview" in html
        # 2-line clamp is the load-bearing visual rule.
        assert "-webkit-line-clamp: 2" in html
        assert "-webkit-box-orient: vertical" in html
        # Bottom fade — both prefixed and unprefixed forms for portability.
        assert "mask-image: linear-gradient(to bottom, #000 70%, transparent 100%)" in html

    def test_rendered_html_carries_mem_expand_btn_css(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert ".mem-expand-btn" in html
        assert ".mem-expand-btn:hover" in html

    def test_rendered_html_carries_mem_content_full_css(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert ".mem-content-full" in html
        # The .shown modifier is what the JS toggle flips.
        assert ".mem-content-full.shown" in html
        # white-space: pre-wrap so newlines in content render correctly.
        assert "white-space: pre-wrap" in html

    # ---- JS bindings ------------------------------------------------------
    def test_renderlist_creates_preview_and_expand_btn(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The renderMemoryList() loop constructs both elements per row.
        assert 'previewEl.className = "mem-content-preview"' in html
        assert 'expandBtn.className = "mem-expand-btn"' in html
        assert 'fullEl.className = "mem-content-full"' in html
        # The expand button toggles .shown on the sibling full-content block.
        assert 'fullEl.classList.toggle("shown")' in html

    def test_renderlist_reads_content_full_for_full_block(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The expanded block prefers content_full and falls back to content.
        assert "mem.content_full || mem.content" in html

    def test_drilldown_reads_content_full_first(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The drilldown setter prefers content_full over content.
        assert 'dd-content"' in html
        # The literal expression the template uses to wire the drilldown.
        assert "mem.content_full || mem.content" in html

    # ---- CLI defaults ----------------------------------------------------
    def test_preview_width_default_is_480(self):
        """The ``mnemos ui`` command's ``--preview-width`` default jumped
        240 -> 480 (issue #92) so even the preview isn't truncated as
        eagerly before the 2-line wrap kicks in. Inspect the Click
        command's options directly so the assertion does not depend on
        ``--help`` wrapping or layout."""
        from core.cli import memory_ui

        opt = next(
            p for p in memory_ui.params
            if isinstance(p, __import__("click").Option)
            and "--preview-width" in p.opts
        )
        assert opt.default == 480

    def test_preview_width_help_text_mentions_480(self, runner, cli_with_repo):
        result = runner.invoke(cli_with_repo, ["ui", "--help"])
        assert result.exit_code == 0, result.output
        assert "480" in result.output

    def test_inspect_preview_width_default_unchanged_at_240(self):
        """Issue #92 only retunes the ``mnemos ui`` LIST default. The
        legacy ``mnemos inspect`` and ``mnemos graph`` commands keep
        their 240 default — they don't have the 2-line wrap, so a
        wider preview would make their drill-down even more cluttered."""
        from core.cli import cli as root_cli

        inspect_cmd = root_cli.get_command(None, "inspect")
        assert inspect_cmd is not None
        opt = next(
            p for p in inspect_cmd.params
            if isinstance(p, __import__("click").Option)
            and "--preview-width" in p.opts
        )
        assert opt.default == 240

    # ---- Backward compat / regression guards -----------------------------
    def test_existing_regression_guards_still_pass(
        self, runner, cli_with_repo, tmp_path
    ):
        """The #83/#85/#86/#90/#91 structural guards listed in the #92
        plan must remain intact after the content-readability edits."""
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # #83 — placeholder appears exactly once (template substitution
        # didn't re-fire).
        assert html.count("__UI_DATA_JSON__") == 0
        # #83 — canvas + ui-data tag + mem-id-pill all present.
        assert '<canvas id="graph"' in html
        assert 'id="ui-data"' in html
        assert "mem-id-pill" in html
        # #85 — display_title binding intact.
        assert "mem.display_title" in html
        # #91 — futuristic white theme tokens intact.
        assert "--accent: #2563eb" in html
        # #90 — Memory tab is the first nav button + active on load.
        # Pinning the literal substring is the cheapest contract check.
        assert 'aria-selected="true"' in html


# --------------------------------------------------------------------------- #
# #93 — Drill-down Related mini-graph
# --------------------------------------------------------------------------- #
class TestUi93DrilldownRelatedGraph:
    """Issue #93 — when a memory is opened in the drill-down (#86/#92),
    a small "Related" graph appears inside the drill-down showing the
    memory's local neighborhood (containing domains + sampled siblings).
    Reuses the existing payload (graph.domains + member_ids); no new
    payload field is added. Reuses the existing cross-filter wiring so
    domain-node clicks behave identically to the main graph."""

    def _render(self, runner, cli, out_path):
        _capture_one(runner, cli, "global", "alpha", tag="agent:backend")
        _capture_one(runner, cli, "global", "beta", tag="agent:frontend")
        _capture_one(runner, cli, "global", "gamma", tag="agent:backend")
        result = runner.invoke(cli, ["ui", "--output", str(out_path)])
        assert result.exit_code == 0, result.output
        return out_path.read_text(encoding="utf-8")

    # ---- New DOM hooks ----------------------------------------------------
    def test_related_canvas_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The drill-down gains a NEW canvas with a dedicated id that must
        # NOT clash with the load-bearing id="graph" packaging assertion.
        assert '<canvas id="dd-related-graph"' in html

    def test_related_panel_markup_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The Related panel wraps the new canvas + empty placeholder
        # inside a .related-panel class so it picks up panel styling.
        assert "related-panel" in html
        assert 'data-panel="related"' in html
        # Title is literal so the source grep stays stable.
        assert "<h3>Related</h3>" in html

    def test_related_empty_placeholder_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The placeholder shown when the opened memory has zero
        # containing domains (e.g. a freshly captured untagged memory).
        assert 'id="dd-related-empty"' in html
        assert "No related domain" in html

    # ---- JS function presence + wiring -----------------------------------
    def test_build_related_graph_function_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The function name is the contract — assert as a substring so a
        # stylistic rewrite that keeps the name still passes.
        assert "buildRelatedGraph" in html
        # And it MUST be invoked from showDrilldown (the open hook), so
        # opening any memory fires the related-graph render.
        assert "buildRelatedGraph(mem)" in html

    def test_related_graph_reuses_cross_filter(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # Domain-node clicks inside the related mini-graph must funnel
        # through the SAME applyCrossFilter the main graph + sidebar +
        # policy table all share. The literal substring is the wire.
        assert "applyCrossFilter(n.domain.member_ids," in html

    def test_related_graph_resolves_siblings_via_memory_by_id(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The {id -> memory} map is built once on payload load and
        # used by buildRelatedGraph to resolve sampled sibling ids.
        assert "memoryById" in html
        # Sibling-node click re-opens the drill-down for the chosen
        # sibling — the JS calls showDrilldown(full) with the resolved
        # memory object.
        assert "showDrilldown(full)" in html

    # ---- CSS hooks (rendered HTML) ---------------------------------------
    def test_related_panel_css_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The canvas inherits surface-elev tone + the bottom of the panel
        # picks up the same 10px border-radius as the .panel container.
        assert ".related-panel canvas" in html
        assert ".related-empty" in html

    # ---- Regression: load-bearing tokens must remain intact -------------
    def test_placeholder_substitution_still_singleton(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The new canvas MUST NOT cause the __UI_DATA_JSON__ placeholder
        # to leak into the rendered HTML (the #83 root-cause regression).
        assert html.count("__UI_DATA_JSON__") == 0

    def test_main_graph_canvas_still_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # The new <canvas id="dd-related-graph"> MUST NOT clash with the
        # load-bearing <canvas id="graph"> packaging assertion — they
        # are different ids. Pin both substrings explicitly.
        assert '<canvas id="graph"' in html
        assert '<canvas id="dd-related-graph"' in html

    def test_ui_data_tag_and_pill_still_present(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        # #83/#85 — ui-data tag + mem-id-pill survive the #93 edit.
        assert 'id="ui-data"' in html
        assert "mem-id-pill" in html
        assert "mem.display_title" in html

    def test_no_innerhtml_sink_introduced(
        self, runner, cli_with_repo, tmp_path
    ):
        """The Related mini-graph must build every DOM mutation via
        createElement + textContent (or canvas drawing primitives) — no
        innerHTML assignment sink introduced. #86 contract preserved."""
        html = self._render(runner, cli_with_repo, tmp_path / "ui.html")
        assert "innerHTML" not in html

    # ---- Template-source guards (no extra placeholder leak) -------------
    def test_template_source_placeholder_singleton(self):
        """#93 must not re-introduce the #83 root-cause regression
        (placeholder mentioned more than once in the template source)."""
        from importlib.resources import files
        tpl = files("core.templates").joinpath("ui.html").read_text("utf-8")
        assert tpl.count("__UI_DATA_JSON__") == 1
        assert (
            '<script id="ui-data" type="application/json">__UI_DATA_JSON__</script>'
            in tpl
        )

    def test_template_source_has_related_canvas_id(self):
        """The new drill-down canvas id must be present in the template
        SOURCE (not only in the rendered HTML). Pinning both layers
        makes the contract regression-detectable from either side."""
        from importlib.resources import files
        tpl = files("core.templates").joinpath("ui.html").read_text("utf-8")
        assert 'id="dd-related-graph"' in tpl
        assert "buildRelatedGraph" in tpl
