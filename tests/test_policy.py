"""Tests for PolicyEngine."""
import pytest
from pathlib import Path
import yaml
import time


@pytest.fixture
def policy_yaml(tmp_path):
    """Create a minimal policy.yaml for testing."""
    config = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {
                    "age_hours": 0.001,  # ~3.6 seconds for fast tests
                    "access_count": 1,
                    "quality_score": 0.3,
                },
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {
                    "age_hours": 1.0,
                    "access_count": 2,
                    "quality_score": 0.5,
                },
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {
                    "age_hours": 4.0,
                    "access_count": 3,
                    "quality_score": 0.6,
                },
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {
                    "age_hours": 24.0,
                    "access_count": 5,
                    "quality_score": 0.7,
                },
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {
                    "age_hours": 0,
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
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml.dump(config))
    return policy_file


@pytest.fixture
def engine(policy_yaml):
    """Return a PolicyEngine backed by the test policy.yaml."""
    from mnemos.policy import PolicyEngine
    return PolicyEngine(policy_path=str(policy_yaml))


def make_item(layer="ephemeral", stage="stored", age_hours=1.0,
              access_count=5, quality_score=0.9):
    """Helper: build an item dict."""
    import datetime
    created = datetime.datetime.utcnow() - datetime.timedelta(hours=age_hours)
    return {
        "id": "test-item-001",
        "layer": layer,
        "stage": stage,
        "created_at": created.isoformat() + "Z",
        "access_count": access_count,
        "quality_score": quality_score,
        "tags": [],
    }


class TestValidateCapture:
    def test_validate_capture_valid(self, engine):
        """Valid layer and content must not raise."""
        engine.validate_capture(layer="ephemeral", item={"content": "hello"})

    def test_validate_capture_invalid_layer(self, engine):
        """Unknown layer must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        with pytest.raises(PolicyViolationError):
            engine.validate_capture(layer="nonexistent", item={"content": "x"})

    def test_validate_capture_all_valid_layers(self, engine):
        """All known layers must be accepted."""
        for layer in ["ephemeral", "working", "session", "project", "global"]:
            engine.validate_capture(layer=layer, item={"content": "x"})


class TestValidatePromote:
    def test_validate_promote_passes_when_eligible(self, engine):
        """Item meeting all thresholds must not raise."""
        item = make_item(layer="ephemeral", age_hours=1.0, access_count=5, quality_score=0.9)
        engine.validate_promote(item=item, target_layer="working")

    def test_validate_promote_insufficient_age(self, engine):
        """Item that is too young must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        # ephemeral promotion requires age_hours >= 0.001 hours = ~3.6 seconds
        # Create item that is 0 seconds old (just created)
        import datetime
        item = make_item(layer="ephemeral", age_hours=0, access_count=5, quality_score=0.9)
        # Override with a future-ish created_at to ensure it's truly too young
        item["created_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        with pytest.raises(PolicyViolationError, match="age"):
            engine.validate_promote(item=item, target_layer="working")

    def test_validate_promote_insufficient_access_count(self, engine):
        """Item with too few accesses must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item = make_item(layer="ephemeral", age_hours=1.0, access_count=0, quality_score=0.9)
        with pytest.raises(PolicyViolationError, match="access_count"):
            engine.validate_promote(item=item, target_layer="working")

    def test_validate_promote_insufficient_quality(self, engine):
        """Item with low quality score must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item = make_item(layer="ephemeral", age_hours=1.0, access_count=5, quality_score=0.1)
        with pytest.raises(PolicyViolationError, match="quality_score"):
            engine.validate_promote(item=item, target_layer="working")

    def test_validate_promote_wrong_target_layer(self, engine):
        """Promoting to non-next layer must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item = make_item(layer="ephemeral", age_hours=1.0, access_count=5, quality_score=0.9)
        with pytest.raises(PolicyViolationError):
            engine.validate_promote(item=item, target_layer="global")


class TestValidateForget:
    def test_validate_forget_not_archived(self, engine):
        """Forgetting a non-archived item must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item = make_item(stage="stored")
        with pytest.raises(PolicyViolationError, match="archived"):
            engine.validate_forget(item=item)

    def test_validate_forget_archived_passes(self, engine):
        """Forgetting an archived item must not raise."""
        item = make_item(stage="archived")
        engine.validate_forget(item=item)


class TestCheckLifecycleStage:
    def test_check_lifecycle_stage_passes(self, engine):
        """Item in required stage must not raise."""
        item = make_item(stage="stored")
        engine.check_lifecycle_stage(item=item, required_stage="stored")

    def test_check_lifecycle_stage_fails(self, engine):
        """Item not in required stage must raise PolicyViolationError."""
        from mnemos.policy import PolicyViolationError
        item = make_item(stage="stored")
        with pytest.raises(PolicyViolationError):
            engine.check_lifecycle_stage(item=item, required_stage="archived")
