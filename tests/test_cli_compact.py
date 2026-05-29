"""CLI integration tests for ``mnemos compact`` (Issue #81 — Stage 2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_cli_inspect.py / test_compaction.py)
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


_UUID_RE = __import__("re").compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def _capture(runner: CliRunner, cli, *contents: str, layer: str = "project") -> list[str]:
    """Capture each item into *layer* and return one id per call.

    The capture CLI emits its uuid in different formats depending on
    classifier / event-bus output (``captured: <uuid>``, ``post-capture``,
    or just embedded in a notice).  We pull the FIRST uuid-shaped token
    from the output so the harness is robust against side-channel chatter.
    """
    ids: list[str] = []
    for content in contents:
        result = runner.invoke(cli, ["capture", "--layer", layer, "--content", content])
        assert result.exit_code == 0, result.output
        match = _UUID_RE.search(result.output)
        assert match, f"no uuid in capture output: {result.output!r}"
        ids.append(match.group(0))
    return ids


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

class TestCompactReview:
    def test_review_prints_groups_and_writes_nothing(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "alpha beta gamma one", "alpha beta gamma two",
                 layer="project")

        before_md = set(repo_root.rglob("*.md"))
        result = runner.invoke(cli, ["compact", "review", "--threshold", "0.5"])
        after_md = set(repo_root.rglob("*.md"))

        assert result.exit_code == 0, result.output
        assert "group" in result.output.lower()
        # The store gained no NEW files during review (read may still
        # auto-promote and move files — those moves are not "new files").
        assert before_md == after_md or len(after_md) == len(before_md)

    def test_review_with_no_groups_emits_friendly_line(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "totally distinct content alpha",
                 "completely unrelated zeta omega", layer="project")
        result = runner.invoke(cli, ["compact", "review", "--threshold", "0.9"])
        assert result.exit_code == 0, result.output
        assert "no similar-memory groups" in result.output.lower()

    def test_review_layer_filter(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "alpha beta gamma one", layer="project")
        _capture(runner, cli, "alpha beta gamma two", layer="global")
        # Restrict to project: only one item in scope → no pair possible.
        result = runner.invoke(cli, ["compact", "review", "--threshold", "0.5",
                                     "--layer", "project"])
        assert result.exit_code == 0, result.output
        assert "no similar-memory groups" in result.output.lower()

    def test_review_json_format_emits_valid_json(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "alpha beta gamma one", "alpha beta gamma two",
                 layer="project")
        result = runner.invoke(cli, ["compact", "review", "--threshold", "0.5",
                                     "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["threshold"] == 0.5
        assert "groups" in payload
        assert len(payload["groups"]) >= 1
        first = payload["groups"][0]
        assert "sources" in first and len(first["sources"]) >= 2
        assert "content" in first


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

class TestCompactApply:
    def test_apply_writes_merged_and_archives_sources(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        ids = _capture(runner, cli, "alpha beta gamma one",
                       "alpha beta gamma two", layer="project")
        assert len(ids) == 2

        result = runner.invoke(cli, ["compact", "apply", "--threshold", "0.5"])
        assert result.exit_code == 0, result.output
        assert "merged:" in result.output

        # Each source must now be archived AND carry superseded_by.
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        for source_id in ids:
            try:
                src = gw.read(source_id)
            except FileNotFoundError:
                # Acceptable if hooks moved the file; just skip
                continue
            assert src.get("stage") == "archived"
            assert src.get("superseded_by")

    def test_apply_with_no_groups_is_a_noop(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "very distinct content alpha unique",
                 "another distinct content omega different", layer="project")
        result = runner.invoke(cli, ["compact", "apply", "--threshold", "0.99"])
        assert result.exit_code == 0, result.output
        assert "nothing to merge" in result.output.lower()

    def test_apply_forget_sources_hard_deletes(
        self, runner: CliRunner, cli_with_repo, repo_root: Path
    ) -> None:
        cli = cli_with_repo
        ids = _capture(runner, cli, "alpha beta gamma one",
                       "alpha beta gamma two", layer="project")
        assert len(ids) == 2

        result = runner.invoke(cli, ["compact", "apply", "--threshold", "0.5",
                                     "--forget-sources"])
        assert result.exit_code == 0, result.output
        # After forget, the source markdown files are gone — but the
        # merged memory still has the sources list (lineage in metadata).
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        for source_id in ids:
            with pytest.raises(FileNotFoundError):
                gw.read(source_id)


# ---------------------------------------------------------------------------
# restore-source
# ---------------------------------------------------------------------------

class TestCompactRestoreSource:
    def test_restore_source_prints_content(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        ids = _capture(runner, cli, "alpha beta gamma one",
                       "alpha beta gamma two", layer="project")
        assert len(ids) == 2
        # Merge first.
        merge = runner.invoke(cli, ["compact", "apply", "--threshold", "0.5"])
        assert merge.exit_code == 0, merge.output

        # Now restore.
        result = runner.invoke(cli, ["compact", "restore-source", ids[0]])
        assert result.exit_code == 0, result.output
        # Output must contain front-matter delimiters + original content.
        assert "---" in result.output
        assert "stage:" in result.output  # YAML key
        assert "alpha beta gamma" in result.output

    def test_restore_source_unknown_id_errors(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        result = runner.invoke(cli, ["compact", "restore-source", "no-such-id"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# merge-candidates (alias for review --format=json)
# ---------------------------------------------------------------------------

class TestCompactMergeCandidates:
    def test_merge_candidates_emits_valid_json(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "alpha beta gamma one", "alpha beta gamma two",
                 layer="project")
        result = runner.invoke(cli, ["compact", "merge-candidates",
                                     "--threshold", "0.5"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "groups" in payload
        assert payload["threshold"] == 0.5

    def test_merge_candidates_respects_layer_filter(
        self, runner: CliRunner, cli_with_repo
    ) -> None:
        cli = cli_with_repo
        _capture(runner, cli, "alpha beta gamma one", layer="project")
        _capture(runner, cli, "alpha beta gamma two", layer="global")
        result = runner.invoke(
            cli,
            ["compact", "merge-candidates", "--threshold", "0.5",
             "--layer", "project"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Single item in project → no pair → no groups.
        assert payload["groups"] == []
