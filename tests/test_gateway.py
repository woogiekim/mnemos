"""Tests for MemoryGateway."""
import hashlib
import json
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
    from core.gateway import MemoryGateway
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
        from core.policy import PolicyViolationError
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


def test_success_case_capture_diagnostics_report_privacy_safe_phase_timings(gateway) -> None:
    """TC-010: removing a blocking phase or leaking content must break this test."""
    # given
    private_content = "private capture timing sentinel"
    sut = gateway

    # when
    sut.capture(
        layer="session",
        content=private_content,
        session_id="timing-session",
        no_classify=True,
    )
    diagnostics = sut.last_capture_diagnostics

    # then
    assert diagnostics["duration_ms"] >= 0.0
    assert [phase["name"] for phase in diagnostics["phases"]] == [
        "dedup_lookup",
        "policy_validation",
        "store_write",
        "fts_index",
        "side_effects",
        "classification",
    ]
    assert [phase["name"] for phase in diagnostics["store"]["phases"]] == [
        "sync_before_write",
        "sync_commit",
        "sync_push",
    ]
    assert private_content not in json.dumps(diagnostics)


def test_success_case_capture_enqueues_qmd_refresh_after_canonical_write(
    gateway,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful canonical capture must durably schedule derived indexing."""
    from core.config import QmdConfig
    from core.qmd_queue import WorkerStartResult

    # given
    gateway._qmd_config = QmdConfig(enabled=True)
    monkeypatch.setattr(
        "core.qmd_queue.start_qmd_index_worker",
        lambda _repo_root: WorkerStartResult(
            started=False,
            error_code="worker_start_failed",
        ),
    )

    # when
    item_id = gateway.capture(
        layer="project",
        content="QMD durable refresh integration",
        no_classify=True,
    )

    # then
    assert gateway.peek(str(item_id))["content"] == "QMD durable refresh integration"
    pending = list(
        (repo_root / ".agent" / "state" / "qmd-refresh" / "pending").glob("*.json")
    )
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["reason"] == "capture"


def test_failure_case_qmd_enqueue_failure_never_rolls_back_canonical_capture(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived-index scheduling failure must remain outside canonical success."""
    from core.config import QmdConfig

    # given
    gateway._qmd_config = QmdConfig(enabled=True)
    enqueue_attempts = 0

    def fail_enqueue(**_kwargs) -> None:
        nonlocal enqueue_attempts
        enqueue_attempts += 1
        raise OSError("derived queue unavailable")

    monkeypatch.setattr("core.qmd_queue.enqueue_qmd_refresh", fail_enqueue)

    # when
    item_id = gateway.capture(
        layer="project",
        content="Canonical capture survives QMD failure",
        no_classify=True,
    )

    # then
    assert gateway.peek(str(item_id))["content"] == "Canonical capture survives QMD failure"
    assert enqueue_attempts == 1
    assert gateway.last_qmd_refresh_diagnostics == {
        "enabled": True,
        "operation": "capture",
        "status": "enqueue_failed",
        "queued": False,
        "worker_started": False,
        "error_code": "OSError",
    }
    assert gateway.last_capture_diagnostics["qmd_refresh"] == (
        gateway.last_qmd_refresh_diagnostics
    )
    assert "derived queue unavailable" not in json.dumps(
        gateway.last_capture_diagnostics
    )


def test_success_case_public_memory_mutations_schedule_qmd_refresh(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every public mutation that changes searchable state schedules a refresh."""
    # given
    item_id = gateway.capture(
        layer="project",
        content="QMD mutation lifecycle",
        no_classify=True,
    )
    reasons: list[str] = []
    monkeypatch.setattr(gateway, "_enqueue_qmd_refresh", reasons.append)

    # when
    gateway.update(str(item_id), "QMD mutation lifecycle updated")
    gateway.classify(str(item_id), "qmd")
    gateway.promote(str(item_id), target_layer="global", force=True)
    gateway.demote(str(item_id), target_layer="project")
    gateway.archive(str(item_id))
    gateway.delete(str(item_id))

    # then
    assert reasons == ["update", "classify", "promote", "demote", "archive", "delete"]


def test_success_case_forget_schedules_qmd_refresh(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy-gated hard-delete path also refreshes the derived index."""
    # given
    item_id = gateway.capture(
        layer="project",
        content="QMD forget lifecycle",
        no_classify=True,
    )
    gateway.archive(str(item_id))
    reasons: list[str] = []
    monkeypatch.setattr(gateway, "_enqueue_qmd_refresh", reasons.append)

    # when
    gateway.forget(str(item_id))

    # then
    assert reasons == ["forget"]


def test_success_case_auto_classify_schedules_qmd_refresh_once(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct bulk-classification writes must converge the derived index."""
    # given
    item_id = gateway.capture(
        layer="project",
        content="QMD auto classification lifecycle",
        no_classify=True,
    )
    reasons: list[str] = []
    monkeypatch.setattr(gateway, "_enqueue_qmd_refresh", reasons.append)

    # when
    tags = gateway.auto_classify(
        str(item_id),
        "performance benchmark for deployment workflow",
    )

    # then
    assert tags
    assert reasons == ["auto_classify"]


def test_success_case_capture_coalesces_internal_auto_classify_refresh(
    gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture-owned classification must emit one final derived-index signal."""
    # given
    reasons: list[str] = []
    monkeypatch.setattr(gateway, "_enqueue_qmd_refresh", reasons.append)

    # when
    gateway.capture(
        layer="project",
        content="QMD performance classification capture",
    )

    # then
    assert reasons == ["capture"]


def test_failure_case_capture_diagnostics_keep_store_timings_when_write_fails(gateway) -> None:
    """TC-010: a failing sync/store phase must remain visible to diagnostics."""
    # given
    expected_store_diagnostics = {
        "enabled": True,
        "phases": [{"name": "sync_before_write", "duration_ms": 25.0}],
    }

    class FakeSyncEngine:
        last_write_diagnostics = expected_store_diagnostics

    class FailingStore:
        _sync_engine = FakeSyncEngine()

        def find_by_content_hash(self, _content_hash: str):
            return None

        def write(self, **_kwargs) -> None:
            raise RuntimeError("store unavailable")

    gateway._store = FailingStore()
    sut = gateway

    # when
    with pytest.raises(RuntimeError, match="store unavailable"):
        sut.capture(
            layer="session",
            content="store failure timing",
            session_id="timing-failure",
            no_classify=True,
        )

    # then
    assert sut.last_capture_diagnostics["store"] == expected_store_diagnostics

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

    def test_promote_force_bypasses_age_policy(self, tmp_path):
        """force=True must succeed even when age < threshold (issue #42)."""
        import yaml
        from core.policy import PolicyViolationError

        # Build a repo with a strict age threshold
        wiki = tmp_path / "wiki"
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (wiki / d).mkdir(parents=True)
        agent = tmp_path / ".agent"
        for d in ["runs", "sessions", "state", "reports", "tools"]:
            (agent / d).mkdir(parents=True)
        (agent / "workflows" / "hooks").mkdir(parents=True)

        strict_policy = {
            "layers": {
                "project": {
                    "path_template": "wiki/projects/",
                    "promotes_to": "global",
                    "promotion": {
                        "age_hours": 9999.0,  # impossibly high threshold
                        "access_count": 0,
                        "quality_score": 0.0,
                    },
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
        (wiki / "policy.yaml").write_text(yaml.dump(strict_policy))
        (wiki / "log.md").write_text("# Log\n")
        (wiki / "log.jsonl").write_text("")

        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(tmp_path))

        item_id = gw.capture(layer="project", content="Force-promote me", run_id="run-force")

        # Without force, policy age check must reject it
        with pytest.raises(PolicyViolationError):
            gw.promote(item_id=item_id)

        # With force=True, promotion must succeed despite the age violation
        new_id = gw.promote(item_id=item_id, force=True)
        assert new_id is not None
        matches = list((tmp_path / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1


class TestDemote:
    def test_demote_moves_item_to_lower_layer(self, gateway, repo_root):
        """Demote should move item from project to ephemeral layer."""
        item_id = gateway.capture(
            layer="project",
            content="Will be demoted",
            run_id="run-demote",
        )
        result_id = gateway.demote(item_id=item_id, target_layer="ephemeral", run_id="run-demote")
        assert result_id == item_id
        # Old location should be gone; new location should exist
        matches = list((repo_root / "wiki" / "projects").glob("*.md"))
        assert not any(item_id in m.name for m in matches)

    def test_demote_raises_on_invalid_target_layer(self, gateway):
        """Demote to unknown layer must raise PolicyViolationError."""
        from core.policy import PolicyViolationError
        item_id = gateway.capture(
            layer="global",
            content="Demote to unknown",
            run_id="run-test",
        )
        with pytest.raises(PolicyViolationError):
            gateway.demote(item_id=item_id, target_layer="nonexistent_layer")

    def test_demote_raises_on_same_layer(self, gateway):
        """Demote to current layer must raise PolicyViolationError."""
        from core.policy import PolicyViolationError
        item_id = gateway.capture(
            layer="global",
            content="Demote to same layer",
            run_id="run-test",
        )
        with pytest.raises(PolicyViolationError):
            gateway.demote(item_id=item_id, target_layer="global")


class TestForget:
    def test_forget_requires_archived_state(self, gateway):
        """Forget on non-archived item must raise PolicyViolationError."""
        from core.policy import PolicyViolationError
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


class TestDelete:
    """Tests for MemoryGateway.delete (issue #33)."""

    def test_delete_removes_item(self, gateway, repo_root):
        """delete must remove the item file from the store."""
        item_id = gateway.capture(
            layer="global",
            content="To be deleted unconditionally",
            run_id="run-test",
        )
        gateway.delete(item_id=item_id)
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert not any(item_id in m.name for m in matches)

    def test_delete_no_archive_required(self, gateway, repo_root):
        """delete must succeed on a non-archived item (unlike forget)."""
        from core.policy import PolicyViolationError
        item_id = gateway.capture(
            layer="global",
            content="Non-archived transient item",
            run_id="run-test",
        )
        # forget would raise; delete must not
        gateway.delete(item_id=item_id)
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert not any(item_id in m.name for m in matches)

    def test_delete_nonexistent_raises_file_not_found(self, gateway):
        """delete on a missing item_id must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            gateway.delete(item_id="does-not-exist-xyz-999")

    def test_delete_removes_from_fts_index(self, gateway):
        """delete must remove the item from the FTS index."""
        item_id = gateway.capture(
            layer="global",
            content="unique-fts-delete-probe-content-xyz",
            run_id="run-test",
        )
        gateway.delete(item_id=item_id)
        results = gateway.search("unique-fts-delete-probe-content-xyz")
        assert not any(r.get("item_id") == item_id for r in results)

    def test_delete_purges_capture_queue_artifacts(self, gateway, repo_root):
        """Hard delete must remove current receipts and legacy raw queue payloads."""
        item_id = gateway.capture(
            layer="global",
            content="queue cleanup target",
            run_id="run-test",
            no_classify=True,
        )
        queue_root = repo_root / ".agent" / "state" / "capture-queue"
        artifact_paths = []
        for state in ["pending", "processing", "done", "failed"]:
            path = queue_root / state / f"legacy-{state}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"item_id": item_id, "content": "legacy private text"}),
                encoding="utf-8",
            )
            artifact_paths.append(path)

        gateway.delete(item_id=item_id)

        assert not any(path.exists() for path in artifact_paths)


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
        entries = [json.loads(l) for l in lines]
        # The capture operation must appear in the log; auto_classify may append
        # additional entries after it, so we look in the full entry list.
        capture_entries = [e for e in entries if e["operation"] == "capture" and e["item_id"] == "log-item-001"]
        assert len(capture_entries) >= 1

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


class TestAutoPromotion:
    """Tests for the silent auto-promotion engine (Part 1 of issue #11)."""

    def test_read_auto_promotes_when_eligible(self, gateway, repo_root):
        """Reading an item that meets promotion thresholds should promote it silently."""
        # The test fixture uses zero thresholds for all promotions,
        # so any item meeting access_count=0 and quality_score=0 is immediately eligible.
        item_id = gateway.capture(
            layer="project",
            content="Auto-promote on read test",
            quality_score=0.9,
            run_id="run-test",
        )
        # Item starts in project layer
        matches_project = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project) == 1

        # Reading increments access_count and triggers auto-promotion check.
        # With zero thresholds, the item qualifies immediately → promoted to global.
        gateway.read(item_id)

        # Item should now be in global layer
        matches_global = list((repo_root / "wiki" / "global").glob(f"{item_id}.md"))
        assert len(matches_global) == 1
        # Old location must be gone
        matches_project_after = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project_after) == 0

    def test_search_does_not_auto_promote_eligible_results(self, gateway, repo_root):
        """Search finds eligible results but never promotes them."""
        item_id = gateway.capture(
            layer="project",
            content="auto-promote-search-unique-token",
            quality_score=0.9,
            item_id="auto-promo-search-001",
            run_id="run-test",
        )
        # Confirm item starts in project layer
        matches_project = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project) == 1

        # Search finds the item without access_count mutation or auto-promotion.
        results = gateway.search("auto-promote-search-unique-token")
        found_ids = [r.get("item_id") for r in results]
        assert item_id in found_ids

        # With zero thresholds, search still must not promote.
        matches_global = list((repo_root / "wiki" / "global").glob(f"{item_id}.md"))
        assert len(matches_global) == 0
        matches_project_after = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project_after) == 1

    def test_auto_promotion_does_not_raise_when_already_at_top(self, gateway, repo_root):
        """Reading a global-layer item (no next layer) must not raise any error."""
        item_id = gateway.capture(
            layer="global",
            content="Already at top layer",
            quality_score=0.9,
        )
        # Should complete without exception even though global has no next layer
        item = gateway.read(item_id)
        assert item is not None

    def test_capture_does_not_auto_promote(self, gateway, repo_root):
        """capture() must NOT auto-promote — newly created items stay in their layer."""
        item_id = gateway.capture(
            layer="project",
            content="Newly captured item",
            quality_score=0.9,
            run_id="run-test",
        )
        # Item must still be in project layer immediately after capture
        matches_project = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project) == 1
        matches_global = list((repo_root / "wiki" / "global").glob(f"{item_id}.md"))
        assert len(matches_global) == 0


class TestRecallCore:
    def _hash_memory_files(self, repo_root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(repo_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((repo_root / "wiki").rglob("*.md"))
            if path.name != "log.md"
        }

    def test_peek_reads_without_changing_memory_item(self, gateway):
        item_id = gateway.capture(
            layer="project",
            content="peek immutable token",
            item_id="peek-readonly-001",
            no_classify=True,
        )
        before = gateway._store.read(item_id)

        peeked = gateway.peek(item_id)

        after = gateway._store.read(item_id)
        assert peeked == before
        assert after == before

    def test_recall_is_read_only_and_does_not_auto_promote(self, gateway, repo_root, monkeypatch):
        item_id = gateway.capture(
            layer="project",
            content="readonly recall unique token",
            item_id="recall-readonly-001",
            quality_score=0.9,
            no_classify=True,
        )
        before_item = gateway._store.read(item_id)
        before_hashes = self._hash_memory_files(repo_root)
        calls = []

        def fail_auto_promote(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("recall must not auto-promote")

        monkeypatch.setattr(gateway, "_auto_promote_if_eligible", fail_auto_promote)

        report = gateway.recall(queries=["readonly recall"], selected_limit=3)

        after_item = gateway._store.read(item_id)
        assert [candidate.id for candidate in report.candidates] == [item_id]
        assert [selected.id for selected in report.selected] == [item_id]
        assert calls == []
        assert self._hash_memory_files(repo_root) == before_hashes
        assert after_item["access_count"] == before_item["access_count"]
        assert after_item["stage"] == before_item["stage"]
        assert after_item["layer"] == before_item["layer"]

    def test_recall_deduplicates_multiple_query_results(self, gateway):
        item_id = gateway.capture(
            layer="project",
            content="duplicate alpha beta shared memory",
            item_id="recall-dedup-001",
            no_classify=True,
        )

        report = gateway.recall(queries=["alpha", "beta"], candidate_limit=10, selected_limit=10)

        assert [candidate.id for candidate in report.candidates].count(item_id) == 1
        assert report.candidates[0].matched_queries == ("alpha", "beta")

    def test_recall_applies_project_and_layer_filters(self, gateway):
        project_item = gateway.capture(
            layer="project",
            content="filterable project recall",
            item_id="recall-filter-project",
            extra_metadata={"project_id": "project-a"},
            no_classify=True,
        )
        gateway.capture(
            layer="global",
            content="filterable global recall",
            item_id="recall-filter-global",
            extra_metadata={"project_id": "project-b"},
            no_classify=True,
        )

        report = gateway.recall(
            queries=["filterable recall"],
            layers=["project"],
            project_id="project-a",
            candidate_limit=10,
            selected_limit=10,
        )

        assert [candidate.id for candidate in report.candidates] == [project_item]

    def test_recall_applies_tag_and_status_filters(self, gateway):
        matched = gateway.capture(
            layer="project",
            content="semantic filtered recall",
            item_id="recall-status-matched",
            tags=["backend", "tdd"],
            extra_metadata={"semantic_status": "verified"},
            no_classify=True,
        )
        gateway.capture(
            layer="project",
            content="semantic filtered recall",
            item_id="recall-status-rejected",
            tags=["backend"],
            extra_metadata={"semantic_status": "draft"},
            no_classify=True,
        )

        report = gateway.recall(
            queries=["semantic filtered"],
            tags_all=["backend"],
            tags_any=["tdd"],
            semantic_statuses=["verified"],
            candidate_limit=10,
            selected_limit=10,
        )

        assert [candidate.id for candidate in report.candidates] == [matched]

    def test_recall_applies_character_budget(self, gateway):
        gateway.capture(
            layer="project",
            content="budget recall " + ("x" * 200),
            item_id="recall-budget-001",
            no_classify=True,
        )

        report = gateway.recall(
            queries=["budget recall"],
            selected_limit=1,
            max_selected_chars=40,
        )

        assert report.used_chars <= 40
        assert len(report.selected) == 1
        assert len(report.selected[0].content) <= 40
        assert report.selected[0].content.endswith("...")

    def test_recall_result_order_is_deterministic_for_same_request(self, gateway):
        for item_id in ["recall-order-b", "recall-order-a", "recall-order-c"]:
            gateway.capture(
                layer="project",
                content="same score ordering token",
                item_id=item_id,
                extra_metadata={
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "quality_score": 0.8,
                },
                no_classify=True,
            )

        first = gateway.recall(queries=["same score ordering"], candidate_limit=10, selected_limit=10)
        second = gateway.recall(queries=["same score ordering"], candidate_limit=10, selected_limit=10)

        assert [candidate.id for candidate in first.candidates] == [
            candidate.id for candidate in second.candidates
        ]

    def test_recall_returns_empty_report_for_empty_queries_or_zero_limit(self, gateway):
        empty = gateway.recall(queries=[" ", ""], candidate_limit=10)
        zero_limit = gateway.recall(queries=["anything"], candidate_limit=0)

        assert empty.candidates == ()
        assert empty.selected == ()
        assert zero_limit.candidates == ()
        assert zero_limit.selected == ()

    def test_recall_skips_missing_and_filter_rejected_results(self, gateway, monkeypatch):
        gateway.capture(
            layer="project",
            content="patched recall reject",
            item_id="recall-filter-rejected",
            extra_metadata={"project_id": "other"},
            no_classify=True,
        )

        def fake_search_for_context(**kwargs):
            return [
                {"item_id": "", "content": "", "metadata": {}, "score": 1.0},
                {"item_id": "missing-memory", "content": "", "metadata": {}, "score": 1.0},
                {"item_id": "recall-filter-rejected", "content": "", "metadata": {}, "score": 1.0},
            ]

        monkeypatch.setattr(gateway, "search_for_context", fake_search_for_context)

        report = gateway.recall(queries=["patched recall"], project_id="wanted")

        assert report.candidates == ()

    def test_recall_filters_reject_each_supported_dimension(self, gateway):
        item = {
            "layer": "project",
            "tags": ["backend"],
            "project_id": "project-a",
            "project_root_hash": "root-a",
            "semantic_status": "verified",
            "task_shape": "bugfix",
            "agent_role": "backend",
            "active_files": ["core/gateway.py"],
        }

        common = {
            "layers": None,
            "tags_all": None,
            "tags_any": None,
            "project_id": None,
            "project_root_hash": None,
            "semantic_statuses": None,
            "task_shape": None,
            "agent_role": None,
            "active_files": None,
        }
        assert gateway._matches_recall_filters(item, **{**common, "layers": ["global"]}) is False
        assert gateway._matches_recall_filters(item, **{**common, "tags_all": ["missing"]}) is False
        assert gateway._matches_recall_filters(item, **{**common, "tags_any": ["missing"]}) is False
        assert gateway._matches_recall_filters(item, **{**common, "project_id": "project-b"}) is False
        assert gateway._matches_recall_filters(item, **{**common, "project_root_hash": "root-b"}) is False
        assert gateway._matches_recall_filters(item, **{**common, "semantic_statuses": ["draft"]}) is False
        assert gateway._matches_recall_filters(item, **{**common, "task_shape": "feature"}) is False
        assert gateway._matches_recall_filters(item, **{**common, "agent_role": "frontend"}) is False
        assert gateway._matches_recall_filters(item, **{**common, "active_files": ["core/search.py"]}) is False

    def test_recall_helper_edge_cases(self):
        from core.contracts import RecallMemory
        from core.gateway import _recall_memory, _recall_score, _select_recall_memories

        assert _recall_score({"score": object()}) == 0.0
        fallback = _recall_memory(
            {"_path": "/tmp/path-fallback.md", "content": "x"},
            score=1.0,
            matched_queries=("x",),
            source=None,
        )
        assert fallback.id == "path-fallback"

        candidate = RecallMemory(
            id="short-budget",
            layer="project",
            stage="stored",
            content="abcdef",
            score=1.0,
        )
        assert _select_recall_memories([candidate], selected_limit=0, max_selected_chars=10) == ([], 0)
        assert _select_recall_memories([candidate], selected_limit=1, max_selected_chars=0) == ([], 0)
        selected, used_chars = _select_recall_memories(
            [candidate],
            selected_limit=1,
            max_selected_chars=2,
        )
        assert selected[0].content == ".."
        assert used_chars == 2

    def test_recall_ranking_ignores_access_and_retrieval_counts(self, gateway):
        from core.feedback import FeedbackStore

        high_access = gateway.capture(
            layer="project",
            content="usage ranking neutral token high",
            item_id="recall-usage-access-a",
            extra_metadata={"access_count": 999},
            no_classify=True,
        )
        low_access = gateway.capture(
            layer="project",
            content="usage ranking neutral token low",
            item_id="recall-usage-access-b",
            extra_metadata={"access_count": 0},
            no_classify=True,
        )
        store = FeedbackStore(gateway._root)
        store.record({
            "schema_version": "mnemos.feedback.request.v1",
            "event_id": "retrieval-loop-a",
            "event": "retrieved",
            "memory_id": high_access,
            "task_id": "task-a",
            "application": {"artifact": "ctx", "locator": "/a"},
        }, legacy_access_count=999)

        report = gateway.recall(
            queries=["usage ranking neutral"],
            candidate_limit=10,
            selected_limit=10,
        )

        assert [candidate.id for candidate in report.candidates[:2]] == [high_access, low_access]
        assert report.candidates[0].score_components["validated_usage"] == 0.0
        assert report.candidates[0].score_components["retrieval_count"] == 1
        assert report.candidates[0].score_components["legacy_access_count"] == 999

    def test_recall_ranking_prefers_validated_distinct_task_usage(self, gateway):
        from core.feedback import FeedbackStore

        weak = gateway.capture(
            layer="project",
            content="validated usage ranking token weak",
            item_id="recall-usage-rank-a",
            no_classify=True,
        )
        strong = gateway.capture(
            layer="project",
            content="validated usage ranking token strong",
            item_id="recall-usage-rank-b",
            no_classify=True,
        )
        store = FeedbackStore(gateway._root)
        for index, task_id in enumerate(["task-1", "task-2"], start=1):
            store.record({
                "schema_version": "mnemos.feedback.request.v1",
                "event_id": f"validated-{index}",
                "event": "validated",
                "memory_id": strong,
                "task_id": task_id,
                "application": {"artifact": "review.json", "locator": f"/{index}"},
            })
        store.record({
            "schema_version": "mnemos.feedback.request.v1",
            "event_id": "applied-weak",
            "event": "applied",
            "memory_id": weak,
            "task_id": "task-3",
            "application": {"artifact": "plan.json", "locator": "/weak"},
        })

        report = gateway.recall(
            queries=["validated usage ranking"],
            candidate_limit=10,
            selected_limit=10,
        )

        assert [candidate.id for candidate in report.candidates[:2]] == [strong, weak]
        assert report.candidates[0].score_components["validated_usage"] > report.candidates[1].score_components["validated_usage"]
        assert report.candidates[0].score_components["distinct_validated_task_count"] == 2

    def test_recall_excludes_superseded_and_invalidated_memories(self, gateway):
        from core.feedback import FeedbackStore

        active = gateway.capture(
            layer="project",
            content="status exclusion recall token active",
            item_id="recall-status-active",
            no_classify=True,
        )
        invalidated = gateway.capture(
            layer="project",
            content="status exclusion recall token invalidated",
            item_id="recall-status-invalidated",
            no_classify=True,
        )
        superseded = gateway.capture(
            layer="project",
            content="status exclusion recall token superseded",
            item_id="recall-status-superseded",
            no_classify=True,
        )
        gateway._store.update(gateway.peek(superseded)["_path"], metadata_updates={"superseded_by": active})
        store = FeedbackStore(gateway._root)
        store.record({
            "schema_version": "mnemos.feedback.request.v1",
            "event_id": "invalidated-status",
            "event": "invalidated",
            "memory_id": invalidated,
            "task_id": "task-status",
            "application": {"artifact": "review.json", "locator": "/invalid"},
        })

        report = gateway.recall(
            queries=["status exclusion recall"],
            candidate_limit=10,
            selected_limit=10,
        )

        assert [candidate.id for candidate in report.candidates] == [active]


class TestSearchTouchPolicy:
    def test_search_default_is_read_only_and_does_not_auto_promote(self, gateway, monkeypatch):
        item_id = gateway.capture(
            layer="project",
            content="legacy search readonly token",
            item_id="search-readonly-001",
            quality_score=1.0,
            no_classify=True,
        )
        before = gateway.peek(item_id)
        calls = []

        def fail_auto_promote(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("search default must not auto-promote")

        monkeypatch.setattr(gateway, "_auto_promote_if_eligible", fail_auto_promote)

        results = gateway.search("legacy search readonly", limit=5)

        after = gateway.peek(item_id)
        assert [result["item_id"] for result in results] == [item_id]
        assert after["access_count"] == before["access_count"]
        assert after["layer"] == before["layer"]
        assert calls == []

    def test_search_touch_increments_legacy_access_without_auto_promote(self, gateway, monkeypatch):
        item_id = gateway.capture(
            layer="project",
            content="legacy search touch token",
            item_id="search-touch-001",
            quality_score=1.0,
            no_classify=True,
        )
        calls = []

        def fail_auto_promote(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("search touch must not auto-promote")

        monkeypatch.setattr(gateway, "_auto_promote_if_eligible", fail_auto_promote)

        results = gateway.search("legacy search touch", limit=5, touch=True)

        after = gateway.peek(item_id)
        assert [result["item_id"] for result in results] == [item_id]
        assert after["access_count"] == 1
        assert after["layer"] == "project"
        assert calls == []


class TestConsolidate:
    """Tests for the gateway.consolidate() sweep (Part 2 of issue #11)."""

    def test_consolidate_promotes_eligible_items(self, gateway, repo_root):
        """consolidate() must promote all items that meet policy thresholds."""
        # Capture several items in project layer (zero thresholds → all eligible)
        ids = []
        for i in range(3):
            item_id = gateway.capture(
                layer="project",
                content=f"Consolidate test item {i}",
                quality_score=0.9,
                run_id="run-test",
            )
            ids.append(item_id)

        # All items start in project
        for item_id in ids:
            assert len(list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))) == 1

        promoted_count = gateway.consolidate()
        assert promoted_count >= 3

        # All items should now be in global layer
        for item_id in ids:
            matches_global = list((repo_root / "wiki" / "global").glob(f"{item_id}.md"))
            assert len(matches_global) == 1

    def test_consolidate_skips_ineligible_items(self, repo_root):
        """consolidate() must not promote items that do not meet quality_score threshold."""
        import yaml
        # Write a policy with high quality_score threshold that prevents promotion
        strict_policy = {
            "layers": {
                "project": {
                    "path_template": "wiki/projects/",
                    "promotes_to": "global",
                    "promotion": {
                        "age_hours": 0.0,
                        "access_count": 0,
                        "quality_score": 0.99,  # Very high — low-quality items won't pass
                    },
                },
                "global": {
                    "path_template": "wiki/global/",
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
            },
            "forget": {"requires_archived": True},
            "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
        }
        (repo_root / "wiki" / "policy.yaml").write_text(yaml.dump(strict_policy))

        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))

        # Capture a low-quality item
        item_id = gw.capture(
            layer="project",
            content="Low quality item",
            quality_score=0.5,  # Below the 0.99 threshold
            run_id="run-test",
        )

        promoted_count = gw.consolidate()
        assert promoted_count == 0

        # Item should still be in project
        matches_project = list((repo_root / "wiki" / "projects").glob(f"{item_id}.md"))
        assert len(matches_project) == 1

    def test_consolidate_returns_zero_on_empty_store(self, gateway, repo_root):
        """consolidate() must return 0 when no items exist."""
        promoted_count = gateway.consolidate()
        assert promoted_count == 0

    def test_consolidate_does_not_double_promote(self, gateway, repo_root):
        """consolidate() run twice must not fail — already promoted items are in global."""
        item_id = gateway.capture(
            layer="project",
            content="Double consolidate test",
            quality_score=0.9,
            run_id="run-test",
        )
        first_run = gateway.consolidate()
        assert first_run >= 1

        # Second run should not promote the global item (no next layer)
        second_run = gateway.consolidate()
        assert second_run == 0


class TestEphemeralDefault:
    """Tests for the ephemeral-first capture default (issue #12)."""

    def test_capture_defaults_to_ephemeral_layer(self, gateway, repo_root):
        """capture() without --layer must write to the ephemeral layer."""
        item_id = gateway.capture(content="Ephemeral default test")

        # File must exist somewhere under .agent/runs/{run_id}/scratch/
        agent_runs = repo_root / ".agent" / "runs"
        matches = list(agent_runs.rglob("*.md"))
        assert any(item_id in m.name for m in matches), (
            f"Expected ephemeral file for {item_id} under {agent_runs}, found: {matches}"
        )

    def test_capture_default_layer_metadata_is_ephemeral(self, gateway, repo_root):
        """Item captured without explicit layer must record layer=ephemeral in metadata."""
        import frontmatter as fm
        item_id = gateway.capture(content="Layer metadata check")

        agent_runs = repo_root / ".agent" / "runs"
        found = [p for p in agent_runs.rglob("*.md") if item_id in p.name]
        assert len(found) == 1, f"Expected exactly one file, got {found}"
        post = fm.load(str(found[0]))
        assert post["layer"] == "ephemeral"

    def test_capture_explicit_layer_overrides_default(self, gateway, repo_root):
        """Explicit layer= argument must still be honoured."""
        item_id = gateway.capture(content="Explicit session capture", layer="session")

        agent_sessions = repo_root / ".agent" / "sessions"
        matches = list(agent_sessions.rglob("*.md"))
        assert any(item_id in m.name for m in matches), (
            f"Expected session file for {item_id} under {agent_sessions}, found: {matches}"
        )

    def test_capture_auto_generates_run_id_per_gateway(self, gateway, repo_root):
        """Two captures from the same gateway must share the same auto-generated run_id."""
        id1 = gateway.capture(content="First ephemeral item")
        id2 = gateway.capture(content="Second ephemeral item")

        agent_runs = repo_root / ".agent" / "runs"
        file1 = next((p for p in agent_runs.rglob("*.md") if id1 in p.name), None)
        file2 = next((p for p in agent_runs.rglob("*.md") if id2 in p.name), None)
        assert file1 is not None
        assert file2 is not None
        # Both files must live in the same run directory
        assert file1.parent.parent == file2.parent.parent, (
            f"Expected same run dir: {file1.parent.parent} vs {file2.parent.parent}"
        )

    def test_capture_explicit_run_id_overrides_gateway_default(self, gateway, repo_root):
        """Explicit run_id= must be used instead of the gateway auto-generated one."""
        custom_run_id = "custom-run-abc"
        item_id = gateway.capture(content="Custom run_id test", run_id=custom_run_id)

        expected_dir = repo_root / ".agent" / "runs" / custom_run_id / "scratch"
        assert expected_dir.exists(), f"Expected directory {expected_dir} to exist"
        matches = list(expected_dir.glob(f"{item_id}.md"))
        assert len(matches) == 1

    def test_capture_records_run_id_and_session_id_in_metadata(self, gateway, repo_root):
        """Captured item must record run_id and session_id in front-matter metadata."""
        import frontmatter as fm
        item_id = gateway.capture(content="Metadata run_id session_id test")

        agent_runs = repo_root / ".agent" / "runs"
        found = [p for p in agent_runs.rglob("*.md") if item_id in p.name]
        assert len(found) == 1
        post = fm.load(str(found[0]))
        assert "run_id" in post.metadata, "run_id must be in front-matter metadata"
        assert "session_id" in post.metadata, "session_id must be in front-matter metadata"
        assert post["run_id"] == gateway._run_id
        assert post["session_id"] == gateway._session_id


class TestCaptureDedup:
    """Tests for on-write dedup in gateway.capture() — Issue #4 anti-regression.

    Background
    ----------
    The Stop hook (hooks/Stop.sh) fires once per AI response turn, not at true
    session end.  Without dedup, re-parsing the same transcript on every turn
    would flood the session layer with repeated captures of the same insight
    (original Issue #4 that led to the Stop hook being removed in commit
    0de1c47).

    Re-introduction contract
    ------------------------
    gateway.capture() now maintains a per-gateway-instance in-memory dedup
    registry keyed by (layer, SHA-256(NFKC-normalised content)).

    - Duplicate captures within the same process return None (silent no-op).
    - The storage, FTS, audit log, and event-bus paths are never reached for
      duplicates.
    - The dedup key is layer-scoped: (session, hash_A) and (global, hash_A)
      are distinct keys, so cross-layer promotion is NOT blocked.
    - Unicode NFKC normalization is applied before whitespace/case folding so
      compatibility variants (full-width letters, ligatures, etc.) are treated
      as identical content.
    """

    @pytest.fixture
    def gateway(self, repo_root):
        """Fresh MemoryGateway instance for each test."""
        from core.gateway import MemoryGateway
        return MemoryGateway(repo_root=repo_root)

    @pytest.fixture
    def repo_root(self, tmp_path):
        """Minimal repo structure required by MemoryGateway."""
        wiki = tmp_path / "wiki"
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (wiki / d).mkdir(parents=True)
        agent = tmp_path / ".agent"
        for d in ["runs", "sessions", "state", "reports", "tools"]:
            (agent / d).mkdir(parents=True)
        (agent / "workflows" / "hooks").mkdir(parents=True)
        import yaml
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
            }
        }
        (tmp_path / "wiki" / "policy.yaml").write_text(yaml.dump(policy))
        return tmp_path

    def test_first_capture_returns_item_id(self, gateway, repo_root):
        """First capture of unique content must return a non-None item ID."""
        item_id = gateway.capture(content="Unique insight for dedup test", layer="session")
        assert item_id is not None, "First capture must return an item_id, not None"
        assert isinstance(item_id, str)
        assert len(item_id) > 0

    def test_duplicate_capture_returns_none(self, gateway, repo_root):
        """Second capture of the same content in the same layer must return None.

        This is the core anti-regression test for Issue #4: the Stop hook fires
        on every AI turn, so re-parsing the same transcript marker must be a
        silent no-op after the first write.
        """
        content = "Duplicate capture should be suppressed"
        first_id = gateway.capture(content=content, layer="session")
        assert first_id is not None, "First capture must succeed"

        second_result = gateway.capture(content=content, layer="session")
        assert second_result is None, (
            "Duplicate capture (same layer, same content) must return None — "
            "the gateway dedup registry should suppress it as a silent no-op"
        )

    def test_cross_layer_not_deduped(self, gateway, repo_root):
        """Same content captured in different layers must NOT be deduped.

        Dedup key is (layer, hash) — not just hash — so that cross-layer
        promotion (session → project → global) remains unblocked.  Capturing
        the same insight in 'session' and then in 'global' represents a
        legitimate promotion event, not a duplicate.
        """
        content = "Insight that gets promoted across layers"
        session_id = gateway.capture(content=content, layer="session")
        assert session_id is not None, "Session-layer capture must succeed"

        global_id = gateway.capture(content=content, layer="global")
        assert global_id is not None, (
            "Cross-layer capture of the same content must NOT be deduped — "
            "promotion across layers must remain unblocked"
        )
        assert session_id != global_id, "Each layer must produce its own item_id"

    def test_different_content_same_layer_not_deduped(self, gateway, repo_root):
        """Different content in the same layer must each return a distinct item_id."""
        id1 = gateway.capture(content="First distinct insight", layer="session")
        id2 = gateway.capture(content="Second distinct insight", layer="session")
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2, "Different content must produce different item IDs"

    def test_nfkc_equivalent_forms_are_deduped(self, gateway, repo_root):
        """NFKC-equivalent Unicode forms must be treated as the same content.

        NFKC normalization collapses compatibility variants — for example,
        full-width Latin letters (U+FF21..U+FF3A) are canonicalised to their
        ASCII equivalents.  Two strings that differ only in Unicode
        compatibility representation must hash to the same dedup key and the
        second capture must return None.
        """
        # Regular ASCII form
        ascii_content = "decision about architecture"
        # Full-width Unicode form — NFKC normalises these to ASCII equivalents
        fullwidth_content = "ｄｅｃｉｓｉｏｎ ａｂｏｕｔ ａｒｃｈｉｔｅｃｔｕｒｅ"

        first_id = gateway.capture(content=ascii_content, layer="session")
        assert first_id is not None

        second_result = gateway.capture(content=fullwidth_content, layer="session")
        assert second_result is None, (
            "NFKC-equivalent content (full-width vs ASCII) must be treated as "
            "a duplicate and return None"
        )

    def test_whitespace_normalisation_deduplication(self, gateway, repo_root):
        """Content differing only in internal whitespace must be deduped.

        After NFKC, the normaliser collapses all whitespace runs to a single
        space and strips leading/trailing whitespace.  Captures that differ
        only in spacing are duplicates.
        """
        content_clean = "insight about caching strategy"
        content_extra_spaces = "  insight  about   caching   strategy  "

        first_id = gateway.capture(content=content_clean, layer="session")
        assert first_id is not None

        second_result = gateway.capture(content=content_extra_spaces, layer="session")
        assert second_result is None, (
            "Content differing only in whitespace must be deduped after "
            "NFKC + whitespace normalisation"
        )

    def test_dedup_registry_is_per_instance(self, repo_root):
        """Cross-process dedup: second instance detects the stored item and
        returns its existing id (non-None) with last_capture_was_duplicate=True.

        The in-memory dedup registry is per-instance and does not transfer, but
        the cross-process persistent dedup scan (Issues #49/#50) means the
        second gateway instance returns the already-stored item_id rather than
        writing a duplicate.
        """
        from core.gateway import MemoryGateway
        content = "Shared content across two gateway instances"

        gw1 = MemoryGateway(repo_root=repo_root)
        id1 = gw1.capture(content=content, layer="session")
        assert id1 is not None

        # Second instance detects the stored item via cross-process scan
        gw2 = MemoryGateway(repo_root=repo_root)
        id2 = gw2.capture(content=content, layer="session")
        # Cross-process dedup returns the existing id (not None, not a new UUID)
        assert id2 is not None, (
            "Cross-process dedup must return the existing item_id, not None"
        )
        assert id2 == id1, (
            "Cross-process dedup must return the SAME item_id as the first capture"
        )
        assert gw2.last_capture_was_duplicate is True, (
            "last_capture_was_duplicate must be True when a cross-process "
            "duplicate is detected"
        )


class TestCrossProcessCaptureDedup:
    """Tests for cross-process persistent deduplication (Issues #49 and #50).

    Background
    ----------
    The in-memory ``_capture_dedup`` registry is reset each time a new
    MemoryGateway instance is created.  A second invocation of
    ``mnemos capture`` therefore starts with an empty registry and — without
    further guards — would write a duplicate memory entry for the same content.

    Fix contract
    ------------
    ``gateway.capture()`` now scans persistent storage (all layers) via
    ``_find_existing_by_hash()`` before writing.  When an item with the same
    SHA-256 content hash exists anywhere in the store, the method:

    - Returns the *existing* item_id (not None, not a fresh UUID).
    - Sets ``self.last_capture_was_duplicate = True`` so the CLI can print
      ``(existing) <uuid>`` instead of the normal capture notice.
    - Warms the in-process cache so subsequent same-process calls remain
      fast (no second scan).

    The ``content_hash`` field is stored in item metadata on every new write
    so future scans can use it for fast equality comparison.
    """

    @pytest.fixture
    def repo_root(self, tmp_path):
        """Minimal repo structure required by MemoryGateway."""
        wiki = tmp_path / "wiki"
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (wiki / d).mkdir(parents=True)
        agent = tmp_path / ".agent"
        for d in ["runs", "sessions", "state", "reports", "tools"]:
            (agent / d).mkdir(parents=True)
        (agent / "workflows" / "hooks").mkdir(parents=True)
        import yaml
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

    def _make_gateway(self, repo_root):
        from core.gateway import MemoryGateway
        return MemoryGateway(repo_root=str(repo_root))

    # ------------------------------------------------------------------
    # Test 1: cross-instance returns existing id
    # ------------------------------------------------------------------

    def test_cross_instance_returns_existing_id(self, repo_root):
        """Second gateway instance (simulating a new process) must return the
        existing item_id and NOT write a duplicate file.
        """
        content = "Cross-process dedup: architecture decision recorded"
        gw1 = self._make_gateway(repo_root)
        id1 = gw1.capture(content=content, layer="project")
        assert id1 is not None, "First capture must succeed"
        assert gw1.last_capture_was_duplicate is False, (
            "last_capture_was_duplicate must be False after a fresh write"
        )

        gw2 = self._make_gateway(repo_root)
        id2 = gw2.capture(content=content, layer="project")

        assert id2 is not None, "Cross-process duplicate must return an id, not None"
        assert id2 == id1, (
            "Cross-process duplicate must return the SAME item_id as the original, "
            f"got id1={id1!r} id2={id2!r}"
        )
        assert gw2.last_capture_was_duplicate is True, (
            "last_capture_was_duplicate must be True on cross-process duplicate"
        )

        # Verify no new file was written — still exactly one .md file
        project_dir = repo_root / "wiki" / "projects"
        md_files = list(project_dir.glob("*.md"))
        assert len(md_files) == 1, (
            f"Expected exactly 1 .md file in project layer, found {len(md_files)}"
        )

    # ------------------------------------------------------------------
    # Test 2: cross-layer dedup across processes
    # ------------------------------------------------------------------

    def test_cross_layer_dedup_across_processes(self, repo_root):
        """Content written to 'session' by gw1 is detected as a duplicate by
        gw2 even when gw2 targets 'global' — the scan checks ALL layers.
        """
        content = "Global insight that was first captured in session layer"
        gw1 = self._make_gateway(repo_root)
        id1 = gw1.capture(content=content, layer="session")
        assert id1 is not None

        # New process, different target layer — cross-layer cross-process scan
        gw2 = self._make_gateway(repo_root)
        id2 = gw2.capture(content=content, layer="global")

        assert id2 is not None, "Cross-layer cross-process duplicate must return an id"
        assert id2 == id1, (
            "Cross-layer cross-process scan must return the original item_id"
        )
        assert gw2.last_capture_was_duplicate is True, (
            "last_capture_was_duplicate must be True on cross-layer cross-process duplicate"
        )

    # ------------------------------------------------------------------
    # Test 3: content_hash stored in metadata
    # ------------------------------------------------------------------

    def test_content_hash_stored_in_metadata(self, repo_root):
        """The 'content_hash' field must be present in the item's YAML front-matter
        after a successful capture so future cross-process scans can use it.
        """
        import frontmatter as fm
        content = "content hash metadata storage test"
        gw = self._make_gateway(repo_root)
        item_id = gw.capture(content=content, layer="global")
        assert item_id is not None

        # Locate the written file
        global_dir = repo_root / "wiki" / "global"
        md_files = list(global_dir.glob("*.md"))
        assert len(md_files) == 1, "Expected exactly one .md file"

        post = fm.load(str(md_files[0]))
        assert "content_hash" in post.metadata, (
            "content_hash must be stored in item YAML front-matter"
        )
        # Verify the hash value is correct
        from core.gateway import _capture_content_hash
        expected_hash = _capture_content_hash(content)
        assert post.metadata["content_hash"] == expected_hash, (
            f"Stored hash {post.metadata['content_hash']!r} != expected {expected_hash!r}"
        )

    # ------------------------------------------------------------------
    # Test 4: normalization applies in cross-process path
    # ------------------------------------------------------------------

    def test_normalization_applies_in_cross_process(self, repo_root):
        """Content written with extra whitespace by gw1 must be found as a
        duplicate by gw2 when capturing the clean form — NFKC + whitespace
        normalisation applies in the cross-process hash comparison.
        """
        content_with_spaces = "  architecture   decision  about   caching  "
        content_clean = "architecture decision about caching"

        gw1 = self._make_gateway(repo_root)
        id1 = gw1.capture(content=content_with_spaces, layer="project")
        assert id1 is not None

        gw2 = self._make_gateway(repo_root)
        id2 = gw2.capture(content=content_clean, layer="project")

        assert id2 is not None, "Normalised duplicate must return an id"
        assert id2 == id1, (
            "Normalised duplicate must match the original item_id — "
            f"id1={id1!r} id2={id2!r}"
        )
        assert gw2.last_capture_was_duplicate is True

    # ------------------------------------------------------------------
    # Test 5: last_capture_was_duplicate flag lifecycle
    # ------------------------------------------------------------------

    def test_last_capture_was_duplicate_flag_lifecycle(self, repo_root):
        """Flag must start False, be True on duplicate, then reset to False on
        the next fresh capture call.
        """
        gw = self._make_gateway(repo_root)

        # Initial state
        assert gw.last_capture_was_duplicate is False

        # First capture — new write
        id1 = gw.capture(content="First unique insight", layer="global")
        assert id1 is not None
        assert gw.last_capture_was_duplicate is False, (
            "Flag must be False after a fresh new write"
        )

        # New gateway simulates a new process
        gw2 = self._make_gateway(repo_root)
        assert gw2.last_capture_was_duplicate is False, (
            "Flag must start as False on a new gateway instance"
        )

        id2 = gw2.capture(content="First unique insight", layer="global")
        assert gw2.last_capture_was_duplicate is True, (
            "Flag must be True after cross-process duplicate detected"
        )

        # Next capture with DIFFERENT content resets the flag
        id3 = gw2.capture(content="Second completely different insight", layer="global")
        assert id3 is not None
        assert gw2.last_capture_was_duplicate is False, (
            "Flag must be reset to False after a fresh new write"
        )

    def test_success_case_regression_update_allows_old_content_to_be_captured_again(
        self,
        repo_root,
    ):
        """success-case(regression) - updated item no longer claims the old hash."""
        # given
        old_content = "old content before edit"
        new_content = "new content after edit"
        gw1 = self._make_gateway(repo_root)
        edited_id = gw1.capture(content=old_content, layer="global", no_classify=True)
        assert edited_id is not None

        # when
        gw1.update(edited_id, content=new_content)
        gw2 = self._make_gateway(repo_root)
        recaptured_id = gw2.capture(
            content=old_content,
            layer="global",
            no_classify=True,
        )

        # then
        assert recaptured_id is not None
        assert recaptured_id != edited_id
        assert gw2.last_capture_was_duplicate is False

    # ------------------------------------------------------------------
    # Test 6: in-process cache warmed after cross-process detection
    # ------------------------------------------------------------------

    def test_in_process_cache_warmed_after_cross_process_detection(self, repo_root):
        """After a cross-process duplicate is detected, subsequent same-process
        calls must use the warmed in-process cache (return None, not the id).
        """
        content = "Cache warming test content"
        gw1 = self._make_gateway(repo_root)
        id1 = gw1.capture(content=content, layer="global")
        assert id1 is not None

        gw2 = self._make_gateway(repo_root)
        # First call: cross-process scan fires, returns existing id
        id2 = gw2.capture(content=content, layer="global")
        assert id2 == id1
        assert gw2.last_capture_was_duplicate is True

        # Second call on the SAME gateway instance: in-process cache is now
        # warmed — returns None (silent no-op, existing in-process contract)
        id3 = gw2.capture(content=content, layer="global")
        assert id3 is None, (
            "After cross-process dedup warms the in-process cache, "
            "subsequent same-process calls must return None (silent no-op)"
        )


class TestPolicyPathResolution:
    """Tests for ``MemoryGateway`` policy.yaml path resolution (#96).

    The gateway looks up ``policy.yaml`` in three locations, in order:

    1. ``MNEMOS_POLICY_PATH`` env override (preference only — falls
       through to the conventional candidates if the override points at
       a non-existent file).
    2. Install-root convention — ``<repo_root>/wiki/policy.yaml``
       (used by ``~/.mnemos`` after ``install.sh``).
    3. Dev/source-repo convention — ``<repo_root>/repo/wiki/policy.yaml``
       (used when ``MNEMOS_REPO_ROOT`` points at a checked-out mnemos
       source tree, where the live policy lives at ``repo/wiki/`` rather
       than the top-level ``wiki/``).

    These tests exercise every branch so the resolution semantics are
    locked in as the contract.
    """

    @staticmethod
    def _minimal_policy_dict() -> dict:
        """Return a minimal policy dict accepted by ``PolicyEngine``."""
        return {
            "layers": {
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

    def test_policy_path_resolves_install_convention(self, tmp_path, monkeypatch):
        """Install layout — ``<root>/wiki/policy.yaml`` exists → load it.

        This is the pre-existing behavior and MUST be preserved. The
        gateway picks the install-root candidate before falling through
        to the dev/source-repo candidate.
        """
        monkeypatch.delenv("MNEMOS_POLICY_PATH", raising=False)
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "policy.yaml").write_text(
            yaml.dump(self._minimal_policy_dict())
        )

        from core.gateway import MemoryGateway

        gw = MemoryGateway(repo_root=str(tmp_path))

        # The gateway must have selected the install-convention candidate.
        assert gw._policy._policy_path == str(tmp_path / "wiki" / "policy.yaml")

    def test_policy_path_resolves_dev_repo_convention(self, tmp_path, monkeypatch):
        """Dev/source-repo layout — ``<root>/repo/wiki/policy.yaml`` exists.

        Reproduces the #96 defect setup: ``MNEMOS_REPO_ROOT`` points at a
        mnemos source-tree checkout where the live policy lives under
        ``repo/wiki/policy.yaml``, NOT the top-level ``wiki/``. Before
        the fix this raised ``FileNotFoundError``; after the fix the
        gateway falls through to the second candidate and loads cleanly.
        """
        monkeypatch.delenv("MNEMOS_POLICY_PATH", raising=False)
        (tmp_path / "repo" / "wiki").mkdir(parents=True)
        (tmp_path / "repo" / "wiki" / "policy.yaml").write_text(
            yaml.dump(self._minimal_policy_dict())
        )
        # Explicitly assert the install-convention candidate does NOT exist
        # so any future refactor that silently falls back to creating it
        # is caught.
        assert not (tmp_path / "wiki" / "policy.yaml").exists()

        from core.gateway import MemoryGateway

        gw = MemoryGateway(repo_root=str(tmp_path))

        assert gw._policy._policy_path == str(
            tmp_path / "repo" / "wiki" / "policy.yaml"
        )

    def test_policy_path_env_override_wins(self, tmp_path, monkeypatch):
        """``MNEMOS_POLICY_PATH`` is preferred over install/dev candidates.

        When the override points at an existing file, the gateway must
        load policy from that exact path — ignoring both the
        install-root and dev/source-repo conventional locations even
        when those also exist.
        """
        # Create ALL three potential candidates with distinct contents so
        # the gateway's choice is unambiguous and assertable.
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "policy.yaml").write_text(
            yaml.dump(self._minimal_policy_dict())
        )
        (tmp_path / "repo" / "wiki").mkdir(parents=True)
        (tmp_path / "repo" / "wiki" / "policy.yaml").write_text(
            yaml.dump(self._minimal_policy_dict())
        )
        (tmp_path / "custom").mkdir()
        custom_path = tmp_path / "custom" / "policy.yaml"
        custom_path.write_text(yaml.dump(self._minimal_policy_dict()))

        monkeypatch.setenv("MNEMOS_POLICY_PATH", str(custom_path))

        from core.gateway import MemoryGateway

        gw = MemoryGateway(repo_root=str(tmp_path))

        assert gw._policy._policy_path == str(custom_path), (
            "MNEMOS_POLICY_PATH override must take priority over "
            "install-root and dev/source-repo candidates"
        )

    def test_policy_path_missing_raises_with_all_candidates_listed(
        self, tmp_path, monkeypatch
    ):
        """No candidate exists → ``FileNotFoundError`` listing tried paths.

        The error message must enumerate all attempted locations AND
        mention ``MNEMOS_POLICY_PATH`` so an operator can diagnose the
        failure without grepping source.
        """
        # No wiki/, no repo/wiki/, no override
        missing_override = tmp_path / "nowhere" / "policy.yaml"
        monkeypatch.setenv("MNEMOS_POLICY_PATH", str(missing_override))

        from core.gateway import MemoryGateway

        with pytest.raises(FileNotFoundError) as excinfo:
            MemoryGateway(repo_root=str(tmp_path))

        msg = str(excinfo.value)
        # All three candidates must appear in the error message.
        assert str(missing_override) in msg, (
            "missing MNEMOS_POLICY_PATH candidate must be listed"
        )
        assert str(tmp_path / "wiki" / "policy.yaml") in msg, (
            "install-root candidate must be listed"
        )
        assert str(tmp_path / "repo" / "wiki" / "policy.yaml") in msg, (
            "dev/source-repo candidate must be listed"
        )
        assert "MNEMOS_POLICY_PATH" in msg, (
            "error must mention the MNEMOS_POLICY_PATH override mechanism"
        )

    def test_policy_path_override_missing_still_falls_through_to_install_root(
        self, tmp_path, monkeypatch
    ):
        """``MNEMOS_POLICY_PATH`` is a preference, not a hard requirement.

        Semantic: when the override points at a non-existent file but a
        conventional candidate exists, the gateway falls through to the
        next candidate. This matches the user expectation "I gave a
        hint; if it's wrong, still try the defaults" and is the least
        surprising semantic for a hint-style env var. Documented in
        :class:`MemoryGateway.__init__`.
        """
        # Install-root candidate exists; override path does NOT exist
        (tmp_path / "wiki").mkdir()
        (tmp_path / "wiki" / "policy.yaml").write_text(
            yaml.dump(self._minimal_policy_dict())
        )
        missing_override = tmp_path / "nowhere" / "policy.yaml"
        assert not missing_override.exists()
        monkeypatch.setenv("MNEMOS_POLICY_PATH", str(missing_override))

        from core.gateway import MemoryGateway

        # Must succeed — the override is a preference; install-root is the
        # fallback.
        gw = MemoryGateway(repo_root=str(tmp_path))

        assert gw._policy._policy_path == str(tmp_path / "wiki" / "policy.yaml")
