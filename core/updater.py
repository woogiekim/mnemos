"""mnemos update — self-update logic.

Sequence:
  1. git pull origin main  (in the mnemos source repo)
  2. pipx reinstall mnemos
  3. Replace managed blocks in host config files, printing a before/after diff.

Managed block sentinels
-----------------------
settings.json  : managed mnemos hook entries for PostToolUse,
                 UserPromptSubmit, and Stop
CLAUDE.md      : <!-- mnemos-start --> … <!-- mnemos-end -->
cursor rules   : <!-- mnemos:start --> … <!-- mnemos:end -->
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from core.adapters.claude import CLAUDE_MD_BLOCK
from core.adapters.cursor import CURSOR_RULES_BLOCK


_UPDATE_REEXEC_ENV = "MNEMOS_UPDATE_REEXEC"
_UPDATE_ENTRYPOINT = "from core.cli import cli; cli()"


# Hook definitions that install.sh injects.  The MNEMOS_REPO_ROOT placeholder
# is expanded at install time; on update we keep whatever value is already
# recorded in the existing hook command (we preserve the path, only the
# structure/matcher is normalised).
_POST_TOOL_USE_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "timeout": 10,
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" '
                'bash "{repo_root}/hooks/PostToolUse.sh" 2>/dev/null || true'
            ),
        }
    ],
}

_USER_PROMPT_SUBMIT_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" '
                'bash "{repo_root}/hooks/UserPromptSubmit.sh" 2>/dev/null || true'
            ),
        }
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unified_diff(label: str, before: str, after: str) -> str:
    """Return a unified-diff string (or empty string if unchanged)."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{label} (before)",
            tofile=f"{label} (after)",
        )
    )
    return "".join(diff_lines)


def _run(cmd: list[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a subprocess, streaming output to stdout/stderr."""
    return subprocess.run(cmd, cwd=cwd, check=True)


# ---------------------------------------------------------------------------
# Step 1 + 2 — git pull + pipx reinstall
# ---------------------------------------------------------------------------

def git_pull(repo_root: str) -> None:
    """Run git pull --rebase origin main in repo_root."""
    _run(["git", "pull", "--rebase", "origin", "main"], cwd=repo_root)


def _git_revision(repo_root: str) -> Optional[str]:
    """Return the checkout's current commit, or ``None`` when unavailable."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    revision = result.stdout.strip()
    return revision or None


def _stash_if_dirty(repo_root: str) -> bool:
    """Stash local changes in repo_root if the working tree is dirty.

    Returns True if a stash was created (so the caller can pop it later).
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if stash_result.returncode == 0 and "No local changes to save" not in stash_result.stdout:
        print(f"stashed local changes in {repo_root}")
        return True
    return False


def _stash_pop(repo_root: str) -> None:
    """Pop the most recent stash in repo_root, ignoring errors."""
    result = subprocess.run(
        ["git", "stash", "pop"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"restored stashed changes in {repo_root}")
    else:
        print(
            f"warning: git stash pop failed — your changes are saved in the stash.\n"
            f"  Run: cd {repo_root} && git stash pop",
            file=sys.stderr,
        )


def pipx_reinstall() -> None:
    """Run pipx reinstall mnemos."""
    _run(["pipx", "reinstall", "mnemos"])


def _run_updated_process(
    repo_root: str,
    *,
    with_qmd: bool = False,
    qmd_package_manager: str = "auto",
) -> int:
    """Finish the update in a process that imports the refreshed source tree."""
    env = os.environ.copy()
    env[_UPDATE_REEXEC_ENV] = "1"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else repo_root
    )

    command = [
        sys.executable,
        "-c",
        _UPDATE_ENTRYPOINT,
        "update",
        "--repo-root",
        repo_root,
        "--skip-git-pull",
        "--skip-pipx",
    ]
    if with_qmd:
        command.extend(["--with-qmd", "--qmd-package-manager", qmd_package_manager])
    result = subprocess.run(command, cwd=repo_root, env=env)
    return result.returncode


# ---------------------------------------------------------------------------
# Step 1b — sync updated source directories to the install location
# ---------------------------------------------------------------------------

_SYNC_DIRS = ("core", "agents", "hooks")


def sync_source_to_install(repo_root: str, install_root: Optional[Path] = None) -> list[str]:
    """Copy updated source directories from repo_root to the install location.

    After ``git pull`` updates the dev repo, the editable-install target
    (``~/.mnemos/core/``, ``~/.mnemos/agents/``, and ``~/.mnemos/hooks/``)
    must be refreshed so the running binary and registered host hooks load the
    new code.

    Args:
        repo_root: Path to the source dev repo (e.g. ~/Development/mnemos).
        install_root: Override the install location.  Defaults to ``~/.mnemos``.

    Returns:
        List of directory paths that were synced (relative names such as
        ``"core"``, ``"agents"``, and ``"hooks"``). Directories that are
        absent in ``repo_root`` are silently skipped.
    """
    if install_root is None:
        install_root = Path.home() / ".mnemos"

    repo_path = Path(repo_root)
    synced: list[str] = []

    for dir_name in _SYNC_DIRS:
        src = repo_path / dir_name
        dst = install_root / dir_name
        if not src.is_dir():
            continue
        # Skip when source and destination are the same path (editable install)
        try:
            if src.resolve() == dst.resolve():
                synced.append(dir_name)
                continue
        except OSError:
            pass
        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        synced.append(dir_name)

    return synced


# ---------------------------------------------------------------------------
# Step 3a — ~/.claude/settings.json  (hook replacement)
# ---------------------------------------------------------------------------

def _is_mnemos_hook_entry(entry: dict) -> bool:
    """Return True if this hook-list entry contains any mnemos command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if (
            "mnemos ingest-claude-md" in cmd
            or "mnemos search" in cmd
            or "PostToolUse.sh" in cmd
            or "UserPromptSubmit.sh" in cmd
            or "Stop.sh" in cmd
        ):
            return True
    return False


def _extract_repo_root_from_hook(entry: dict) -> str:
    """Try to extract the MNEMOS_REPO_ROOT value from an existing hook command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        m = re.search(r'MNEMOS_REPO_ROOT="([^"]+)"', cmd)
        if m:
            return m.group(1)
    return os.environ.get("MNEMOS_REPO_ROOT", "")


def update_settings_json(settings_path: Path) -> tuple[bool, str]:
    """Replace mnemos hook entries in settings.json with canonical versions.

    Returns (changed, diff_text).
    """
    if not settings_path.exists():
        return False, ""

    original_text = settings_path.read_text()
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse {settings_path}: {exc}") from exc

    hooks = data.get("hooks", {})
    changed = False

    # Collect repo_root from existing entries (prefer first found)
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    for hook_type in ("PostToolUse", "UserPromptSubmit"):
        for entry in hooks.get(hook_type, []):
            if _is_mnemos_hook_entry(entry):
                found = _extract_repo_root_from_hook(entry)
                if found:
                    repo_root = found
                    break
        if repo_root:
            break

    # Stop hook: replace any legacy mnemos Stop entries (including the
    # old extract-insight hook removed in Issue #4) with the canonical
    # Stop.sh entry — which is now safe because gateway.capture() deduplicates
    # writes by (layer, NFKC-normalised content hash).
    #
    # This function is the low-level helper used by TestUpdateSettingsJson tests;
    # the ClaudeCodeAdapter._update_settings_json() path handles the full
    # install/update including Stop hook registration.  This function is kept
    # for backwards-compatibility with the updater module API.
    #
    # LEGACY BEHAVIOUR PRESERVED: entries that contain any legacy mnemos command
    # (extract-insight, mnemos search, etc.) are removed.  The caller (adapter)
    # is responsible for installing the canonical Stop.sh entry separately.
    if "Stop" in hooks:
        stop_list = hooks["Stop"]
        has_legacy_mnemos_stop = any(
            any(
                ("mnemos" in h.get("command", ""))
                and ("Stop.sh" not in h.get("command", ""))
                for h in entry.get("hooks", [])
            )
            for entry in stop_list
        )
        if has_legacy_mnemos_stop:
            cleaned_stop = [
                e for e in stop_list
                if not any(
                    ("mnemos" in h.get("command", ""))
                    and ("Stop.sh" not in h.get("command", ""))
                    for h in e.get("hooks", [])
                )
            ]
            changed = True
            if cleaned_stop:
                hooks["Stop"] = cleaned_stop
            else:
                del hooks["Stop"]

    # Remove all existing mnemos hook entries and replace with canonical ones
    for hook_type, template in [
        ("PostToolUse", _POST_TOOL_USE_HOOK_TEMPLATE),
        ("UserPromptSubmit", _USER_PROMPT_SUBMIT_HOOK_TEMPLATE),
    ]:
        hook_list = hooks.get(hook_type, [])
        # Strip old mnemos entries
        non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        # Build canonical replacement entry (fill in repo_root)
        canonical = json.loads(
            json.dumps(template).replace("{repo_root}", repo_root)
        )
        new_list = non_mnemos + [canonical]
        if new_list != hook_list:
            changed = True
        hooks[hook_type] = new_list

    if not changed:
        return False, ""

    data["hooks"] = hooks
    new_text = json.dumps(data, indent=2) + "\n"
    diff = _unified_diff(str(settings_path), original_text, new_text)
    settings_path.write_text(new_text)
    return True, diff


# ---------------------------------------------------------------------------
# Step 3b — ~/.claude/CLAUDE.md
# ---------------------------------------------------------------------------

def update_claude_md(claude_md_path: Path) -> tuple[bool, str]:
    """Replace <!-- mnemos-start --> … <!-- mnemos-end --> block.

    Returns (changed, diff_text).
    """
    if not claude_md_path.exists():
        return False, ""

    original = claude_md_path.read_text()
    pattern = re.compile(
        r"<!-- mnemos-start -->.*?<!-- mnemos-end -->",
        re.DOTALL,
    )

    if pattern.search(original):
        updated = pattern.sub(CLAUDE_MD_BLOCK, original)
    else:
        # Block not present — append it
        updated = original.rstrip("\n") + "\n\n" + CLAUDE_MD_BLOCK + "\n"

    if updated == original:
        return False, ""

    diff = _unified_diff(str(claude_md_path), original, updated)
    claude_md_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# Step 3c — ~/.cursor/rules
# ---------------------------------------------------------------------------

def _find_cursor_rules(cursor_dir: Path) -> Optional[Path]:
    """Return ~/.cursor/rules, ~/.cursor/rules.md, or None."""
    for name in ("rules", "rules.md"):
        p = cursor_dir / name
        if p.exists():
            return p
    return None


def update_cursor_rules(cursor_dir: Path) -> tuple[bool, str]:
    """Replace <!-- mnemos:start --> … <!-- mnemos:end --> block.

    Returns (changed, diff_text).
    """
    rules_path = _find_cursor_rules(cursor_dir)
    if rules_path is None:
        return False, ""

    original = rules_path.read_text()
    pattern = re.compile(
        r"<!-- mnemos:start -->.*?<!-- mnemos:end -->",
        re.DOTALL,
    )

    if pattern.search(original):
        updated = pattern.sub(CURSOR_RULES_BLOCK, original)
    else:
        # Block not present — append it
        updated = original.rstrip("\n") + "\n\n" + CURSOR_RULES_BLOCK + "\n"

    if updated == original:
        return False, ""

    diff = _unified_diff(str(rules_path), original, updated)
    rules_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def _run_bg_check_quiet() -> None:
    """Run a non-mutating update health check in-process.

    Invokes :func:`core.bg.run_background_check` directly (no subprocess) so
    the update step works even when the newly-reinstalled ``mnemos`` binary is
    not yet on PATH. Output is suppressed because --quiet is the intended
    behaviour in update context.
    """
    from core.bg import run_background_check

    repo_root = os.environ.get("MNEMOS_REPO_ROOT")
    if not repo_root:
        # Fall back to the repo root inferred from this module's location
        repo_root = str(Path(__file__).resolve().parents[1])

    # Force a run (bypass throttle), but do not let update drain mutating
    # maintenance. GC, promotion, and distillation can write many vault files
    # and trigger git sync, which makes a package update look hung.
    run_background_check(
        repo_root=repo_root,
        force=True,
        gc_enabled=False,
        auto_promote_enabled=False,
        auto_distill_enabled=False,
        dedup_enabled=True,
    )


def run_update(
    repo_root: Optional[str] = None,
    skip_git_pull: bool = False,
    skip_pipx: bool = False,
    home: Optional[Path] = None,
    *,
    with_qmd: bool = False,
    qmd_package_manager: str = "auto",
) -> int:
    """Run the full update sequence.  Returns exit code (0 = success)."""
    from core.adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter

    if home is None:
        home = Path.home()

    # Resolve repo_root
    if repo_root is None:
        repo_root = os.environ.get("MNEMOS_REPO_ROOT")
    if not repo_root:
        # Attempt to find it from pipx venv
        repo_root = str(Path(__file__).resolve().parents[1])

    exit_code = 0
    source_revision_before: Optional[str] = None
    source_revision_after: Optional[str] = None

    # -- 1. git pull ---------------------------------------------------------
    if not skip_git_pull:
        source_revision_before = _git_revision(repo_root)
        print("── git pull --rebase origin main ─────────────────────────────")
        # Auto-stash any local changes before pulling, restore after.
        stashed = _stash_if_dirty(repo_root)
        pull_ok = True
        try:
            git_pull(repo_root)
        except subprocess.CalledProcessError as exc:
            pull_ok = False
            cmd_str = " ".join(exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
            print(
                f"warning: git pull failed — '{cmd_str}' exited with code {exc.returncode}\n"
                f"         Installing from a potentially stale source is unsafe.\n"
                f"\nTo recover manually:\n"
                f"  cd {repo_root} && git pull --rebase origin main\n"
                f"  Then re-run: mnemos update\n"
                f"\nIf there are local commits conflicting with remote:\n"
                f"  cd {repo_root} && git rebase --abort\n"
                f"  cd {repo_root} && git reset --hard origin/main  # discards local commits",
                file=sys.stderr,
            )
        finally:
            if stashed:
                _stash_pop(repo_root)

        if not pull_ok:
            print("\n── update aborted ────────────────────────────────────────────────")
            return 1

        source_revision_after = _git_revision(repo_root)

    # -- 1b. sync updated source directories to install location -------------
    # git pull updates the dev repo but ~/.mnemos/core/ and ~/.mnemos/agents/
    # are separate copies that must be refreshed so the running binary loads
    # the new code.  This step is always attempted when repo_root is known.
    print("\n── syncing source directories to install location ───────────────")
    try:
        synced = sync_source_to_install(repo_root, install_root=home / ".mnemos")
        if synced:
            print(f"synced: {', '.join(synced)}")
        else:
            print("sync: nothing to sync (source directories not found)")
    except Exception as exc:
        print(f"warning: sync failed — {exc}", file=sys.stderr)

    # -- 2. pipx reinstall ---------------------------------------------------
    if not skip_pipx:
        print("\n── pipx reinstall mnemos ─────────────────────────────────────")
        try:
            pipx_reinstall()
        except subprocess.CalledProcessError as exc:
            print(f"warning: pipx reinstall failed — {exc}", file=sys.stderr)
            exit_code = 1

    source_changed = (
        source_revision_before is not None
        and source_revision_after is not None
        and source_revision_before != source_revision_after
    )
    if source_changed and os.environ.get(_UPDATE_REEXEC_ENV) != "1":
        if exit_code != 0:
            print(
                "warning: refreshed runtime was not started because pipx reinstall failed",
                file=sys.stderr,
            )
            return exit_code

        print("\n── continuing with refreshed runtime ───────────────────────────")
        sys.stdout.flush()
        sys.stderr.flush()
        return _run_updated_process(
            repo_root,
            with_qmd=with_qmd,
            qmd_package_manager=qmd_package_manager,
        )

    # -- 3. Replace managed blocks via adapters (run ALL — not filtered by is_present) --
    print("\n── updating managed config blocks ───────────────────────────────")

    adapter_list = [ClaudeCodeAdapter(), CursorAdapter(), CodexAdapter()]
    for adapter in adapter_list:
        try:
            messages = adapter.update(home)
            for msg in messages:
                if msg.startswith("[updated]"):
                    print(f"\n{msg}")
                else:
                    print(msg)
        except Exception as exc:
            print(
                f"warning: {adapter.name} adapter update failed — {exc}",
                file=sys.stderr,
            )

    # -- 4. Policy migration — add transient layer if absent ----------------
    # Installations predating the transient layer will be missing it in
    # wiki/policy.yaml, causing `mnemos capture --layer transient` to fail
    # with "Unknown layer 'transient'".  The migration is idempotent and
    # non-fatal; a warning is emitted if it fails so the user can investigate.
    print("\n── migrating policy: transient layer ─────────────────────────────")
    try:
        from core.install import migrate_policy_transient

        repo_root_path = Path(repo_root)
        changed = migrate_policy_transient(repo_root_path)
        if changed:
            print("policy updated: transient layer added to wiki/policy.yaml")
        else:
            print("policy ok: transient layer already present")
    except Exception as exc:
        print(f"warning: policy migration failed — {exc}", file=sys.stderr)

    # -- 5. Optional QMD bootstrap ------------------------------------------
    if with_qmd:
        print("\n── preparing optional QMD backend ───────────────────────────────")
        try:
            from core.qmd_bootstrap import bootstrap_qmd

            result = bootstrap_qmd(repo_root, package_manager=qmd_package_manager)
            print(
                f"qmd: installed={result['installed']} prepared={result['prepared']} "
                f"config={result['config_path']}"
            )
        except Exception as exc:
            print(f"warning: qmd bootstrap failed — {exc}", file=sys.stderr)
            exit_code = 1

    # -- 6. Run non-mutating bg-check --quiet -------------------------------
    # This step runs silently so the update summary stays readable. It must
    # not drain write-heavy maintenance because update is an install workflow.
    # Failures are non-fatal — they are reported as warnings and do not change exit_code.
    print("\n── running mnemos bg-check --quiet ───────────────────────────────")
    try:
        _run_bg_check_quiet()
        print("bg-check: complete")
    except Exception as exc:
        print(f"warning: bg-check failed — {exc}", file=sys.stderr)

    print("\n── update complete ───────────────────────────────────────────────")
    return exit_code
