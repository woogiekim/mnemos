"""Tests for MemoryGateway."""
import pytest
import yaml
from pathlib import Path


@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal repo structure for testing."""
    # Create wiki directories
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    # Create .agent directories
    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    # Create policy.yaml
    policy = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {
                    "age_hours": 0.0,
                    "access_count": 0,
                    "quality_score": 0.0,
                },
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {
                    "age_hours": 0.0,
                    "access_count": 0,
                    "quality_score": 0.0,
                },
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {
                    "age_hours": 0.0,
                    "access_count": 0,
                    "quality_score": 0.0,
                },
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {
                    "age_hours": 0.0,
                    "access_count": 0,
                    "quality_score": 0.0,
                },
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {
                    "age_hours": 0.0,
                    "access_count": 0,
                    "quality_score": 0.0,
                },
            },
        },
        "forget": {"requires_archived": True},
        "archive": {
            "allowed_stages": ["stored", "retrieved", "used", "validated"]
        },
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))

    # Create log files
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")

    return tmp_path


@pytest.fixture
def gateway(repo_root):
    """Return a MemoryGateway pointed at tmp repo."""
    from mnemos.gateway import MemoryGateway
    return MemoryGateway(repo_root=str(repo_root))


class TestCapture:
    def test_capture_stores_item_in_layer(self, gateway, repo_root):
        """Capture should create a markdown file with YAML front-matter."""
        item_id = gateway.capture(
            layer="global",
            content="Hello, mnemos world",
            run_id="run-test",
        )
        assert item_id is not None

        # File should exist somewhere under wiki/global
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_capture_raises_on_invalid_layer(self, gateway):
        """Capture with unknown layer must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        with pytest.raises(PolicyViolationError):
            gateway.capture(layer="invalid_layer", content="test")

    def test_capture_uses_provided_id(self, gateway, repo_root):
        """Capture with explicit id should use that id."""
        item_id = gateway.capture(
            layer="global",
            content="Explicit ID test",
            item_id="explicit-001",
            run_id="run-test",
        )
        assert item_id == "explicit-001"

    def test_capture_front_matter_structure(self, gateway, repo_root):
        """Captured item must have correct YAML front-matter fields."""
        import frontmatter
        item_id = gateway.capture(
            layer="project",
            content="Front-matter test content",
            tags=["test", "qa"],
            run_id="run-test",
        )
        matches = list((repo_root / "wiki" / "projects").glob("*.md"))
        assert len(matches) >= 1
        post = frontmatter.load(str(matches[0]))
        assert post["id"] == item_id
        assert post["layer"] == "project"
        assert "created_at" in post
        assert "access_count" in post
        assert "quality_score" in post


class TestPromote:
    def test_promote_validates_policy(self, gateway, repo_root):
        """Promote should move item to next layer."""
        item_id = gateway.capture(
            layer="ephemeral",
            content="Will be promoted",
            quality_score=0.9,
            run_id="run-promote",
        )
        result_id = gateway.promote(item_id=item_id, run_id="run-promote")
        assert result_id is not None

    def test_promote_to_next_layer(self, gateway, repo_root):
        """Promote moves item from project to global layer."""
        item_id = gateway.capture(
            layer="project",
            content="Promote to global",
            quality_score=0.9,
            run_id="run-test",
        )
        new_id = gateway.promote(item_id=item_id, run_id="run-test")
        # Should now exist in global
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1


class TestForget:
    def test_forget_requires_archived_state(self, gateway):
        """Forget on non-archived item must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item_id = gateway.capture(
            layer="global",
            content="Should not be forgotten directly",
            run_id="run-test",
        )
        with pytest.raises(PolicyViolationError):
            gateway.forget(item_id=item_id)

    def test_forget_after_archive_succeeds(self, gateway, repo_root):
        """Archive then forget should succeed without error."""
        item_id = gateway.capture(
            layer="global",
            content="To be archived and forgotten",
            run_id="run-test",
        )
        gateway.archive(item_id=item_id)
        gateway.forget(item_id=item_id)
        # Item file should no longer exist
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert not any(item_id in m.name for m in matches)


class TestAuditLog:
    def test_all_mutations_are_logged(self, gateway, repo_root):
        """Every mutation (capture, archive) must append to log.jsonl."""
        import json
        log_path = repo_root / "wiki" / "log.jsonl"

        gateway.capture(
            layer="global",
            content="Logging test",
            item_id="log-item-001",
            run_id="run-test",
        )

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["operation"] == "capture"
        assert entry["item_id"] == "log-item-001"

    def test_archive_is_logged(self, gateway, repo_root):
        """Archive operation must appear in log.jsonl."""
        import json
        log_path = repo_root / "wiki" / "log.jsonl"

        item_id = gateway.capture(
            layer="global",
            content="Archive logging test",
            run_id="run-test",
        )
        gateway.archive(item_id=item_id)

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        ops = [json.loads(l)["operation"] for l in lines]
        assert "archive" in ops
