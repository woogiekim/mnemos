"""ClaudeCodeAdapter — manages mnemos integration with Claude Code."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from core.adapters.base import HostAdapter


# ---------------------------------------------------------------------------
# Canonical managed block content (mirrors install.sh)
# ---------------------------------------------------------------------------

CLAUDE_MD_BLOCK = """\
<!-- mnemos-start -->
## Memory (mnemos)
When asked about past context, notes, or decisions, run:
`mnemos search <query>`
This searches your personal memory store managed by mnemos.

### Capturing knowledge
Use `mnemos capture "<insight>"` when you identify:
- Stable project decisions
- Architecture constraints
- User preferences
- Reusable workflows
- Important operational knowledge
- Persistent project context

Do NOT capture:
- Temporary debugging, speculative reasoning, incomplete thoughts
- Transient conversation, low-confidence assumptions, scratch work

All captured insights begin in the session layer.
Promotion to project or global layer happens through mnemos promotion rules only.
<!-- mnemos-end -->"""

# Hook definitions that install.sh injects.
_POST_TOOL_USE_HOOK_TEMPLATE = {
    "matcher": "Write|Edit",
    "hooks": [
        {
            "type": "command",
            "command": "MNEMOS_REPO_ROOT=\"{repo_root}\" mnemos ingest-claude-md",
        }
    ],
}

_USER_PROMPT_SUBMIT_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" mnemos search '
                '"${CLAUDE_PROMPT:0:200}" 2>/dev/null | head -30 || true'
            ),
        }
    ],
}

_STOP_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" mnemos capture '
                '--layer session --content "session-end" --tag auto-stop '
                "2>/dev/null || true"
            ),
        }
    ],
}


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
    - Stop hook in ~/.claude/settings.json (captures session-end marker)
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

        # settings.json
        settings_path = home / ".claude" / "settings.json"
        msgs = self._install_settings_json(settings_path, repo_root)
        messages.extend(msgs)

        # CLAUDE.md
        claude_md_path = home / ".claude" / "CLAUDE.md"
        msgs = self._install_claude_md(claude_md_path)
        messages.extend(msgs)

        return messages

    def _install_settings_json(self, settings_path: Path, repo_root: str) -> list[str]:
        """Write PostToolUse and UserPromptSubmit hooks into settings.json."""
        messages: list[str] = []

        if not settings_path.exists():
            return messages

        original_text = settings_path.read_text()
        try:
            data = json.loads(original_text)
        except json.JSONDecodeError:
            return [f"[warning] Could not parse {settings_path}"]

        hooks = data.setdefault("hooks", {})
        changed = False

        for hook_type, template in [
            ("PostToolUse", _POST_TOOL_USE_HOOK_TEMPLATE),
            ("UserPromptSubmit", _USER_PROMPT_SUBMIT_HOOK_TEMPLATE),
            ("Stop", _STOP_HOOK_TEMPLATE),
        ]:
            hook_list = hooks.get(hook_type, [])
            non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
            canonical = json.loads(json.dumps(template).replace("{repo_root}", repo_root))
            new_list = non_mnemos + [canonical]
            if new_list != hook_list:
                changed = True
            hooks[hook_type] = new_list

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

        if not claude_md_path.exists():
            return messages

        original = claude_md_path.read_text()
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
        for hook_type in ("PostToolUse", "UserPromptSubmit", "Stop"):
            for entry in hooks.get(hook_type, []):
                if _is_mnemos_hook_entry(entry):
                    found = _extract_repo_root_from_hook(entry)
                    if found:
                        repo_root = found
                        break
            if repo_root:
                break

        # Remove all existing mnemos hook entries and replace with canonical ones
        # (This also replaces any legacy Stop hook from Issue #4 with the new canonical version)
        for hook_type, template in [
            ("PostToolUse", _POST_TOOL_USE_HOOK_TEMPLATE),
            ("UserPromptSubmit", _USER_PROMPT_SUBMIT_HOOK_TEMPLATE),
            ("Stop", _STOP_HOOK_TEMPLATE),
        ]:
            hook_list = hooks.get(hook_type, [])
            non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
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
        - UserPromptSubmit hook (mnemos search) in settings.json
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
                    missing.append("PostToolUse hook (settings.json)")

                user_list = hooks.get("UserPromptSubmit", [])
                if not any(_is_mnemos_hook_entry(e) and "mnemos search" in str(e) for e in user_list):
                    missing.append("UserPromptSubmit hook (settings.json)")

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
