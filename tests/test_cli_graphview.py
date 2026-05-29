"""CLI integration tests for ``mnemos graph`` (issue #68).

Derived from design-spec.md Section 6 and Section 9 (CLI surface + C01-C10).
Tests pin: exit codes, stdout marker, default --no-open behavior, --open
opt-in, --full bypass, --preview-width truncation, --layer / --limit
forwarding, empty-store path, error-on-schema-mismatch.

Like the rest of the suite, these tests run under ``filterwarnings=["error"]``
so the default ``--no-open`` is essential: invoking ``webbrowser.open`` in a
CI sandbox is the most common source of ResourceWarning failures.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from core import cohesion


# --------------------------------------------------------------------------- #
# Fixtures — mirror tests/test_cli.py shape so the existing patterns hold
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

    policy = {
        "layers": {
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
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
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
        r'<script id="graph-data" type="application/json">(.*?)</script>',
        text,
        re.DOTALL,
    )
    assert m, f"graph-data script block missing in {html_path}"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# C01 / C09 — happy path: writes file, exits 0, stdout marker
# --------------------------------------------------------------------------- #
class TestGraphHappyPath:
    def test_writes_file_exits_zero_and_prints_marker(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        # Capture at least one memory so the graph is non-empty.
        _capture_one(
            runner, cli_with_repo, "global", "Hello world", tag="agent:backend"
        )
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert f"[mnemos] wrote {out}" in result.output


# --------------------------------------------------------------------------- #
# C02 / C03 — --no-open default and --open opt-in
# --------------------------------------------------------------------------- #
class TestGraphOpenFlag:
    def test_no_open_default_does_not_invoke_browser(
        self, runner, cli_with_repo, repo_root, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "x", tag="agent:backend")
        calls = []
        monkeypatch.setattr(
            "core.cli.webbrowser.open",
            lambda *a, **kw: calls.append((a, kw)) or True,
        )
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert calls == []  # browser must not have been invoked

    def test_explicit_open_invokes_browser_once(
        self, runner, cli_with_repo, repo_root, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "x", tag="agent:backend")
        calls = []
        monkeypatch.setattr(
            "core.cli.webbrowser.open",
            lambda *a, **kw: calls.append((a, kw)) or True,
        )
        out = tmp_path / "g.html"
        result = runner.invoke(
            cli_with_repo, ["graph", "--output", str(out), "--open"]
        )
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        opened_path = calls[0][0][0]
        assert str(out) in opened_path


# --------------------------------------------------------------------------- #
# C04 / C05 — --full bypass and --preview-width truncation
# --------------------------------------------------------------------------- #
class TestGraphPreviewFlags:
    def test_full_flag_embeds_full_content(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        long = "y" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "g.html"
        result = runner.invoke(
            cli_with_repo, ["graph", "--output", str(out), "--full"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        # At least one memory must carry the full content.
        previews = [m["content_preview"] for m in payload["memories"].values()]
        assert any(p == long for p in previews), previews

    def test_preview_width_truncates(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        long = "z" * 500
        _capture_one(runner, cli_with_repo, "global", long, tag="agent:backend")
        out = tmp_path / "g.html"
        result = runner.invoke(
            cli_with_repo,
            ["graph", "--output", str(out), "--preview-width", "10"],
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        previews = [m["content_preview"] for m in payload["memories"].values()]
        assert any(p == "z" * 10 + "..." for p in previews), previews


# --------------------------------------------------------------------------- #
# C06 / C07 — --layer and --limit forwarding
# --------------------------------------------------------------------------- #
class TestGraphLayerAndLimitFilters:
    def test_layer_filter_restricts_payload(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        _capture_one(runner, cli_with_repo, "global", "G1", tag="agent:backend")
        _capture_one(runner, cli_with_repo, "session", "S1", tag="agent:backend")
        out = tmp_path / "g.html"
        result = runner.invoke(
            cli_with_repo,
            ["graph", "--output", str(out), "--layer", "global"],
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        layers_seen = {m["layer"] for m in payload["memories"].values()}
        assert layers_seen == {"global"}

    def test_limit_caps_source_items(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        for i in range(3):
            _capture_one(
                runner, cli_with_repo, "global", f"item-{i}", tag="agent:backend"
            )
        out = tmp_path / "g.html"
        result = runner.invoke(
            cli_with_repo, ["graph", "--output", str(out), "--limit", "1"]
        )
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert len(payload["memories"]) == 1


# --------------------------------------------------------------------------- #
# C08 — empty store
# --------------------------------------------------------------------------- #
class TestGraphEmptyStore:
    def test_empty_store_writes_empty_payload_exits_zero(
        self, runner, cli_with_repo, repo_root, tmp_path
    ):
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code == 0, result.output
        payload = _read_payload(out)
        assert payload["schema_version"] == 1
        assert payload["domains"] == []
        assert payload["relationships"] == []
        assert payload["memories"] == {}


# --------------------------------------------------------------------------- #
# C10 — schema version mismatch surfaces as non-zero exit with stderr message
# --------------------------------------------------------------------------- #
class TestGraphSchemaMismatch:
    def test_schema_mismatch_exits_nonzero_with_stderr_message(
        self, runner, cli_with_repo, repo_root, tmp_path, monkeypatch
    ):
        _capture_one(runner, cli_with_repo, "global", "x", tag="agent:backend")
        # Force the graphview module's view of build_domain_graph to return a
        # bumped schema_version so build_graph_payload raises ValueError.
        from core import graphview

        original = cohesion.build_domain_graph

        def fake_build(items):
            g = original(items)
            return cohesion.DomainGraph(
                schema_version=99,
                domains=g.domains,
                relationships=g.relationships,
                generated_at=g.generated_at,
            )

        monkeypatch.setattr(graphview.cohesion, "build_domain_graph", fake_build)
        out = tmp_path / "g.html"
        result = runner.invoke(cli_with_repo, ["graph", "--output", str(out)])
        assert result.exit_code != 0, result.output
        combined = (result.output or "") + (
            "" if result.stderr_bytes is None else result.stderr_bytes.decode("utf-8")
        )
        assert "schema_version" in combined.lower() or "error" in combined.lower()


# --------------------------------------------------------------------------- #
# R01-adjacent — additive only: existing CLI commands still respond
# --------------------------------------------------------------------------- #
class TestGraphAdditiveOnly:
    def test_existing_list_command_unaffected(self, runner, cli_with_repo):
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output

    def test_capture_command_unaffected(self, runner, cli_with_repo):
        result = runner.invoke(
            cli_with_repo, ["capture", "--layer", "global", "--content", "still works"]
        )
        assert result.exit_code == 0, result.output
