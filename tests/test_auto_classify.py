"""Tests for auto-classify-on-capture feature (Issue #26).

Covers:
  - MemoryGateway.auto_classify() assigns at least one tag
  - MemoryGateway.capture() runs auto_classify by default
  - MemoryGateway.capture(no_classify=True) skips auto_classify
  - CLI capture assigns tag automatically
  - CLI capture --no-classify skips auto-classification
  - CLI classify --all backfills tags on all items
  - CLI classify --all --untagged skips already-tagged items
"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal repo structure for testing."""
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
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated", "classified"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")

    return tmp_path


@pytest.fixture
def gateway(repo_root):
    from core.gateway import MemoryGateway
    return MemoryGateway(repo_root=str(repo_root))


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


# ---------------------------------------------------------------------------
# Tests: MemoryGateway.auto_classify()
# ---------------------------------------------------------------------------

class TestAutoClassify:
    def test_auto_classify_assigns_at_least_one_tag(self, gateway):
        """auto_classify must assign at least one tag — even for generic text."""
        item_id = gateway.capture(
            layer="global",
            content="some random note with no specific keyword",
            run_id="test-run",
            no_classify=True,  # write without tags first
        )
        assert item_id is not None

        new_tags = gateway.auto_classify(item_id=item_id, content="some random note with no specific keyword")
        # At minimum the catch-all "general" tag should appear
        item = gateway._store.read(item_id)
        assert len(item.get("tags", [])) >= 1

    def test_auto_classify_architecture_keyword(self, gateway):
        """Content mentioning 'architecture' should receive the 'architecture' tag."""
        item_id = gateway.capture(
            layer="global",
            content="Architecture decision: we use hexagonal pattern",
            run_id="test-run",
            no_classify=True,
        )
        gateway.auto_classify(
            item_id=item_id,
            content="Architecture decision: we use hexagonal pattern",
        )
        item = gateway._store.read(item_id)
        assert "architecture" in item.get("tags", [])

    def test_auto_classify_decision_keyword(self, gateway):
        """Content with a decision keyword should get the 'decision' tag."""
        item_id = gateway.capture(
            layer="global",
            content="We decided to use SQLite for FTS storage.",
            run_id="test-run",
            no_classify=True,
        )
        gateway.auto_classify(
            item_id=item_id,
            content="We decided to use SQLite for FTS storage.",
        )
        item = gateway._store.read(item_id)
        assert "decision" in item.get("tags", [])

    def test_auto_classify_general_fallback(self, gateway):
        """Content with no recognized keyword should receive 'general' tag."""
        item_id = gateway.capture(
            layer="global",
            content="zzz xyz aaa bbb ccc",
            run_id="test-run",
            no_classify=True,
        )
        gateway.auto_classify(item_id=item_id, content="zzz xyz aaa bbb ccc")
        item = gateway._store.read(item_id)
        assert "general" in item.get("tags", [])

    def test_auto_classify_does_not_duplicate_existing_tags(self, gateway):
        """auto_classify should not duplicate tags that are already present."""
        item_id = gateway.capture(
            layer="global",
            content="Architecture decision for the project",
            run_id="test-run",
            no_classify=True,
        )
        # First classify
        gateway.auto_classify(
            item_id=item_id, content="Architecture decision for the project"
        )
        item_after_first = gateway._store.read(item_id)
        tags_after_first = item_after_first.get("tags", [])

        # Second classify — same content
        gateway.auto_classify(
            item_id=item_id, content="Architecture decision for the project"
        )
        item_after_second = gateway._store.read(item_id)
        tags_after_second = item_after_second.get("tags", [])

        # Tags must not grow on the second call
        assert sorted(tags_after_first) == sorted(tags_after_second)

    def test_auto_classify_returns_new_tags_list(self, gateway):
        """auto_classify returns the list of tags it actually added."""
        item_id = gateway.capture(
            layer="global",
            content="zzz xyz aaa bbb ccc",
            run_id="test-run",
            no_classify=True,
        )
        new_tags = gateway.auto_classify(item_id=item_id, content="zzz xyz aaa bbb ccc")
        assert isinstance(new_tags, list)
        assert len(new_tags) >= 1


# ---------------------------------------------------------------------------
# Tests: capture() auto-classify integration
# ---------------------------------------------------------------------------

class TestCaptureAutoClassify:
    def test_capture_auto_classifies_by_default(self, gateway):
        """capture() should auto-classify (add at least one tag) by default."""
        item_id = gateway.capture(
            layer="global",
            content="I decided to use this architecture pattern",
            run_id="test-run",
        )
        assert item_id is not None
        item = gateway._store.read(item_id)
        assert len(item.get("tags", [])) >= 1

    def test_capture_no_classify_skips_classification(self, gateway):
        """capture(no_classify=True) must not add auto-classify tags."""
        item_id = gateway.capture(
            layer="global",
            content="A completely untagged note for testing",
            run_id="test-run",
            no_classify=True,
        )
        assert item_id is not None
        item = gateway._store.read(item_id)
        # With no_classify=True and no explicit tags, tags should be empty
        assert item.get("tags", []) == []

    def test_capture_explicit_tags_preserved_after_auto_classify(self, gateway):
        """Explicit --tag values must survive auto-classification."""
        item_id = gateway.capture(
            layer="global",
            content="Some content about architecture and decisions",
            run_id="test-run",
            tags=["my-custom-tag"],
        )
        assert item_id is not None
        item = gateway._store.read(item_id)
        tags = item.get("tags", [])
        assert "my-custom-tag" in tags
        # Auto-classify may have added more, but custom tag is preserved
        assert len(tags) >= 1

    def test_capture_duplicate_still_returns_none(self, gateway):
        """Dedup logic must still work when auto-classify is active."""
        content = "unique dedup test content for auto-classify"
        first_id = gateway.capture(
            layer="global", content=content, run_id="run-dedup"
        )
        second_id = gateway.capture(
            layer="global", content=content, run_id="run-dedup"
        )
        assert first_id is not None
        assert second_id is None  # dedup should block the second capture


# ---------------------------------------------------------------------------
# Tests: CLI capture --no-classify
# ---------------------------------------------------------------------------

class TestCliCaptureNoClassify:
    def test_cli_capture_adds_tag_by_default(self, runner, cli_with_repo, repo_root):
        """CLI capture without --no-classify should result in tagged item."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "Architecture decision: use hexagonal pattern"],
        )
        assert result.exit_code == 0, result.output

        # Find the captured item and verify it has tags
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        items = gw.list_all(layers=["global"])
        assert len(items) >= 1
        # At least one item should have at least one tag
        has_tag = any(len(item.get("tags", [])) >= 1 for item in items)
        assert has_tag, f"Expected at least one tagged item; got: {items}"

    def test_cli_capture_no_classify_skips_tags(self, runner, cli_with_repo, repo_root):
        """CLI capture --no-classify should not add auto-classify tags."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "Architecture decision: use hexagonal pattern", "--no-classify"],
        )
        assert result.exit_code == 0, result.output

        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        items = gw.list_all(layers=["global"])
        assert len(items) >= 1
        # Item should have no tags (none were passed explicitly)
        for item in items:
            assert item.get("tags", []) == []


# ---------------------------------------------------------------------------
# Tests: CLI classify --all --untagged
# ---------------------------------------------------------------------------

class TestCliClassifyBackfill:
    def test_classify_all_backfills_untagged_items(self, runner, cli_with_repo, repo_root):
        """classify --all should auto-classify every item in the store."""
        # Create two items without auto-classify
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "Architecture decision", "--no-classify"],
        )
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "We decided to use SQLite", "--no-classify"],
        )

        # Backfill
        result = runner.invoke(cli_with_repo, ["classify", "--all"])
        assert result.exit_code == 0, result.output
        assert "classified" in result.output

        # All items should now have tags
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        items = gw.list_all(layers=["global"])
        assert len(items) >= 2
        for item in items:
            assert len(item.get("tags", [])) >= 1, (
                f"Item {item.get('item_id')} has no tags after backfill"
            )

    def test_classify_all_untagged_skips_already_tagged(self, runner, cli_with_repo, repo_root):
        """classify --all --untagged must skip items that already have a tag."""
        # Create one already-tagged item and one untagged item
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "Tagged note about workflow and testing", "--tag", "manual-tag"],
        )
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "Untagged note about bugs and errors", "--no-classify"],
        )

        result = runner.invoke(cli_with_repo, ["classify", "--all", "--untagged"])
        assert result.exit_code == 0, result.output
        # Output should mention "skipped (already tagged)"
        assert "skipped" in result.output.lower()

    def test_classify_all_empty_store_is_harmless(self, runner, cli_with_repo):
        """classify --all on an empty store should not error."""
        result = runner.invoke(cli_with_repo, ["classify", "--all"])
        assert result.exit_code == 0, result.output
        assert "classified" in result.output

    def test_classify_single_item_still_works(self, runner, cli_with_repo, repo_root):
        """classify <ITEM_ID> --tag still works for single-item classification."""
        # Capture first
        capture_result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content",
             "A note to classify manually", "--no-classify"],
        )
        assert capture_result.exit_code == 0, capture_result.output

        # Extract item_id from captured: line
        item_id = None
        for line in capture_result.output.splitlines():
            if line.startswith("captured:"):
                item_id = line.split(":", 1)[1].strip()
                break
        assert item_id is not None, f"Could not parse item_id from: {capture_result.output}"

        classify_result = runner.invoke(
            cli_with_repo,
            ["classify", item_id, "--tag", "my-test-tag"],
        )
        assert classify_result.exit_code == 0, classify_result.output
        assert "classified" in classify_result.output

    def test_classify_without_item_id_or_all_errors(self, runner, cli_with_repo):
        """classify with no ITEM_ID and no --all should exit non-zero."""
        result = runner.invoke(cli_with_repo, ["classify", "--tag", "some-tag"])
        assert result.exit_code != 0

    def test_classify_single_without_tag_errors(self, runner, cli_with_repo):
        """classify <ITEM_ID> without --tag should exit non-zero."""
        result = runner.invoke(cli_with_repo, ["classify", "some-fake-id"])
        assert result.exit_code != 0
