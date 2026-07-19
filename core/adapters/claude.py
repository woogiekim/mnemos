"""ClaudeCodeAdapter — manages mnemos integration with Claude Code."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from core.adapters.base import HostAdapter, MNEMOS_BEHAVIOR_BLOCK

if TYPE_CHECKING:
    from core.events import EventBus


# ---------------------------------------------------------------------------
# Canonical managed block content (mirrors install.sh)
# ---------------------------------------------------------------------------

CLAUDE_MD_BLOCK = (
    "<!-- mnemos-start -->\n"
    + MNEMOS_BEHAVIOR_BLOCK
    + "\n<!-- mnemos-end -->"
)

# Hook definitions that install.sh injects.
#
# Two PostToolUse entries are registered:
#   1. ingest-claude-md — fires on Write|Edit to keep CLAUDE.md memories current.
#   2. bg-check — fires on all tool calls (empty matcher) for background GC,
#      auto-promotion, and duplicate detection. The hook atomically elects a
#      detached worker, so heavy maintenance never blocks the tool response.
_POST_TOOL_USE_INGEST_HOOK_TEMPLATE = {
    "matcher": "Write|Edit",
    "hooks": [
        {
            "type": "command",
            "command": "MNEMOS_REPO_ROOT=\"{repo_root}\" mnemos ingest-claude-md",
        }
    ],
}

_POST_TOOL_USE_BG_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "timeout": 10,
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" '
                'bash "{bg_hook_script}" 2>/dev/null || true'
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
                'bash "{hook_script}" 2>/dev/null || true'
            ),
        }
    ],
}

# Stop hook — re-introduced with idempotency contract (Issue #4 fix).
#
# Originally removed (commit 0de1c47) because Claude Code's Stop event fires
# once per AI response turn (not at session end), which caused the hook to
# flood the session layer with a new capture on every turn.
#
# Safe re-introduction: gateway.capture() now maintains a per-instance
# in-memory dedup registry keyed by (layer, SHA-256(NFKC-normalised content)).
# Duplicate captures within the same process are silent no-ops — the write
# path (store, FTS, audit log, event-bus) is never reached.  This makes
# per-turn Stop firing harmless: the second and subsequent identical captures
# simply return None.
_STOP_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" '
                'bash "{stop_hook_script}" 2>/dev/null || true'
            ),
        }
    ],
}


# ---------------------------------------------------------------------------
# repo_root safety guard (Issue #70)
# ---------------------------------------------------------------------------

# Substring markers that indicate a temp / non-standard repo_root which must
# never be templated into a real ~/.claude/settings.json. A test that isolates
# MNEMOS_REPO_ROOT to a per-test temp dir (pytest tmp factory) would otherwise
# bake a soon-to-be-deleted path into the developer's live settings, leaving the
# mnemos hooks dangling. See tests/test_home_isolation.py.
_UNSAFE_REPO_ROOT_MARKERS = ("pytest-of-", "/T/", "/tmp/", "/private/var/folders")


def is_unsafe_repo_root(repo_root: str) -> bool:
    """Return True when *repo_root* must not be templated into settings.json.

    A repo_root is unsafe when ANY of the following holds:
    - it is empty or whitespace-only;
    - its path contains a temp / non-standard marker
      (``pytest-of-``, ``/T/``, ``/tmp/``, ``/private/var/folders``);
    - ``{repo_root}/hooks`` is not an existing directory on disk.

    Otherwise the repo_root is safe and hooks template as before.
    """
    if not repo_root or not repo_root.strip():
        return True

    if any(marker in repo_root for marker in _UNSAFE_REPO_ROOT_MARKERS):
        return True

    if not (Path(repo_root) / "hooks").is_dir():
        return True

    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_mnemos_hook_entry(entry: dict) -> bool:
    """Return True if this hook-list entry contains any mnemos command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if (
            "mnemos ingest-claude-md" in cmd
            or "mnemos search" in cmd
            or "mnemos capture" in cmd
            or "mnemos extract-insight" in cmd  # legacy — removed in Issue #4
            or "UserPromptSubmit.sh" in cmd  # active hook script (post-refactor)
            or "PostToolUse.sh" in cmd  # background ops hook (background ops)
            or "Stop.sh" in cmd  # Stop hook (re-introduced with dedup contract)
            or "mnemos bg-check" in cmd  # direct bg-check invocation
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


def _hook_script_path(repo_root: str) -> str:
    """Return the absolute path to the UserPromptSubmit hook script."""
    return str(Path(repo_root) / "hooks" / "UserPromptSubmit.sh")


def _bg_hook_script_path(repo_root: str) -> str:
    """Return the absolute path to the PostToolUse background-ops hook script."""
    return str(Path(repo_root) / "hooks" / "PostToolUse.sh")


def _stop_hook_script_path(repo_root: str) -> str:
    """Return the absolute path to the Stop hook script."""
    return str(Path(repo_root) / "hooks" / "Stop.sh")


def _render_template(template: dict, repo_root: str) -> dict:
    """Instantiate a hook template by substituting {repo_root}, {hook_script}, {bg_hook_script}, {stop_hook_script}."""
    hook_script = _hook_script_path(repo_root)
    bg_hook_script = _bg_hook_script_path(repo_root)
    stop_hook_script = _stop_hook_script_path(repo_root)
    raw = json.dumps(template)
    raw = raw.replace("{repo_root}", repo_root)
    raw = raw.replace("{hook_script}", hook_script)
    raw = raw.replace("{bg_hook_script}", bg_hook_script)
    raw = raw.replace("{stop_hook_script}", stop_hook_script)
    return json.loads(raw)


def _unified_diff(label: str, before: str, after: str) -> str:
    import difflib
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


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter
# ---------------------------------------------------------------------------

class ClaudeCodeAdapter(HostAdapter):
    """Adapter for Claude Code (Anthropic CLI).

    Owns:
    - PostToolUse hook in ~/.claude/settings.json
    - UserPromptSubmit hook in ~/.claude/settings.json
    - CLAUDE.md managed block (<!-- mnemos-start --> ... <!-- mnemos-end -->)
    """

    @property
    def name(self) -> str:
        return "Claude Code"

    def is_present(self, home: Path) -> bool:
        """Return True if ~/.claude exists or 'claude' is in PATH."""
        return (home / ".claude").exists() or shutil.which("claude") is not None

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self, home: Path) -> list[str]:
        """Install Claude Code hooks and CLAUDE.md block.

        Returns a list of human-readable status messages.
        """
        messages: list[str] = []
        repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")

        # settings.json — refuse to template an unsafe repo_root into the hooks
        # so a temp/dangling path can never be baked into a real settings.json
        # (Issue #70). The existing hook commands are left untouched.
        settings_path = home / ".claude" / "settings.json"
        if is_unsafe_repo_root(repo_root):
            messages.append(
                f"[warning] refusing to template unsafe MNEMOS_REPO_ROOT "
                f"({repo_root!r}) into {settings_path} — hooks left unchanged"
            )
        else:
            msgs = self._install_settings_json(settings_path, repo_root)
            messages.extend(msgs)

        # CLAUDE.md — unaffected by the repo_root guard.
        claude_md_path = home / ".claude" / "CLAUDE.md"
        msgs = self._install_claude_md(claude_md_path)
        messages.extend(msgs)

        return messages

    def _install_settings_json(self, settings_path: Path, repo_root: str) -> list[str]:
        """Write PostToolUse and UserPromptSubmit hooks into settings.json."""
        messages: list[str] = []

        if not settings_path.exists():
            return [f"[skipped] hooks unavailable ({settings_path})"]

        original_text = settings_path.read_text()
        try:
            data = json.loads(original_text)
        except json.JSONDecodeError:
            return [f"[warning] Could not parse {settings_path}"]

        hooks = data.setdefault("hooks", {})
        changed = False

        # PostToolUse: two canonical entries (ingest + bg-check)
        post_tool_templates = [
            _POST_TOOL_USE_INGEST_HOOK_TEMPLATE,
            _POST_TOOL_USE_BG_HOOK_TEMPLATE,
        ]
        hook_list = hooks.get("PostToolUse", [])
        non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        canonical_entries = [_render_template(t, repo_root) for t in post_tool_templates]
        new_list = non_mnemos + canonical_entries
        if new_list != hook_list:
            changed = True
        hooks["PostToolUse"] = new_list

        # UserPromptSubmit: single canonical entry
        hook_list = hooks.get("UserPromptSubmit", [])
        non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        canonical = _render_template(_USER_PROMPT_SUBMIT_HOOK_TEMPLATE, repo_root)
        new_list = non_mnemos + [canonical]
        if new_list != hook_list:
            changed = True
        hooks["UserPromptSubmit"] = new_list

        # Stop: single canonical entry (re-introduced with dedup idempotency contract)
        stop_list = hooks.get("Stop", [])
        non_mnemos_stop = [e for e in stop_list if not _is_mnemos_hook_entry(e)]
        canonical_stop = _render_template(_STOP_HOOK_TEMPLATE, repo_root)
        new_stop_list = non_mnemos_stop + [canonical_stop]
        if new_stop_list != stop_list:
            changed = True
        hooks["Stop"] = new_stop_list

        if changed:
            new_text = json.dumps(data, indent=2) + "\n"
            settings_path.write_text(new_text)
            messages.append(f"[installed] hooks in {settings_path}")
        else:
            messages.append(f"[unchanged] {settings_path}")

        return messages

    def _install_claude_md(self, claude_md_path: Path) -> list[str]:
        """Write or update the <!-- mnemos-start --> block in CLAUDE.md."""
        messages: list[str] = []

        claude_md_path.parent.mkdir(parents=True, exist_ok=True)

        original = claude_md_path.read_text() if claude_md_path.exists() else ""
        pattern = re.compile(
            r"<!-- mnemos-start -->.*?<!-- mnemos-end -->",
            re.DOTALL,
        )

        if pattern.search(original):
            updated = pattern.sub(CLAUDE_MD_BLOCK, original)
        else:
            updated = original.rstrip("\n") + "\n\n" + CLAUDE_MD_BLOCK + "\n"

        if updated != original:
            claude_md_path.write_text(updated)
            messages.append(f"[installed] managed block in {claude_md_path}")
        else:
            messages.append(f"[unchanged] {claude_md_path}")

        return messages

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    def update(self, home: Path) -> list[str]:
        """Replace managed Claude Code config blocks with canonical versions.

        Returns a list of human-readable status messages (includes diff output).
        """
        messages: list[str] = []

        # settings.json
        settings_path = home / ".claude" / "settings.json"
        changed, diff = self._update_settings_json(settings_path)
        if changed:
            messages.append(f"[updated] {settings_path}")
            if diff:
                messages.append(diff)
        else:
            messages.append(f"[unchanged] {settings_path}")

        # CLAUDE.md
        claude_md_path = home / ".claude" / "CLAUDE.md"
        changed, diff = self._update_claude_md(claude_md_path)
        if changed:
            messages.append(f"[updated] {claude_md_path}")
            if diff:
                messages.append(diff)
        else:
            messages.append(f"[unchanged] {claude_md_path}")

        return messages

    def _update_settings_json(self, settings_path: Path) -> tuple[bool, str]:
        """Replace mnemos hook entries in settings.json with canonical versions."""
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

        # PostToolUse: two canonical entries (ingest + bg-check)
        post_tool_list = hooks.get("PostToolUse", [])
        non_mnemos_pt = [e for e in post_tool_list if not _is_mnemos_hook_entry(e)]
        canonical_pt = [
            _render_template(_POST_TOOL_USE_INGEST_HOOK_TEMPLATE, repo_root),
            _render_template(_POST_TOOL_USE_BG_HOOK_TEMPLATE, repo_root),
        ]
        new_pt = non_mnemos_pt + canonical_pt
        if new_pt != post_tool_list:
            changed = True
        hooks["PostToolUse"] = new_pt

        # UserPromptSubmit: single canonical entry
        ups_list = hooks.get("UserPromptSubmit", [])
        non_mnemos_ups = [e for e in ups_list if not _is_mnemos_hook_entry(e)]
        canonical_ups = _render_template(_USER_PROMPT_SUBMIT_HOOK_TEMPLATE, repo_root)
        new_ups = non_mnemos_ups + [canonical_ups]
        if new_ups != ups_list:
            changed = True
        hooks["UserPromptSubmit"] = new_ups

        # Stop hook: replace legacy extract-insight entries and install canonical
        # Stop.sh entry (re-introduced with dedup idempotency contract).
        #
        # The legacy Stop hook used `mnemos extract-insight` which was removed
        # in Issue #4 because it flooded the session layer.  The new Stop.sh
        # hook is safe because gateway.capture() now deduplicates by
        # (layer, NFKC-normalised content hash) — per-turn re-parsing of the
        # same markers becomes a silent no-op at the storage layer.
        stop_list = hooks.get("Stop", [])
        non_mnemos_stop = [e for e in stop_list if not _is_mnemos_hook_entry(e)]
        canonical_stop = _render_template(_STOP_HOOK_TEMPLATE, repo_root)
        new_stop = non_mnemos_stop + [canonical_stop]
        if new_stop != stop_list:
            changed = True
        hooks["Stop"] = new_stop

        if not changed:
            return False, ""

        data["hooks"] = hooks
        new_text = json.dumps(data, indent=2) + "\n"
        diff = _unified_diff(str(settings_path), original_text, new_text)
        settings_path.write_text(new_text)
        return True, diff

    def _update_claude_md(self, claude_md_path: Path) -> tuple[bool, str]:
        """Replace <!-- mnemos-start --> ... <!-- mnemos-end --> block."""
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
            updated = original.rstrip("\n") + "\n\n" + CLAUDE_MD_BLOCK + "\n"

        if updated == original:
            return False, ""

        diff = _unified_diff(str(claude_md_path), original, updated)
        claude_md_path.write_text(updated)
        return True, diff

    # ------------------------------------------------------------------
    # verify_hooks
    # ------------------------------------------------------------------

    def verify_hooks(self, home: Path) -> tuple[bool, list[str]]:
        """Check that all expected Claude Code hooks and managed blocks are present.

        Checks:
        - PostToolUse hook (mnemos ingest-claude-md) in settings.json
        - PostToolUse hook (PostToolUse.sh / mnemos bg-check) in settings.json
        - UserPromptSubmit hook (UserPromptSubmit.sh) in settings.json
        - Managed block (<!-- mnemos-start --> ... <!-- mnemos-end -->) in CLAUDE.md

        Returns:
            (True, []) when all hooks are present; (False, [missing...]) otherwise.
        """
        missing: list[str] = []

        settings_path = home / ".claude" / "settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text())
                hooks = data.get("hooks", {})

                post_list = hooks.get("PostToolUse", [])
                if not any(_is_mnemos_hook_entry(e) and "ingest-claude-md" in str(e) for e in post_list):
                    missing.append("PostToolUse ingest-claude-md hook (settings.json)")

                if not any(
                    _is_mnemos_hook_entry(e)
                    and ("PostToolUse.sh" in str(e) or "bg-check" in str(e))
                    for e in post_list
                ):
                    missing.append("PostToolUse bg-check hook (settings.json)")

                user_list = hooks.get("UserPromptSubmit", [])
                if not any(_is_mnemos_hook_entry(e) for e in user_list):
                    missing.append("UserPromptSubmit hook (settings.json)")

                stop_list = hooks.get("Stop", [])
                if not any(
                    _is_mnemos_hook_entry(e) and "Stop.sh" in str(e)
                    for e in stop_list
                ):
                    missing.append("Stop hook (settings.json)")

            except (json.JSONDecodeError, OSError):
                missing.append("settings.json (unreadable)")

        claude_md_path = home / ".claude" / "CLAUDE.md"
        if claude_md_path.exists():
            content = claude_md_path.read_text()
            if "<!-- mnemos-start -->" not in content or "<!-- mnemos-end -->" not in content:
                missing.append("CLAUDE.md managed block")

        return (len(missing) == 0, missing)

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self, home: Path) -> list[str]:
        """Remove all Claude Code managed config sections.

        Returns a list of human-readable status messages.
        """
        messages: list[str] = []

        # settings.json
        settings_path = home / ".claude" / "settings.json"
        changed, _ = self._remove_settings_json_hooks(settings_path)
        messages.append(
            f"[{'removed' if changed else 'unchanged'}] {settings_path}"
        )

        # CLAUDE.md
        claude_md_path = home / ".claude" / "CLAUDE.md"
        changed, _ = self._remove_claude_md_block(claude_md_path)
        messages.append(
            f"[{'removed' if changed else 'unchanged'}] {claude_md_path}"
        )

        return messages

    def _remove_settings_json_hooks(self, settings_path: Path) -> tuple[bool, str]:
        """Remove all mnemos hook entries from settings.json."""
        if not settings_path.exists():
            return False, ""

        original_text = settings_path.read_text()
        try:
            data = json.loads(original_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Cannot parse {settings_path}: {exc}") from exc

        hooks = data.get("hooks", {})
        changed = False

        def _is_any_mnemos(entry: dict) -> bool:
            for h in entry.get("hooks", []):
                if "mnemos" in h.get("command", ""):
                    return True
            return False

        for hook_type in ("PostToolUse", "UserPromptSubmit", "Stop"):
            hook_list = hooks.get(hook_type, [])
            cleaned = [e for e in hook_list if not _is_any_mnemos(e)]
            if cleaned != hook_list:
                changed = True
                if cleaned:
                    hooks[hook_type] = cleaned
                else:
                    del hooks[hook_type]

        if not changed:
            return False, ""

        if hooks:
            data["hooks"] = hooks
        elif "hooks" in data:
            del data["hooks"]

        new_text = json.dumps(data, indent=2) + "\n"
        diff = _unified_diff(str(settings_path), original_text, new_text)
        settings_path.write_text(new_text)
        return True, diff

    def _remove_claude_md_block(self, claude_md_path: Path) -> tuple[bool, str]:
        """Remove the <!-- mnemos-start --> ... <!-- mnemos-end --> block."""
        if not claude_md_path.exists():
            return False, ""

        original = claude_md_path.read_text()
        pattern = re.compile(
            r"\n?<!-- mnemos-start -->.*?<!-- mnemos-end -->\n?",
            re.DOTALL,
        )

        updated = pattern.sub("", original)
        updated = re.sub(r"\n{3,}", "\n\n", updated)

        if updated == original:
            return False, ""

        diff = _unified_diff(str(claude_md_path), original, updated)
        claude_md_path.write_text(updated)
        return True, diff

    # ------------------------------------------------------------------
    # EventBus integration
    # ------------------------------------------------------------------

    # Layers that warrant a visible notification on direct capture.
    # ephemeral and working are intentionally excluded (silent by design).
    _NOTIFY_CAPTURE_LAYERS = frozenset({"session", "project", "global"})

    def subscribe_to_event_bus(self, bus: "EventBus") -> None:
        """Register in-process handlers on *bus* for promotion notifications.

        Subscribes to:
        - ``post-promote`` — prints a styled promotion notice to stdout.
        - ``post-capture`` — prints a styled capture notice for persistent layers
          (session, project, global) to stdout.
        """
        bus.subscribe("post-promote", self._on_post_promote)
        bus.subscribe("post-capture", self._on_post_capture)

    def _on_post_promote(self, payload: dict) -> None:
        """Handle a ``post-promote`` event by printing a promotion notice.

        Output format (plain)::

            ✻ 🧠 promoted <item_id> → <to_layer>

        ANSI styling uses the destination layer's colour.  The notice is
        suppressed when ``NO_COLOR`` is set in the environment (same
        behaviour as :func:`~core.output.capture_notice`).
        """
        from core.output import promote_notice

        item_id = payload.get("item_id", "?")
        to_layer = payload.get("to_layer", "?")
        no_color = "NO_COLOR" in os.environ
        print(promote_notice(item_id=item_id, target_layer=to_layer, no_color=no_color))

    def _on_post_capture(self, payload: dict) -> None:
        """Handle a ``post-capture`` event by printing a notice for persistent layers.

        Only session, project, and global layer captures produce output.
        ephemeral and working captures are intentionally silent.

        Output format::

            ✻ 🧠 <content_preview> (<layer>)
        """
        from core.output import capture_notice

        layer = payload.get("layer", "")
        if layer not in self._NOTIFY_CAPTURE_LAYERS:
            return
        content_preview = payload.get("content_preview", "")
        item_id = payload.get("item_id", "")
        no_color = "NO_COLOR" in os.environ
        print(capture_notice(content_preview, layer, item_id=item_id, no_color=no_color))
