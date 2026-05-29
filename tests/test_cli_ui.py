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
