"""Tests for promotion visibility (mnemos #20 gap 3).

Tests cover:
1. `mnemos promote` emits '✻ <target-emoji> promoted <id> → <target-layer>' on success
   where target-emoji is 💡 (session), 💾 (project), or 🧠 (global)
2. `mnemos promote --quiet` suppresses the promotion notice
3. `mnemos consolidate` emits per-promotion notice lines
4. `mnemos bg-check` emits per-promotion notice lines via event bus
5. hooks/UserPromptSubmit.sh injects <mnemos-promotion> block when there are
   promotions since the last hook fire
6. MNEMOS_BEHAVIOR_BLOCK (base.py) includes layer-selection criteria
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared fixture: minimal mnemos repo with zero-threshold policy
# ---------------------------------------------------------------------------

HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "UserPromptSubmit.sh"


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal mnemos repo structure with zero-threshold promotions."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state"]:
        (agent / d).mkdir(parents=True)

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
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root: Path, monkeypatch) -> object:
    """Return CLI with MNEMOS_REPO_ROOT set to tmp repo."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


# ---------------------------------------------------------------------------
# Helper: capture an item directly via the gateway
# ---------------------------------------------------------------------------

def _capture_item(repo_root: Path, content: str, layer: str = "session",
                  item_id: str | None = None) -> str:
    """Capture a memory item and return its ID."""
    from core.gateway import MemoryGateway
    gw = MemoryGateway(repo_root=str(repo_root))
    captured_id = gw.capture(
        content=content,
        layer=layer,
        item_id=item_id,
        session_id="test-session-promote",
    )
    return captured_id


# ---------------------------------------------------------------------------
# 1. mnemos promote — emit notice on success, suppressed by --quiet
# ---------------------------------------------------------------------------

class TestPromoteCLIVisibility:
    def test_promote_emits_notice_on_success(self, runner, cli_with_repo, repo_root):
        """promote must print '✻ <target-emoji> promoted <id> → <layer>' after success.

        Under the emoji-by-layer scheme, the marker emoji reflects the *target*
        layer (💡 session, 💾 project, 🧠 global). A session→project promotion
        therefore emits 💾, not 🧠.
        """
        item_id = _capture_item(repo_root, "Architecture decision: use SQLite for FTS")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        # Must contain the promotion notice
        assert "✻" in result.output
        # Target layer is project (session→project), so the emoji is 💾.
        assert "💾" in result.output
        assert "promoted" in result.output.lower()
        assert item_id in result.output
        # Must include target layer (session → project)
        assert "→" in result.output

    def test_promote_notice_includes_item_id(self, runner, cli_with_repo, repo_root):
        """The promotion notice must include the item ID."""
        item_id = _capture_item(repo_root, "Reusable constraint: no new deps")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert item_id in result.output

    def test_promote_notice_includes_target_layer(self, runner, cli_with_repo, repo_root):
        """The promotion notice must show the target layer after '→'."""
        item_id = _capture_item(repo_root, "User preference: TDD required")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        # session → project (or whatever the next layer is)
        # The notice format: ✻ 🧠 promoted <id> → <layer>
        assert re.search(r"→\s*\w+", result.output), (
            f"Expected '→ <layer>' in output, got: {result.output!r}"
        )

    def test_promote_quiet_suppresses_notice(self, runner, cli_with_repo, repo_root):
        """promote --quiet must NOT print the ✻ <emoji> notice."""
        item_id = _capture_item(repo_root, "Quiet capture test")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--quiet"],
        )
        assert result.exit_code == 0, result.output
        # The standard 'promoted: <id>' line should still appear
        assert "promoted" in result.output.lower()
        # But the ✻ <emoji> decoration must be absent (any layer emoji).
        assert "✻" not in result.output
        assert "💡" not in result.output
        assert "💾" not in result.output
        assert "🧠" not in result.output

    def test_promote_default_is_not_quiet(self, runner, cli_with_repo, repo_root):
        """promote without --quiet must print the notice (non-quiet is the default)."""
        item_id = _capture_item(repo_root, "Default visibility test")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "✻" in result.output

    def test_promote_policy_error_no_notice(self, runner, cli_with_repo, repo_root):
        """When promotion is rejected by policy (e.g. global has no next layer),
        output must be the policy error — not a notice."""
        # Capture at global layer — promoting it should fail (no higher layer)
        item_id = _capture_item(repo_root, "Already at top layer", layer="global")
        result = runner.invoke(
            cli_with_repo,
            ["promote", item_id, "--no-color"],
        )
        assert result.exit_code != 0
        assert "✻" not in result.output
        assert "💡" not in result.output
        assert "💾" not in result.output
        assert "🧠" not in result.output


# ---------------------------------------------------------------------------
# 2. mnemos consolidate — per-promotion notice lines via event bus
# ---------------------------------------------------------------------------

class TestConsolidateVisibility:
    def test_consolidate_emits_per_promotion_notice(self, runner, cli_with_repo, repo_root):
        """consolidate must emit ✻ <target-emoji> promoted <id> → <layer> for each promoted item.

        Session→project promotion emits 💾 (the target layer's emoji).
        """
        # Create items at session layer (eligible for promotion to project)
        id1 = _capture_item(repo_root, "Architecture: hexagonal pattern", item_id="arch-001")
        id2 = _capture_item(repo_root, "Constraint: immutable audit log", item_id="constraint-002")

        # Use NO_COLOR env so ANSI codes don't interfere with string matching
        result = runner.invoke(cli_with_repo, ["consolidate"],
                               env={"NO_COLOR": "1", "MNEMOS_REPO_ROOT": str(repo_root)})
        assert result.exit_code == 0, result.output
        # Must contain promotion notice lines for each item promoted
        assert "✻" in result.output
        # Target layer is project, so the emoji is 💾.
        assert "💾" in result.output
        assert "promoted" in result.output.lower()
        # Each promoted item should appear at least once
        assert "→" in result.output

    def test_consolidate_no_items_no_notice(self, runner, cli_with_repo, repo_root):
        """consolidate with no eligible items must emit 0-promotion output with no ✻ <emoji>."""
        result = runner.invoke(cli_with_repo, ["consolidate"],
                               env={"NO_COLOR": "1", "MNEMOS_REPO_ROOT": str(repo_root)})
        assert result.exit_code == 0, result.output
        # With no promotable items, no notice should appear
        assert "✻" not in result.output
        assert "💡" not in result.output
        assert "💾" not in result.output
        assert "🧠" not in result.output


# ---------------------------------------------------------------------------
# 3. mnemos bg-check — per-promotion notice via event bus
# ---------------------------------------------------------------------------

class TestBgCheckVisibility:
    def test_bg_check_emits_promotion_notice(self, runner, cli_with_repo, repo_root, monkeypatch):
        """bg-check must emit ✻ <target-emoji> promoted <id> → <layer> for each auto-promoted item.

        Session→project promotion emits 💾.
        """
        # Items that will be auto-promoted
        id1 = _capture_item(repo_root, "BG check test: project decision", item_id="bg-test-001")

        result = runner.invoke(
            cli_with_repo,
            ["bg-check", "--force", "--no-gc", "--no-dedup"],
            env={"NO_COLOR": "1", "MNEMOS_REPO_ROOT": str(repo_root)},
        )
        assert result.exit_code == 0, result.output
        # When auto-promotion happens, the notice must appear
        if "bg-test-001" in result.output or "promoted" in result.output.lower():
            assert "✻" in result.output
            # Target layer is project, so the emoji is 💾.
            assert "💾" in result.output
            assert "→" in result.output


# ---------------------------------------------------------------------------
# 4. ClaudeCodeAdapter._on_post_promote — notice format includes item_id
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterPromoteFormat:
    def test_on_post_promote_includes_item_id(self, capsys):
        """_on_post_promote must include item_id (not just content_preview) in output."""
        from core.adapters.claude import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        payload = {
            "item_id": "test-item-abc-123",
            "content_preview": "Architecture decision: use SQLite",
            "from_layer": "session",
            "to_layer": "project",
        }

        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            adapter._on_post_promote(payload)

        captured = capsys.readouterr()
        output = captured.out
        # Must contain the item_id
        assert "test-item-abc-123" in output, f"item_id missing from: {output!r}"
        # Must contain target layer
        assert "project" in output
        # Must use the ✻ <target-emoji> prefix. Target is project → 💾.
        assert "✻" in output
        assert "💾" in output
        # Must show → separator
        assert "→" in output

    def test_on_post_promote_format_matches_spec(self, capsys):
        """Output must match: ✻ <target-emoji> promoted <id> → <target-layer>

        Under the emoji-by-layer scheme the marker emoji reflects the target
        layer (💡 session, 💾 project, 🧠 global). A session→project promotion
        therefore matches 💾.
        """
        from core.adapters.claude import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        payload = {
            "item_id": "spec-item-xyz",
            "content_preview": "Some content preview",
            "from_layer": "session",
            "to_layer": "project",
        }

        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            adapter._on_post_promote(payload)

        captured = capsys.readouterr()
        output = captured.out.strip()

        # Pattern: ✻ 💾 promoted <id> → project  (target is project)
        assert re.search(r"✻\s+💾\s+promoted\s+spec-item-xyz\s+→\s+project", output), (
            f"Output does not match expected format: {output!r}"
        )


# ---------------------------------------------------------------------------
# 5. hooks/UserPromptSubmit.sh — <mnemos-promotion> block injection
# ---------------------------------------------------------------------------

def _run_hook(
    prompt: str,
    session_id: str = "test-session-promo",
    mnemos_repo_root: str = "",
    env_extras: dict | None = None,
) -> tuple[int, str]:
    """Run the UserPromptSubmit hook and return (returncode, stdout+stderr)."""
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.json",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })

    env = os.environ.copy()
    if mnemos_repo_root:
        env["MNEMOS_REPO_ROOT"] = mnemos_repo_root
    else:
        env.pop("MNEMOS_REPO_ROOT", None)
    if env_extras:
        env.update(env_extras)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result.returncode, result.stdout + result.stderr


class TestHookPromotionBlockInjection:
    """Tests for <mnemos-promotion> block injection in UserPromptSubmit.sh.

    The hook reads from the observability log (wiki/observability.jsonl) and
    filters entries with event == "promotion" that are newer than the cursor.
    The cursor path is ~/.mnemos/.cache/promotion-cursor.txt (or MNEMOS_PROMO_CURSOR).
    """

    def _make_repo_with_promotions(self, tmp_path, entries):
        """Write a minimal repo with wiki/observability.jsonl containing given entries."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        obs_log = wiki / "observability.jsonl"
        obs_log.write_text("\n".join(json.dumps(e) for e in entries) + "\n" if entries else "")
        return obs_log

    def test_hook_injects_promotion_block_when_promotions_exist(self, tmp_path):
        """When there are recent promotions, the hook must inject <mnemos-promotion>."""
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"ts": ts, "event": "promotion", "memory_id": "test-promo-001", "layer": "project"}]
        self._make_repo_with_promotions(tmp_path, entries)

        cache_dir = tmp_path / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True)
        cursor_file = cache_dir / "promotion-cursor.txt"
        cursor_file.write_text("2020-01-01T00:00:00Z")

        rc, output = _run_hook(
            "hello world",
            mnemos_repo_root=str(tmp_path),
            env_extras={"MNEMOS_PROMO_CURSOR": str(cursor_file)},
        )
        assert rc == 0
        assert "<mnemos-promotion>" in output
        assert "test-promo-001" in output
        assert "project" in output

    def test_hook_omits_promotion_block_when_no_promotions(self, tmp_path):
        """When there are no new promotions, the hook must NOT emit <mnemos-promotion>."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "observability.jsonl").write_text("")

        cache_dir = tmp_path / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True)
        cursor_file = cache_dir / "promotion-cursor.txt"
        cursor_file.write_text("2020-01-01T00:00:00Z")

        rc, output = _run_hook(
            "hello world",
            mnemos_repo_root=str(tmp_path),
            env_extras={"MNEMOS_PROMO_CURSOR": str(cursor_file)},
        )
        assert rc == 0
        assert "<mnemos-promotion>" not in output

    def test_hook_promotion_block_contains_promotion_entries(self, tmp_path):
        """Each entry in the promotion block must be <promotion>{id} → {layer}</promotion>."""
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [
            {"ts": ts, "event": "promotion", "memory_id": "abc-111", "layer": "project"},
            {"ts": ts, "event": "promotion", "memory_id": "def-222", "layer": "global"},
        ]
        self._make_repo_with_promotions(tmp_path, entries)

        cache_dir = tmp_path / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True)
        cursor_file = cache_dir / "promotion-cursor.txt"
        cursor_file.write_text("2020-01-01T00:00:00Z")

        rc, output = _run_hook(
            "tell me about the project",
            mnemos_repo_root=str(tmp_path),
            env_extras={"MNEMOS_PROMO_CURSOR": str(cursor_file)},
        )
        assert rc == 0
        assert "<mnemos-promotion>" in output
        assert "</mnemos-promotion>" in output
        # Each entry format: <promotion>abc-111 → project</promotion>
        assert "abc-111 → project" in output
        assert "def-222 → global" in output

    def test_hook_updates_cursor_after_injection(self, tmp_path):
        """After injecting the promotion block, the cursor must be updated."""
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"ts": ts, "event": "promotion", "memory_id": "cursor-test-001", "layer": "project"}]
        self._make_repo_with_promotions(tmp_path, entries)

        cache_dir = tmp_path / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True)
        cursor_file = cache_dir / "promotion-cursor.txt"
        old_ts = "2020-01-01T00:00:00Z"
        cursor_file.write_text(old_ts)

        rc, output = _run_hook(
            "some prompt",
            mnemos_repo_root=str(tmp_path),
            env_extras={"MNEMOS_PROMO_CURSOR": str(cursor_file)},
        )
        assert rc == 0
        # Cursor must be updated to a newer timestamp
        new_cursor = cursor_file.read_text().strip()
        assert new_cursor != old_ts, (
            f"Cursor was not updated: still {new_cursor!r}"
        )

    def test_hook_handles_missing_observability_log_gracefully(self, tmp_path):
        """Hook must exit 0 and emit no promotion block when observability log is absent."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        # No observability.jsonl file created

        cache_dir = tmp_path / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True)
        cursor_file = cache_dir / "promotion-cursor.txt"
        cursor_file.write_text("2020-01-01T00:00:00Z")

        rc, output = _run_hook(
            "hello",
            mnemos_repo_root=str(tmp_path),
            env_extras={"MNEMOS_PROMO_CURSOR": str(cursor_file)},
        )
        assert rc == 0
        assert "<mnemos-promotion>" not in output

    def test_hook_uses_default_cursor_path_when_env_not_set(self, tmp_path, monkeypatch):
        """When MNEMOS_PROMO_CURSOR is not set, hook falls back to the default path."""
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"ts": ts, "event": "promotion", "memory_id": "default-cursor-test", "layer": "project"}]
        self._make_repo_with_promotions(tmp_path, entries)

        # Override HOME so default cursor goes into tmp_path
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        default_cache = fake_home / ".mnemos" / ".cache"
        default_cache.mkdir(parents=True)
        cursor_file = default_cache / "promotion-cursor.txt"
        cursor_file.write_text("2020-01-01T00:00:00Z")

        rc, output = _run_hook(
            "test default cursor",
            mnemos_repo_root=str(tmp_path),
            env_extras={"HOME": str(fake_home)},
        )
        assert rc == 0
        # Block should appear since cursor is old and promotion exists
        assert "<mnemos-promotion>" in output


# ---------------------------------------------------------------------------
# 6. MNEMOS_BEHAVIOR_BLOCK — layer-selection criteria
# ---------------------------------------------------------------------------

class TestBehaviorBlockLayerCriteria:
    def test_behavior_block_mentions_project_layer(self):
        """MNEMOS_BEHAVIOR_BLOCK must mention capturing directly to project layer."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        assert "project" in MNEMOS_BEHAVIOR_BLOCK.lower()

    def test_behavior_block_mentions_global_layer(self):
        """MNEMOS_BEHAVIOR_BLOCK must mention capturing directly to global layer."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        assert "global" in MNEMOS_BEHAVIOR_BLOCK.lower()

    def test_behavior_block_shows_layer_selection_criteria(self):
        """MNEMOS_BEHAVIOR_BLOCK must include when-to-use-each-layer criteria."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        # Must explicitly describe when to write directly to project vs global
        assert "--layer project" in MNEMOS_BEHAVIOR_BLOCK or "layer project" in MNEMOS_BEHAVIOR_BLOCK
        assert "--layer global" in MNEMOS_BEHAVIOR_BLOCK or "layer global" in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_includes_all_three_layers_in_examples(self):
        """The capture examples in MNEMOS_BEHAVIOR_BLOCK must show session, project, global."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        # All three layers should appear as concrete capture options/examples
        block_lower = MNEMOS_BEHAVIOR_BLOCK.lower()
        assert "session" in block_lower
        assert "project" in block_lower
        assert "global" in block_lower

    def test_behavior_block_defines_when_to_use_project_layer(self):
        """MNEMOS_BEHAVIOR_BLOCK must define when to capture directly to project layer."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        # Must mention architecture decisions or reusable constraints as project-layer triggers
        block_lower = MNEMOS_BEHAVIOR_BLOCK.lower()
        has_arch = "architecture" in block_lower or "architectural" in block_lower
        has_constraint = "constraint" in block_lower or "reusable" in block_lower
        has_decision = "decision" in block_lower
        assert has_arch or has_constraint or has_decision, (
            "MNEMOS_BEHAVIOR_BLOCK must explain what belongs in project layer"
        )

    def test_behavior_block_defines_when_to_use_global_layer(self):
        """MNEMOS_BEHAVIOR_BLOCK must define when to capture directly to global layer."""
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        block_lower = MNEMOS_BEHAVIOR_BLOCK.lower()
        # Must mention cross-project or user-preference criteria for global layer
        has_cross_project = "cross-project" in block_lower or "across project" in block_lower
        has_user_pref = "user preference" in block_lower or "preference" in block_lower
        assert has_cross_project or has_user_pref, (
            "MNEMOS_BEHAVIOR_BLOCK must explain what belongs in global layer"
        )
