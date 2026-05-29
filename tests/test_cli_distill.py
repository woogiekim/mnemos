"""CLI integration tests for ``mnemos distill`` (Issue #84)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_cli_compact.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy_cfg = {
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy_cfg))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def _capture(
    runner: CliRunner, cli, content: str, *, layer: str, tag: str
) -> str:
    """Capture one tagged item and return its id."""
    result = runner.invoke(
        cli, ["capture", "--layer", layer, "--content", content, "--tag", tag]
    )
    assert result.exit_code == 0, result.output
    match = _UUID_RE.search(result.output)
    assert match, f"no uuid in capture output: {result.output!r}"
    return match.group(0)


def _seed_domain(runner: CliRunner, cli) -> list[str]:
    return [
        _capture(runner, cli, "backend api one", layer="project", tag="agent:backend"),
        _capture(runner, cli, "backend api two", layer="global", tag="agent:backend"),
    ]


def _seed_policy(runner: CliRunner, cli) -> list[str]:
    return [
        _capture(runner, cli, "no push one", layer="session", tag="constraint:no-push"),
        _capture(runner, cli, "no push two", layer="project", tag="constraint:no-push"),
    ]


# ---------------------------------------------------------------------------
# domains review / apply
# ---------------------------------------------------------------------------

class TestDistillDomains:
    def test_review_prints_plan_and_writes_nothing(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        _seed_domain(runner, cli)

        before = set(repo_root.rglob("*.md"))
        result = runner.invoke(cli, ["distill", "domains", "review"])
        after = set(repo_root.rglob("*.md"))

        assert result.exit_code == 0, result.output
        assert "would be created" in result.output
        assert before == after

    def test_review_json_format(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        _seed_domain(runner, cli)
        result = runner.invoke(cli, ["distill", "domains", "review", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "plans" in payload
        assert any(p["kind"] == "domain" for p in payload["plans"])

    def test_review_empty_store_friendly_line(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "domains", "review"])
        assert result.exit_code == 0, result.output
        assert "no domains to distill" in result.output

    def test_review_layer_filter(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        _seed_domain(runner, cli)
        result = runner.invoke(
            cli, ["distill", "domains", "review", "--layer", "global"]
        )
        assert result.exit_code == 0, result.output

    def test_apply_writes_artifacts_and_appends_distilled_into(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        source_ids = _seed_domain(runner, cli)

        result = runner.invoke(cli, ["distill", "domains", "apply"])
        assert result.exit_code == 0, result.output
        assert "distilled:" in result.output
        assert "sources (layer=" in result.output

        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        for sid in source_ids:
            src = gw._store.read(sid)
            assert src.get("distilled_into")

    def test_apply_empty_store_is_noop(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "domains", "apply"])
        assert result.exit_code == 0, result.output
        assert "nothing to distill" in result.output

    def test_apply_twice_skips_second_time(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _seed_domain(runner, cli)
        first = runner.invoke(cli, ["distill", "domains", "apply"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(cli, ["distill", "domains", "apply"])
        assert second.exit_code == 0, second.output
        assert "exists — skipped" in second.output
        assert "applied 0 domain distillation(s)" in second.output


# ---------------------------------------------------------------------------
# policies review / apply
# ---------------------------------------------------------------------------

class TestDistillPolicies:
    def test_review_prints_plan(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        result = runner.invoke(cli, ["distill", "policies", "review"])
        assert result.exit_code == 0, result.output
        assert "would be created" in result.output

    def test_review_json(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        result = runner.invoke(cli, ["distill", "policies", "review", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert any(p["kind"] == "policy" for p in payload["plans"])

    def test_review_empty_store_friendly_line(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "policies", "review"])
        assert result.exit_code == 0, result.output
        assert "no policies to distill" in result.output

    def test_apply_writes_policy_artifacts(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        result = runner.invoke(cli, ["distill", "policies", "apply"])
        assert result.exit_code == 0, result.output
        assert "distilled:" in result.output
        assert "applied 1 policy distillation(s)" in result.output

    def test_apply_empty_store_is_noop(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "policies", "apply"])
        assert result.exit_code == 0, result.output
        assert "nothing to distill" in result.output

    def test_apply_twice_skips(self, runner: CliRunner, cli_with_repo) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        runner.invoke(cli, ["distill", "policies", "apply"])
        second = runner.invoke(cli, ["distill", "policies", "apply"])
        assert second.exit_code == 0, second.output
        assert "exists — skipped" in second.output


# ---------------------------------------------------------------------------
# cohesion (standalone aggregate_policy_cohesion exposure)
# ---------------------------------------------------------------------------

class TestDistillCohesion:
    def test_cohesion_text_exposes_themes(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        result = runner.invoke(cli, ["distill", "cohesion"])
        assert result.exit_code == 0, result.output
        assert "constraint:no-push" in result.output
        assert "recurrence=2" in result.output

    def test_cohesion_json_exposes_aggregate(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        result = runner.invoke(cli, ["distill", "cohesion", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        themes = {c["theme"] for c in payload["cohesion"]}
        assert "constraint:no-push" in themes
        # to_dict() shape from PolicyCohesion is exposed verbatim.
        first = payload["cohesion"][0]
        assert {"theme", "member_ids", "layers", "recurrence", "suggested_layer"} <= set(first)

    def test_cohesion_empty_store_friendly_line(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "cohesion"])
        assert result.exit_code == 0, result.output
        assert "no policy cohesion themes" in result.output

    def test_cohesion_writes_nothing(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        _seed_policy(runner, cli)
        before = set(repo_root.rglob("*.md"))
        result = runner.invoke(cli, ["distill", "cohesion"])
        after = set(repo_root.rglob("*.md"))
        assert result.exit_code == 0, result.output
        assert before == after


# ---------------------------------------------------------------------------
# restore-source
# ---------------------------------------------------------------------------

class TestDistillRestoreSource:
    def test_restore_source_prints_content_and_back_pointer(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        source_ids = _seed_domain(runner, cli)
        apply = runner.invoke(cli, ["distill", "domains", "apply"])
        assert apply.exit_code == 0, apply.output

        result = runner.invoke(cli, ["distill", "restore-source", source_ids[0]])
        assert result.exit_code == 0, result.output
        assert "---" in result.output
        assert "distilled_into:" in result.output
        assert "backend api" in result.output

    def test_restore_source_unknown_id_errors(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["distill", "restore-source", "no-such-id"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()
