"""CLI integration tests for ``mnemos inspect`` (issue #80).

Pinned by ``${TASK_DIR}/context/prd.md`` § Acceptance criteria (all six).
Each AC is anchored by at least one rendered-HTML assertion below.

Tests run under ``filterwarnings=["error"]`` so default ``--no-open`` is
essential (mirrors ``test_cli_graphview.py``'s rationale).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


# --------------------------------------------------------------------------- #
# Fixtures — mirror test_cli_graphview.py shape exactly
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
        r'<script id="inspect-data" type="application/json">(.*?)</script>',
        text,
        re.DOTALL,
    )
    assert m, f"inspect-data script block missing in {html_path}"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# Happy path — file written, exit 0, stdout marker
# --------------------------------------------------------------------------- #
class TestInspectHappyPath:
    def test_writes_file_exits_zero_and_prints_marker(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        _capture_one(runner, cli_with_repo, "global", "Hello world", tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert f"[mnemos] wrote {out}" in result.output


# --------------------------------------------------------------------------- #
# --no-open default + --open opt-in (filterwarnings=error guard)
# --------------------------------------------------------------------------- #
class TestInspectOpenFlag:
    def test_no_open_default_does_not_invoke_browser(
        self, runner, cli_with_repo, repo_root, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "x", tag="agent:backend")
        calls = []
        monkeypatch.setattr(
            "core.cli.webbrowser.open",
            lambda *a, **kw: calls.append((a, kw)) or True,
        )
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_explicit_open_invokes_browser_once(
        self, runner, cli_with_repo, repo_root, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "x", tag="agent:backend")
        calls = []
        monkeypatch.setattr(
            "core.cli.webbrowser.open",
            lambda *a, **kw: calls.append((a, kw)) or True,
        )
        out = tmp_path / "i.html"
        result = runner.invoke(
            cli_with_repo, ["inspect", "--output", str(out), "--open"]
        )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1


# --------------------------------------------------------------------------- #
# --full + --preview-width forwarding
# --------------------------------------------------------------------------- #
class TestInspectPreviewFlags:
    def test_full_flag_embeds_full_content(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        long = "y" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(
            cli_with_repo, ["inspect", "--output", str(out), "--full"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        previews = [m["content"] for m in payload["memories"]]
        assert any(p == long for p in previews), previews
        assert all(m["preview_truncated"] is False for m in payload["memories"])

    def test_preview_width_truncates(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        long = "z" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(
            cli_with_repo,
            ["inspect", "--output", str(out), "--preview-width", "10"],
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        previews = [m["content"] for m in payload["memories"]]
        assert any(p == "z" * 10 + "..." for p in previews), previews
        truncated_flags = [m["preview_truncated"] for m in payload["memories"]]
        assert any(truncated_flags), truncated_flags


# --------------------------------------------------------------------------- #
# --layer + --limit forwarding
# --------------------------------------------------------------------------- #
class TestInspectLayerAndLimit:
    def test_layer_filter_restricts_payload(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        _capture_one(runner, cli_with_repo, "global", "G1", tag="agent:backend")
        _capture_one(runner, cli_with_repo, "session", "S1", tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(
            cli_with_repo,
            ["inspect", "--output", str(out), "--layer", "global"],
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        layers_seen = {m["layer"] for m in payload["memories"]}
        assert layers_seen == {"global"}

    def test_limit_caps_source_items(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        for i in range(3):
            _capture_one(runner, cli_with_repo, "global", f"item-{i}", tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(
            cli_with_repo, ["inspect", "--output", str(out), "--limit", "1"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert len(payload["memories"]) == 1


# --------------------------------------------------------------------------- #
# Empty store
# --------------------------------------------------------------------------- #
class TestInspectEmptyStore:
    def test_empty_store_writes_empty_payload_exits_zero(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert payload["schema_version"] == 1
        assert payload["memories"] == []
        # The layer + stage enums are still embedded even when zero memories
        # exist — that drives the UI's filter checkboxes.
        assert payload["layers"]
        assert payload["stages"]


# --------------------------------------------------------------------------- #
# Six AC anchors — every AC must be observable in the rendered HTML
# --------------------------------------------------------------------------- #
class TestInspectSixAcAnchors:
    """Each test below pins exactly one AC. The HTML template must carry
    the anchor; the CLI must render it; the test must observe it."""

    def _render(self, runner, cli_with_repo, tmp_path):
        _capture_one(runner, cli_with_repo, "global", "anchored", tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        return out.read_text(encoding="utf-8")

    def test_ac1_search_input_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC1: search UI — role=search input must exist for ARIA + tests.
        assert 'role="search"' in html, "AC1: search input missing"

    def test_ac2_content_viewer_scaffold_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC2: content viewer — drill-down panel id must exist.
        assert 'id="drilldown"' in html, "AC2: drill-down content viewer scaffold missing"

    def test_ac3_layer_and_stage_filter_controls(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC3: lifecycle/layer filtering — both control containers visible.
        assert 'id="layer-filters"' in html, "AC3: layer filter controls missing"
        assert 'id="stage-filters"' in html, "AC3: stage filter controls missing"

    def test_ac4_trust_panel_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC4: trust panel — must be in the template, surfacing the
        # layer + quality_score + access_count triple.
        assert 'data-panel="trust"' in html, "AC4: trust panel missing"

    def test_ac5_provenance_panel_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC5: provenance panel — must be in the template.
        assert 'data-panel="provenance"' in html, "AC5: provenance panel missing"

    def test_ac6_lifecycle_panel_present(self, runner, cli_with_repo, tmp_path):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC6: promotion/archive — lifecycle panel must be in the template.
        assert 'data-panel="lifecycle"' in html, "AC6: lifecycle panel missing"

    def test_payload_surfaces_promotable_and_next_layer(
        self, runner, cli_with_repo, tmp_path
    ):
        html = self._render(runner, cli_with_repo, tmp_path)
        # AC6 (payload): the embedded JSON must carry promotable + next_layer
        # so the UI panels have data to bind against.
        m = re.search(
            r'<script id="inspect-data" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert m, "inspect-data block missing"
        payload = json.loads(m.group(1))
        assert payload["memories"], "expected at least one memory"
        first = payload["memories"][0]
        assert "promotable" in first
        assert "next_layer" in first
        assert "archived" in first


# --------------------------------------------------------------------------- #
# Template safety — no network, no eval
# --------------------------------------------------------------------------- #
class TestInspectTemplateSafety:
    def test_no_fetch_xhr_or_eval_in_rendered_html(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        _capture_one(runner, cli_with_repo, "global", "safety", tag="agent:backend")
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        html = out.read_text(encoding="utf-8")
        # No network primitives.
        assert "fetch(" not in html, "rendered HTML must not call fetch()"
        assert "XMLHttpRequest" not in html, "rendered HTML must not use XMLHttpRequest"
        # No eval / Function constructor that turns strings into code.
        assert "eval(" not in html, "rendered HTML must not use eval()"
        # The Function constructor pattern (string-to-code).
        assert "new Function(" not in html, "rendered HTML must not use new Function()"


# --------------------------------------------------------------------------- #
# #85 — display_title is emitted and surfaced as the primary row heading
# --------------------------------------------------------------------------- #
class TestInspectDisplayTitle:
    def test_payload_emits_display_title_for_every_memory(
        self, runner, cli_with_repo, tmp_path
    ):
        _capture_one(
            runner, cli_with_repo, "global", "Architecture decision: use SQLite",
            tag="agent:backend",
        )
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert payload["memories"], "expected at least one memory"
        titles = [m["display_title"] for m in payload["memories"]]
        assert all(isinstance(t, str) for t in titles)
        # The captured content's first line is the title.
        assert any(t == "Architecture decision: use SQLite" for t in titles)

    def test_rendered_html_renders_display_title_as_primary_heading(
        self, runner, cli_with_repo, tmp_path
    ):
        _capture_one(
            runner, cli_with_repo, "global", "Architecture decision: use SQLite",
            tag="agent:backend",
        )
        out = tmp_path / "i.html"
        result = runner.invoke(cli_with_repo, ["inspect", "--output", str(out)])
        assert result.exit_code == 0, result.output
        html = out.read_text(encoding="utf-8")
        # The template carries a .mem-title heading and a .mem-sub id row.
        assert "mem-title" in html, "primary heading class missing"
        assert "mem-sub" in html, "secondary id class missing"
        # The renderer reads mem.display_title (the new field) for both the
        # list-row heading and the drill-down title.
        assert "mem.display_title" in html


# --------------------------------------------------------------------------- #
# Additive only — existing commands still respond
# --------------------------------------------------------------------------- #
class TestInspectAdditiveOnly:
    def test_existing_graph_command_unaffected(self, runner, cli_with_repo, tmp_path):
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code == 0, result.output

    def test_existing_list_command_unaffected(self, runner, cli_with_repo):
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
