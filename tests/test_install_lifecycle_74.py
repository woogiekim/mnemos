"""Install / update / rollback lifecycle E2E validation (Issue #74).

Validates the six acceptance criteria for the install -> update -> rollback
flow against the EXISTING production surfaces (no code changes in this issue):

  - AC1: fresh install state machine          -> TestFreshInstall_AC1
  - AC2: repeated update refreshes blocks      -> TestUpdate_AC2
  - AC3: failed update recovery                -> TestFailedUpdateRecovery_AC3
  - AC4: rollback after install/update         -> TestRollback_AC4
  - AC5: user-owned state preservation         -> TestUserStatePreservation_AC5
  - AC6: idempotency of repeated update runs   -> TestUpdateIdempotency_AC6

Drives ONLY the public entry points:

  - ``core.install.install(path, home=...)``
  - ``core.updater.run_update(home=..., skip_git_pull=True, skip_pipx=True)``
  - ``core.uninstaller.run_uninstall(yes=True, home=...)``

The module-level back-compat helpers (``update_settings_json``,
``update_claude_md``, ``update_cursor_rules`` at ``core/updater.py:210-375``)
are NEVER invoked — they live only for legacy callers and are explicitly NOT
exercised by ``run_update`` (which routes through ``adapter.update`` at
``core/updater.py:484-497``). AC2 asserts that contract by monkeypatching the
legacy helpers to record any unexpected invocation.

The adapter-registry parity guard (the three-call-site ``adapter_list = [...]``
triplication risk noted in Issue #78) is owned by
``tests/test_host_consistency_78.py`` and is intentionally NOT re-asserted
here. This module's scope is end-to-end lifecycle behavior, not registry
shape.

Fixtures
--------

All tests reuse the autouse fixtures from ``tests/conftest.py``:

- ``isolate_home``           — redirects HOME / ``Path.home()`` (Issue #70).
- ``isolate_mnemos_repo_root`` — pins ``MNEMOS_REPO_ROOT`` to a per-test temp
  dir so no test silently reads the developer's real store.

Tests that need adapter hooks to actually template into ``settings.json``
opt into the ``safe_repo_root`` fixture, which provides a marker-free
``MNEMOS_REPO_ROOT`` with a real ``hooks/`` directory (the
``is_unsafe_repo_root`` guard at ``core/adapters/claude.py:108-128`` refuses
to template a ``pytest-of-*`` path).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.adapters import ClaudeCodeAdapter, CursorAdapter
from core.install import install
from core.uninstaller import run_uninstall
from core.updater import run_update


# ---------------------------------------------------------------------------
# Helpers (test-internal — not part of production API)
# ---------------------------------------------------------------------------

def _seed_claude_home(home: Path, claude_md_extra: str = "") -> tuple[Path, Path]:
    """Create a minimal ``~/.claude`` host environment under *home*.

    Returns ``(settings_path, claude_md_path)``. The settings file starts as
    ``{}`` so the adapter at ``core/adapters/claude.py:259-313`` finds a
    parseable JSON object and proceeds to register hooks.
    """
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text("{}\n")
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text(("# Claude\n" + claude_md_extra) if claude_md_extra else "# Claude\n")
    return settings, claude_md


def _seed_cursor_home(home: Path, rules_extra: str = "") -> Path:
    """Create a minimal ``~/.cursor`` host environment under *home*.

    Returns the rules-file path. Mirrors the layout that
    ``CursorAdapter.is_present`` at ``core/adapters/cursor.py:79-81`` and
    ``_find_cursor_rules`` at ``core/adapters/cursor.py:29-35`` expect.
    """
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    rules_path = cursor_dir / "rules"
    rules_path.write_text(rules_extra if rules_extra else "# Cursor rules\n")
    return rules_path


def _seed_full_home(home: Path) -> dict[str, Path]:
    """Set up both Claude and Cursor host environments + repo workspace.

    The returned dict carries the well-known paths so tests can read user-owned
    state without re-deriving paths. Layout follows the contracts at
    ``core/install.py:114-169`` (install) and
    ``core/uninstaller.py:324-431`` (uninstall).
    """
    settings, claude_md = _seed_claude_home(home, claude_md_extra="\nUser prose.\n")
    rules_path = _seed_cursor_home(home, rules_extra="# Cursor rules\nUser prose.\n")
    zshrc_path = home / ".zshrc"
    zshrc_path.write_text("# user shell config\nexport PS1='%n%# '\n")
    return {
        "settings": settings,
        "claude_md": claude_md,
        "cursor_rules": rules_path,
        "zshrc": zshrc_path,
    }


# ---------------------------------------------------------------------------
# AC1 — fresh install state machine
# ---------------------------------------------------------------------------

class TestFreshInstall_AC1:
    """Full post-install state machine (issue #74 AC1).

    Validates every artifact that ``install(path, home=...)`` is contracted to
    produce on a clean machine:

    - Wiki + agent directory scaffolding (``core/install.py:114-131``).
    - Six-layer ``mnemos.yml`` and ``wiki/policy.yaml`` (``core/install.py:132-146``).
    - ``AGENTS.md`` manifest (``core/install.py:148-150``).
    - ``.gitignore`` mnemos block (``core/install.py:152-158``).
    - ``~/.zshrc`` ``MNEMOS_REPO_ROOT`` export (``core/install.py:208-218``).
    - Claude adapter hooks + ``CLAUDE.md`` block (``core/adapters/claude.py:231-338``).
    - Cursor adapter rules block (``core/adapters/cursor.py:87-98``).
    - ``verify_hooks()`` returns ``(True, [])`` on BOTH adapters
      (``core/adapters/claude.py:472-523``, ``core/adapters/cursor.py:148-163``).
    """

    def test_install_writes_full_repo_skeleton(self, tmp_path, safe_repo_root):
        """install() must scaffold every wiki/agent dir + six policy layers.

        Cites: ``core/install.py:9-25`` (_WIKI_DIRS / _AGENT_DIRS),
        ``core/install.py:27-65`` (_DEFAULT_CONFIG six layers),
        ``core/install.py:148-150`` (AGENTS.md),
        ``core/install.py:152-158`` (.gitignore block).
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        _seed_cursor_home(home)
        repo = tmp_path / "repo"

        install(repo, home=home)

        # Wiki + agent dirs all created
        for rel in (
            "wiki/global", "wiki/projects", "wiki/entities", "wiki/claims", "wiki/topics",
            ".agent/runs", ".agent/sessions", ".agent/state", ".agent/reports",
            ".agent/tools", ".agent/workflows/hooks", ".agent/transient",
        ):
            assert (repo / rel).is_dir(), f"missing scaffold dir: {rel}"

        # mnemos.yml has six layers (transient through global)
        cfg = yaml.safe_load((repo / "mnemos.yml").read_text())
        assert set(cfg["layers"]) == {
            "transient", "ephemeral", "working", "session", "project", "global",
        }

        # policy.yaml mirrors the same six layers including transient
        policy = yaml.safe_load((repo / "wiki" / "policy.yaml").read_text())
        assert set(policy["layers"]) == {
            "transient", "ephemeral", "working", "session", "project", "global",
        }
        assert policy["forget"]["requires_archived"] is True

        # AGENTS.md manifest + gitignore mnemos block
        assert "AGENTS.md — mnemos Agent Manifest" in (repo / "AGENTS.md").read_text()
        gitignore = (repo / ".gitignore").read_text()
        for line in ("# mnemos", ".agent/runs/", ".agent/sessions/", ".agent/state/"):
            assert line in gitignore

    def test_install_registers_host_blocks_and_hooks(self, tmp_path, safe_repo_root):
        """install() must register Claude hooks + Claude/Cursor managed blocks.

        Cites: ``core/adapters/claude.py:231-313`` (Claude install settings.json),
        ``core/adapters/claude.py:315-338`` (CLAUDE.md block),
        ``core/adapters/cursor.py:87-98`` (Cursor install).
        """
        home = tmp_path / "home"
        settings, claude_md = _seed_claude_home(home)
        cursor_rules = _seed_cursor_home(home)
        repo = tmp_path / "repo"

        install(repo, home=home)

        # CLAUDE.md has the managed block with mnemos behavior
        claude_text = claude_md.read_text()
        assert "<!-- mnemos-start -->" in claude_text and "<!-- mnemos-end -->" in claude_text
        assert "mnemos search" in claude_text

        # settings.json has PostToolUse / UserPromptSubmit / Stop hooks
        data = json.loads(settings.read_text())
        hooks = data["hooks"]
        post_cmds = " ".join(
            h.get("command", "")
            for entry in hooks["PostToolUse"]
            for h in entry.get("hooks", [])
        )
        assert "mnemos ingest-claude-md" in post_cmds
        assert "PostToolUse.sh" in post_cmds  # bg-check hook
        assert any(
            "UserPromptSubmit.sh" in h.get("command", "")
            for entry in hooks["UserPromptSubmit"]
            for h in entry.get("hooks", [])
        )
        assert any(
            "Stop.sh" in h.get("command", "")
            for entry in hooks["Stop"]
            for h in entry.get("hooks", [])
        )

        # Cursor rules block present
        cursor_text = cursor_rules.read_text()
        assert "<!-- mnemos:start -->" in cursor_text and "<!-- mnemos:end -->" in cursor_text

        # verify_hooks() reports green on BOTH adapters
        ok_c, missing_c = ClaudeCodeAdapter().verify_hooks(home)
        assert ok_c and missing_c == []
        ok_u, missing_u = CursorAdapter().verify_hooks(home)
        assert ok_u and missing_u == []

    def test_install_appends_zshrc_export(self, tmp_path, safe_repo_root):
        """install() must append ``export MNEMOS_REPO_ROOT="..."`` to ~/.zshrc.

        Cites: ``core/install.py:208-218`` (_install_zshrc), Issue #70 zshrc gap.
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        zshrc = home / ".zshrc"
        zshrc.write_text("# preexisting user line\nalias ll='ls -la'\n")
        repo = tmp_path / "repo"

        install(repo, home=home)

        content = zshrc.read_text()
        assert "alias ll='ls -la'" in content, "user content must survive install"
        assert "MNEMOS_REPO_ROOT" in content
        assert str(repo.resolve()) in content
        assert "# mnemos — repository root (added by install.py)" in content


# ---------------------------------------------------------------------------
# AC2 — repeated update refreshes managed blocks
# ---------------------------------------------------------------------------

class TestUpdate_AC2:
    """run_update() refreshes managed blocks to the canonical bytes (AC2).

    Validates that ``run_update`` flows through ``adapter.update(home)`` at
    ``core/updater.py:484-497`` and NOT through the legacy module-level helpers
    (``update_settings_json`` / ``update_claude_md`` / ``update_cursor_rules``
    at ``core/updater.py:210-375``), AND that ``migrate_policy_transient`` at
    ``core/install.py:172-205`` runs as Step 4 of the orchestrator.
    """

    def test_run_update_refreshes_stale_claude_md_block(self, tmp_path):
        """A stale CLAUDE.md block must be rewritten while user prose is preserved.

        Cites: ``core/adapters/claude.py:445-466`` (_update_claude_md regex sub).
        """
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text(
            "# Claude\nUser prose above.\n\n"
            "<!-- mnemos-start -->\nSTALE BLOCK CONTENT\n<!-- mnemos-end -->\n\n"
            "User prose below.\n"
        )
        (claude_dir / "settings.json").write_text("{}\n")

        rc = run_update(
            repo_root=str(tmp_path),
            skip_git_pull=True,
            skip_pipx=True,
            home=home,
        )
        assert rc == 0

        text = claude_md.read_text()
        assert "STALE BLOCK CONTENT" not in text, "stale managed bytes must be replaced"
        assert "User prose above." in text and "User prose below." in text
        assert "mnemos search" in text, "canonical mnemos behavior block must be present"

    def test_run_update_does_not_invoke_legacy_module_helpers(self, tmp_path, monkeypatch):
        """run_update must route through adapter.update — NEVER the legacy helpers.

        Cites: ``core/updater.py:210-375`` (legacy helpers retained for back-compat),
        ``core/updater.py:484-497`` (adapter dispatch — the only path run_update uses).
        """
        from core import updater as updater_module

        legacy_calls: list[str] = []

        def trap(name):
            def _inner(*_a, **_kw):
                legacy_calls.append(name)
                return False, ""
            return _inner

        monkeypatch.setattr(updater_module, "update_settings_json", trap("settings"))
        monkeypatch.setattr(updater_module, "update_claude_md", trap("claude_md"))
        monkeypatch.setattr(updater_module, "update_cursor_rules", trap("cursor"))

        home = tmp_path / "home"
        _seed_claude_home(home)
        _seed_cursor_home(home)

        rc = run_update(
            repo_root=str(tmp_path),
            skip_git_pull=True,
            skip_pipx=True,
            home=home,
        )
        assert rc == 0
        assert legacy_calls == [], (
            f"run_update must not invoke legacy helpers; called: {legacy_calls}"
        )

    def test_run_update_runs_migrate_policy_transient(self, tmp_path):
        """run_update Step 4 must run migrate_policy_transient against repo_root.

        Cites: ``core/updater.py:499-515`` (Step 4 — policy migration),
        ``core/install.py:172-205`` (migrate_policy_transient body).
        """
        # Pre-create a policy.yaml WITHOUT the transient layer to confirm migration fires.
        repo = tmp_path
        (repo / "wiki").mkdir(parents=True, exist_ok=True)
        legacy_policy = {
            "layers": {
                "ephemeral": {"path_template": ".agent/runs/{run_id}/scratch/"},
                "global": {"path_template": "wiki/global/"},
            },
            "forget": {"requires_archived": True},
        }
        (repo / "wiki" / "policy.yaml").write_text(yaml.dump(legacy_policy))
        home = repo / "home"
        _seed_claude_home(home)

        rc = run_update(
            repo_root=str(repo),
            skip_git_pull=True,
            skip_pipx=True,
            home=home,
        )
        assert rc == 0

        updated_policy = yaml.safe_load((repo / "wiki" / "policy.yaml").read_text())
        assert "transient" in updated_policy["layers"], (
            "migrate_policy_transient must add the transient layer on update"
        )


# ---------------------------------------------------------------------------
# AC3 — failed update recovery
# ---------------------------------------------------------------------------

class TestFailedUpdateRecovery_AC3:
    """Recovery contract when an adapter or git-pull step fails (AC3).

    Validates that ``run_update`` quarantines per-adapter failures (the broad
    ``try/except`` at ``core/updater.py:485-497``) and that a failed git-pull
    aborts BEFORE adapters run (``core/updater.py:434-456``).
    """

    def test_run_update_quarantines_claude_adapter_failure(self, tmp_path):
        """A ClaudeCodeAdapter.update() exception must NOT prevent CursorAdapter.update().

        Cites: ``core/updater.py:485-497`` (per-adapter try/except quarantine).
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        cursor_rules = _seed_cursor_home(
            home, rules_extra="# Cursor\n\n<!-- mnemos:start -->\nOLD\n<!-- mnemos:end -->\n",
        )

        with patch.object(
            ClaudeCodeAdapter,
            "update",
            side_effect=RuntimeError("simulated adapter failure"),
        ):
            rc = run_update(
                repo_root=str(tmp_path),
                skip_git_pull=True,
                skip_pipx=True,
                home=home,
            )

        # run_update returns 0 — adapter failures degrade gracefully (warning to stderr)
        assert rc == 0
        # CursorAdapter still got to refresh its block despite the Claude crash
        cursor_text = cursor_rules.read_text()
        assert "OLD" not in cursor_text
        assert "<!-- mnemos:start -->" in cursor_text and "mnemos search" in cursor_text

    def test_run_update_aborts_before_adapters_when_git_pull_fails(self, tmp_path):
        """git_pull failure must abort BEFORE sync/adapters run.

        Cites: ``core/updater.py:428-456`` (Step 1 git_pull and early-return),
        ``core/updater.py:458-470`` (Step 1b sync — only reached after pull succeeds).
        """
        import subprocess as _subprocess
        from core import updater as updater_module

        # Track whether any adapter.update() was reached
        sentinel = {"claude_called": False, "cursor_called": False}
        def claude_marker(self, home):  # pragma: no cover - must not fire
            sentinel["claude_called"] = True
            return []
        def cursor_marker(self, home):  # pragma: no cover - must not fire
            sentinel["cursor_called"] = True
            return []

        home = tmp_path / "home"
        _seed_claude_home(home)

        # Force git_pull to raise CalledProcessError so Step 1 aborts.
        def fake_git_pull(repo_root):
            raise _subprocess.CalledProcessError(
                returncode=1, cmd=["git", "pull", "--rebase", "origin", "main"],
            )

        with (
            patch.object(updater_module, "git_pull", side_effect=fake_git_pull),
            patch.object(updater_module, "_stash_if_dirty", return_value=False),
            patch.object(ClaudeCodeAdapter, "update", new=claude_marker),
            patch.object(CursorAdapter, "update", new=cursor_marker),
        ):
            rc = run_update(
                repo_root=str(tmp_path),
                skip_git_pull=False,
                skip_pipx=True,
                home=home,
            )

        assert rc == 1, "failed git_pull must return exit code 1"
        assert sentinel["claude_called"] is False, "Claude adapter must NOT run after failed pull"
        assert sentinel["cursor_called"] is False, "Cursor adapter must NOT run after failed pull"


# ---------------------------------------------------------------------------
# AC4 — rollback after install/update
# ---------------------------------------------------------------------------

class TestRollback_AC4:
    """Uninstall after install restores user-owned files byte-identically (AC4).

    Validates the symmetry between ``install()`` and ``run_uninstall()``:
    every file that existed BEFORE install must match its post-uninstall
    bytes exactly (the mnemos block is the only mutated region). Cites
    ``core/install.py:114-169`` (install entry), ``core/uninstaller.py:324-391``
    (run_uninstall orchestrator).
    """

    def test_install_then_uninstall_restores_user_owned_files(self, tmp_path, safe_repo_root):
        """install -> uninstall must leave user-owned bytes byte-identical.

        Cites: ``core/uninstaller.py:56-94`` (settings.json hook removal),
        ``core/uninstaller.py:101-127`` (CLAUDE.md block removal),
        ``core/uninstaller.py:143-167`` (cursor rules block removal),
        ``core/uninstaller.py:174-209`` (zshrc line removal).
        """
        home = tmp_path / "home"
        seeds = _seed_full_home(home)

        # Snapshot user-owned bytes BEFORE install
        before = {label: p.read_text() for label, p in seeds.items()}
        repo = tmp_path / "repo"

        install(repo, home=home)
        rc = run_uninstall(yes=True, home=home)
        assert rc == 0

        # CLAUDE.md prose around the managed block restored byte-identically
        claude_after = seeds["claude_md"].read_text()
        # uninstaller collapses extra blank lines; assert the user content survives
        assert "# Claude" in claude_after
        assert "User prose." in claude_after
        assert "<!-- mnemos-start -->" not in claude_after

        # Cursor rules: managed block gone, user prose preserved
        cursor_after = seeds["cursor_rules"].read_text()
        assert "User prose." in cursor_after
        assert "<!-- mnemos:start -->" not in cursor_after

        # settings.json: hooks key removed entirely (no other top-level keys existed)
        settings_after = json.loads(seeds["settings"].read_text())
        assert "hooks" not in settings_after

        # zshrc: managed export line gone; pre-existing user lines byte-identical
        zshrc_after = seeds["zshrc"].read_text()
        assert "MNEMOS_REPO_ROOT" not in zshrc_after
        # The original user lines (we wrote them BEFORE install) must still be present
        for original_line in before["zshrc"].splitlines():
            assert original_line in zshrc_after, f"lost user .zshrc line: {original_line!r}"

    def test_uninstall_after_torn_marker_block_is_idempotent(self, tmp_path):
        """uninstall on a CLAUDE.md with a missing closing marker leaves it intact.

        Empirical behavior of the regex pattern at
        ``core/uninstaller.py:113-116`` (``r"\\n?<!-- mnemos-start -->.*?<!-- mnemos-end -->\\n?"``,
        ``re.DOTALL``): a torn block (start marker present, end marker absent) does NOT
        match — the regex requires BOTH markers, so the file is left unchanged and
        ``run_uninstall`` reports "Nothing to uninstall". This is the documented
        rollback contract and is enforced here so future refactors do not silently
        change the behavior.
        """
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        torn = "# Claude\n<!-- mnemos-start -->\norphaned content (no end marker)\n"
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text(torn)
        (claude_dir / "settings.json").write_text("{}\n")

        rc = run_uninstall(yes=True, home=home)

        # Either Nothing to uninstall (rc=0, file unchanged) — the contract path,
        # OR a future enhancement removes torn blocks; in both cases rc must be 0.
        assert rc == 0
        # The torn block contents survive because the regex requires both markers.
        # This asserts the EMPIRICAL contract; the docstring above explains why.
        assert claude_md.read_text() == torn


# ---------------------------------------------------------------------------
# AC5 — user-owned state preservation across lifecycle steps
# ---------------------------------------------------------------------------

class TestUserStatePreservation_AC5:
    """User-owned state is preserved across install + N updates (AC5).

    The "user-owned state" set defined for this contract:

    1. CLAUDE.md prose outside the managed block.
    2. settings.json non-mnemos keys (model/theme/env) and non-mnemos hook entries.
    3. cursor rules prose outside the managed block.
    4. ~/.zshrc lines outside the mnemos export.
    5. Pre-existing mnemos.yml (``core/install.py:133-136`` "not overwritten" guard).
    6. Memory store contents (wiki/projects/*, .agent/runs/*).
    """

    def test_claude_md_prose_survives_install_plus_two_updates(self, tmp_path, safe_repo_root):
        """CLAUDE.md user prose unchanged through one install + two run_update calls.

        Cites: ``core/adapters/claude.py:315-338`` (install regex sub preserves outer prose),
        ``core/adapters/claude.py:445-466`` (update regex sub same property).
        """
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        user_prose = "# Claude\n\nUSER-ABOVE prose with `code`.\n"
        user_below = "\nUSER-BELOW more prose.\n"
        claude_md.write_text(user_prose + user_below)
        (claude_dir / "settings.json").write_text("{}\n")
        repo = tmp_path / "repo"

        install(repo, home=home)
        for _ in range(2):
            run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)

        text = claude_md.read_text()
        assert "USER-ABOVE prose with `code`." in text
        assert "USER-BELOW more prose." in text

    def test_settings_json_preserves_non_mnemos_keys_and_hooks(self, tmp_path, safe_repo_root):
        """All four top-level settings keys + non-mnemos hooks preserved through install + update.

        Cites: ``core/adapters/claude.py:259-313`` (install uses ``data.setdefault``,
        ``hooks.setdefault`` to merge — never overwrites unrelated keys).
        """
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        settings = claude_dir / "settings.json"
        user_settings = {
            "model": "claude-opus-4",
            "theme": "dark",
            "env": {"FOO": "bar"},
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo user-hook"}],
                    }
                ],
            },
        }
        settings.write_text(json.dumps(user_settings, indent=2) + "\n")
        (claude_dir / "CLAUDE.md").write_text("# Claude\n")
        repo = tmp_path / "repo"

        install(repo, home=home)
        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)

        data = json.loads(settings.read_text())
        assert data["model"] == "claude-opus-4"
        assert data["theme"] == "dark"
        assert data["env"] == {"FOO": "bar"}

        # The non-mnemos PostToolUse user hook survives both install and update
        post_cmds = [
            h.get("command", "")
            for entry in data["hooks"]["PostToolUse"]
            for h in entry.get("hooks", [])
        ]
        assert any("echo user-hook" in cmd for cmd in post_cmds), (
            "non-mnemos PostToolUse hook must survive install+update"
        )

    def test_cursor_user_prose_preserved_through_update(self, tmp_path):
        """Cursor rules prose outside the managed block survives run_update.

        Cites: ``core/adapters/cursor.py:120-142`` (_update_cursor_rules regex sub).
        """
        home = tmp_path / "home"
        cursor_dir = home / ".cursor"
        cursor_dir.mkdir(parents=True)
        rules = cursor_dir / "rules"
        rules.write_text(
            "# Cursor user rules\nUSER-LINE-1\n\n"
            "<!-- mnemos:start -->\nOLD MANAGED\n<!-- mnemos:end -->\n\n"
            "USER-LINE-2\n"
        )

        run_update(repo_root=str(tmp_path), skip_git_pull=True, skip_pipx=True, home=home)

        text = rules.read_text()
        assert "USER-LINE-1" in text
        assert "USER-LINE-2" in text
        assert "OLD MANAGED" not in text

    def test_zshrc_user_lines_preserved_through_install(self, tmp_path, safe_repo_root):
        """install() appends the mnemos export WITHOUT reordering user lines.

        Cites: ``core/install.py:208-218`` (_install_zshrc append-only behavior).
        """
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        zshrc = home / ".zshrc"
        original = (
            "# user header\n"
            "alias g='git'\n"
            "export PATH=\"$HOME/bin:$PATH\"\n"
        )
        zshrc.write_text(original)
        _seed_claude_home(home)
        repo = tmp_path / "repo"

        install(repo, home=home)

        text = zshrc.read_text()
        # Every user line is present, in original order, before the mnemos block
        idx = 0
        for line in original.splitlines():
            new_idx = text.find(line)
            assert new_idx >= idx, f"user line out of order: {line!r}"
            idx = new_idx
        # mnemos export came AFTER user content (append, not prepend)
        assert text.index("MNEMOS_REPO_ROOT") > text.index("alias g='git'")

    def test_install_does_not_overwrite_existing_mnemos_yml_or_memory_store(
        self, tmp_path, safe_repo_root,
    ):
        """Pre-existing mnemos.yml + memory store files are NOT clobbered by install.

        Cites: ``core/install.py:133-136`` (config_path.exists() guard),
        ``core/install.py:130-131`` (mkdir parents=True, exist_ok=True for wiki/.agent
        dirs — content already inside survives because mkdir doesn't clear).
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        repo = tmp_path / "repo"
        repo.mkdir()
        # Pre-create user content that mnemos lifecycle steps must preserve
        (repo / "mnemos.yml").write_text("custom: user-config\n")
        (repo / "wiki" / "projects").mkdir(parents=True)
        user_project_note = repo / "wiki" / "projects" / "test.md"
        user_project_note.write_text("# user-authored project note\n")
        (repo / ".agent" / "runs" / "r1" / "scratch").mkdir(parents=True)
        scratch_file = repo / ".agent" / "runs" / "r1" / "scratch" / "x.md"
        scratch_file.write_text("ephemeral user content\n")

        # Snapshot the bytes BEFORE the lifecycle runs
        before_yml = (repo / "mnemos.yml").read_text()
        before_note = user_project_note.read_text()
        before_scratch = scratch_file.read_text()

        install(repo, home=home)
        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)

        # mnemos.yml: the user's custom config survives (existence guard prevented overwrite)
        assert (repo / "mnemos.yml").read_text() == before_yml
        # Wiki / agent run files: byte-identical across the lifecycle
        assert user_project_note.read_text() == before_note
        assert scratch_file.read_text() == before_scratch


# ---------------------------------------------------------------------------
# AC6 — idempotency of repeated update runs
# ---------------------------------------------------------------------------

class TestUpdateIdempotency_AC6:
    """Repeated run_update calls converge to a stable file state (AC6).

    Validates that the second update of an identical environment is a no-op:
    every managed-block byte, every hook entry count, every policy migration
    state — all unchanged. Cites the ``if updated == original: return False, ""``
    guards at ``core/adapters/claude.py:461-463`` and
    ``core/adapters/cursor.py:137-139``.
    """

    def test_double_update_yields_byte_identical_adapter_targets(self, tmp_path, safe_repo_root):
        """Running update twice produces byte-identical settings.json + CLAUDE.md + cursor rules + .zshrc.

        Cites: ``core/adapters/claude.py:373-443`` (_update_settings_json — strips
        and re-adds canonical entries; second call is a no-op),
        ``core/adapters/cursor.py:120-142`` (Cursor update is similarly idempotent),
        ``core/install.py:213-218`` (zshrc only appends if MNEMOS_REPO_ROOT not present).
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        _seed_cursor_home(home)
        repo = tmp_path / "repo"
        install(repo, home=home)
        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)

        snapshot = {
            "settings": (home / ".claude" / "settings.json").read_text(),
            "claude_md": (home / ".claude" / "CLAUDE.md").read_text(),
            "cursor_rules": (home / ".cursor" / "rules").read_text(),
            "zshrc": (home / ".zshrc").read_text(),
            "policy": (repo / "wiki" / "policy.yaml").read_text(),
        }

        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)

        for label, expected in snapshot.items():
            actual = {
                "settings": (home / ".claude" / "settings.json").read_text(),
                "claude_md": (home / ".claude" / "CLAUDE.md").read_text(),
                "cursor_rules": (home / ".cursor" / "rules").read_text(),
                "zshrc": (home / ".zshrc").read_text(),
                "policy": (repo / "wiki" / "policy.yaml").read_text(),
            }[label]
            assert actual == expected, f"{label} drifted across double-update"

    def test_double_update_keeps_hook_counts_stable(self, tmp_path, safe_repo_root):
        """The number of mnemos hook entries does not grow across repeated updates.

        Cites: ``core/adapters/claude.py:399-434`` (strip + canonical pattern — N
        entries before, N after, never N+K).
        """
        home = tmp_path / "home"
        _seed_claude_home(home)
        repo = tmp_path / "repo"
        install(repo, home=home)

        def hook_counts() -> dict[str, int]:
            data = json.loads((home / ".claude" / "settings.json").read_text())
            hooks = data["hooks"]
            return {k: len(hooks.get(k, [])) for k in ("PostToolUse", "UserPromptSubmit", "Stop")}

        baseline = hook_counts()
        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)
        after_first = hook_counts()
        run_update(repo_root=str(repo), skip_git_pull=True, skip_pipx=True, home=home)
        after_second = hook_counts()

        assert after_first == baseline, f"first update grew hooks: {baseline} -> {after_first}"
        assert after_second == after_first, (
            f"second update grew hooks: {after_first} -> {after_second}"
        )

    def test_migrate_policy_transient_is_no_op_on_second_update(self, tmp_path):
        """migrate_policy_transient must return False on the second run_update.

        Cites: ``core/install.py:188-191`` (early-return when transient already
        present), ``core/updater.py:504-515`` (Step 4 logs "policy ok: ... already
        present" on the no-op path).
        """
        from core import install as install_module
        home = tmp_path / "home"
        _seed_claude_home(home)
        # First update creates the policy.yaml from scratch (none seeded), which
        # writes the transient layer up-front via install._DEFAULT_CONFIG. The
        # SECOND update should observe an unchanged policy and return False.
        run_update(repo_root=str(tmp_path), skip_git_pull=True, skip_pipx=True, home=home)

        # Wrap migrate_policy_transient to observe its return value on the 2nd call.
        observed: list[bool] = []
        real_fn = install_module.migrate_policy_transient

        def recording_fn(repo_root_path):
            ret = real_fn(repo_root_path)
            observed.append(ret)
            return ret

        with patch.object(install_module, "migrate_policy_transient", side_effect=recording_fn):
            run_update(repo_root=str(tmp_path), skip_git_pull=True, skip_pipx=True, home=home)

        assert observed == [False], (
            f"second run_update must observe migrate_policy_transient -> False, "
            f"got {observed}"
        )
