"""Tests that verify TDD is mandatorily enforced in backend and frontend agent prompts.

TDD is non-negotiable: agents that do not write tests before implementation code
produce unreliable output. These tests guard against regressions where TDD
instructions are accidentally removed or weakened.

The tests read the installed agent prompt files from the agent-crew system
directory (~/.agent-crew/system/agents/) which are the files actually used
during pipeline execution.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AGENT_CREW_HOME = Path(os.environ.get("AGENT_CREW_HOME", Path.home() / ".agent-crew"))
SYSTEM_AGENTS_DIR = AGENT_CREW_HOME / "system" / "agents"


def _read_agent(name: str) -> str:
    """Return the full text of an installed agent prompt file."""
    path = SYSTEM_AGENTS_DIR / f"{name}.md"
    if not path.exists():
        pytest.skip(f"Agent file not found: {path} — agent-crew may not be installed")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend agent — TDD enforcement
# ---------------------------------------------------------------------------


class TestBackendAgentTDDEnforcement:
    """The backend agent prompt must contain explicit, mandatory TDD instructions."""

    def test_backend_agent_file_exists(self) -> None:
        path = SYSTEM_AGENTS_DIR / "backend.md"
        assert path.exists(), (
            f"backend.md not found at {path}. "
            "Install agent-crew or run `crew:update` to sync system agents."
        )

    def test_backend_prompt_contains_tdd_section(self) -> None:
        content = _read_agent("backend")
        assert "TDD" in content, (
            "backend.md must contain a TDD section. "
            "TDD is mandatory — agents must write tests before implementation code."
        )

    def test_backend_prompt_mandates_test_before_implementation(self) -> None:
        content = _read_agent("backend")
        # Must contain a phrase that clearly mandates test-first order
        mandatory_phrases = [
            "MANDATORY",
            "MUST NOT be written until a failing test",
            "Write the failing test FIRST",
        ]
        found = any(phrase in content for phrase in mandatory_phrases)
        assert found, (
            "backend.md must contain an explicit mandate to write the failing test "
            "BEFORE implementation code. "
            f"None of these phrases were found: {mandatory_phrases}"
        )

    def test_backend_prompt_contains_red_green_refactor_cycle(self) -> None:
        content = _read_agent("backend")
        assert "RED" in content and "GREEN" in content and "REFACTOR" in content, (
            "backend.md must document the RED → GREEN → REFACTOR TDD cycle. "
            "Agents need explicit step-by-step TDD instructions to execute it correctly."
        )

    def test_backend_prompt_requires_no_commit_without_tests(self) -> None:
        content = _read_agent("backend")
        # Check for a rule that forbids implementation-only commits
        forbidden_commit_phrases = [
            "No commit without test",
            "commit without tests",
            "implementation-only commits are forbidden",
        ]
        found = any(phrase.lower() in content.lower() for phrase in forbidden_commit_phrases)
        assert found, (
            "backend.md Absolute Rules must include a rule forbidding commits "
            "without test files. "
            f"None of these phrases were found: {forbidden_commit_phrases}"
        )

    def test_backend_prompt_specifies_test_file_naming(self) -> None:
        content = _read_agent("backend")
        # Should specify test file naming convention for Kotlin/Spring Boot
        assert "Test.kt" in content or "test file" in content.lower(), (
            "backend.md should specify a test file naming convention "
            "(e.g., {ClassName}Test.kt) so agents know where to put tests."
        )

    def test_backend_prompt_requires_tdd_log(self) -> None:
        content = _read_agent("backend")
        assert "tdd_log" in content, (
            "backend.md must require agents to maintain tdd_log.md "
            "so TDD cycle execution is observable and auditable."
        )

    def test_backend_absolute_rules_mention_tests(self) -> None:
        content = _read_agent("backend")
        # Find the Absolute Rules section and verify it mentions tests
        rules_start = content.find("## Absolute Rules")
        assert rules_start != -1, "backend.md must have an '## Absolute Rules' section"
        rules_section = content[rules_start:]
        assert "test" in rules_section.lower(), (
            "backend.md Absolute Rules section must mention tests. "
            "Rules without a test mandate can be silently ignored."
        )


# ---------------------------------------------------------------------------
# Frontend agent — TDD enforcement
# ---------------------------------------------------------------------------


class TestFrontendAgentTDDEnforcement:
    """The frontend agent prompt must contain explicit, mandatory TDD instructions.

    The frontend agent previously had NO TDD instructions at all — tests guard
    against regression to that state.
    """

    def test_frontend_agent_file_exists(self) -> None:
        path = SYSTEM_AGENTS_DIR / "frontend.md"
        assert path.exists(), (
            f"frontend.md not found at {path}. "
            "Install agent-crew or run `crew:update` to sync system agents."
        )

    def test_frontend_prompt_contains_tdd_section(self) -> None:
        content = _read_agent("frontend")
        assert "TDD" in content, (
            "frontend.md must contain a TDD section. "
            "Previously, the frontend agent had NO test instructions — "
            "this is the regression guard."
        )

    def test_frontend_prompt_mandates_test_before_implementation(self) -> None:
        content = _read_agent("frontend")
        mandatory_phrases = [
            "MANDATORY",
            "MUST NOT be written until a failing test",
            "Write the failing test FIRST",
        ]
        found = any(phrase in content for phrase in mandatory_phrases)
        assert found, (
            "frontend.md must contain an explicit mandate to write the failing test "
            "BEFORE component code. "
            f"None of these phrases were found: {mandatory_phrases}"
        )

    def test_frontend_prompt_contains_red_green_refactor_cycle(self) -> None:
        content = _read_agent("frontend")
        assert "RED" in content and "GREEN" in content and "REFACTOR" in content, (
            "frontend.md must document the RED → GREEN → REFACTOR TDD cycle. "
            "This was completely absent before — the regression guard."
        )

    def test_frontend_prompt_specifies_test_framework(self) -> None:
        content = _read_agent("frontend")
        # Should mention a test framework (Vitest, Jest, or Testing Library)
        test_frameworks = ["Vitest", "Jest", "Testing Library", "vitest", "jest"]
        found = any(fw in content for fw in test_frameworks)
        assert found, (
            "frontend.md must specify a test framework so agents know which tool to use. "
            f"Expected one of: {test_frameworks}"
        )

    def test_frontend_prompt_specifies_test_file_naming(self) -> None:
        content = _read_agent("frontend")
        assert ".test.tsx" in content or ".spec.tsx" in content or "test file" in content.lower(), (
            "frontend.md should specify a test file naming convention "
            "(e.g., {ComponentName}.test.tsx) so agents know where to put tests."
        )

    def test_frontend_prompt_requires_no_commit_without_tests(self) -> None:
        content = _read_agent("frontend")
        forbidden_commit_phrases = [
            "No commit without test",
            "commit without tests",
            "implementation-only commits are forbidden",
        ]
        found = any(phrase.lower() in content.lower() for phrase in forbidden_commit_phrases)
        assert found, (
            "frontend.md Absolute Rules must include a rule forbidding commits "
            "without test files. "
            f"None of these phrases were found: {forbidden_commit_phrases}"
        )

    def test_frontend_prompt_requires_tdd_log(self) -> None:
        content = _read_agent("frontend")
        assert "tdd_log" in content, (
            "frontend.md must require agents to maintain tdd_log.md. "
            "Without it, TDD cycle execution cannot be audited."
        )

    def test_frontend_absolute_rules_mention_tests(self) -> None:
        content = _read_agent("frontend")
        rules_start = content.find("## Absolute Rules")
        assert rules_start != -1, "frontend.md must have an '## Absolute Rules' section"
        rules_section = content[rules_start:]
        assert "test" in rules_section.lower(), (
            "frontend.md Absolute Rules section must mention tests. "
            "Previously this section had NO test-related rules — regression guard."
        )

    def test_frontend_description_mentions_tests(self) -> None:
        content = _read_agent("frontend")
        # The YAML frontmatter description is used for agent routing — it should
        # signal that the agent produces test code
        header_end = content.find("---", 3)  # find closing --- of frontmatter
        frontmatter = content[:header_end] if header_end != -1 else content[:500]
        assert "test" in frontmatter.lower(), (
            "frontend.md YAML description must mention test code output. "
            "Agent routing decisions rely on the description — if tests are not "
            "mentioned, the planner may not know the agent produces them."
        )


# ---------------------------------------------------------------------------
# Cross-agent consistency
# ---------------------------------------------------------------------------


class TestAgentTDDConsistency:
    """Both agents must enforce TDD with consistent strength."""

    def test_both_agents_have_mandatory_language(self) -> None:
        backend = _read_agent("backend")
        frontend = _read_agent("frontend")
        assert "MANDATORY" in backend, "backend.md must use MANDATORY language for TDD"
        assert "MANDATORY" in frontend, "frontend.md must use MANDATORY language for TDD"

    def test_both_agents_block_on_missing_test_framework(self) -> None:
        backend = _read_agent("backend")
        frontend = _read_agent("frontend")
        block_phrase = "BLOCKED"
        assert block_phrase in backend, (
            "backend.md must instruct the agent to report BLOCKED "
            "when no test framework is available."
        )
        assert block_phrase in frontend, (
            "frontend.md must instruct the agent to report BLOCKED "
            "when no test framework is available."
        )

    def test_both_agents_require_tests_in_commit(self) -> None:
        backend = _read_agent("backend")
        frontend = _read_agent("frontend")
        commit_phrase = "MUST include"
        assert commit_phrase in backend, (
            "backend.md must state that commits MUST include test files."
        )
        assert commit_phrase in frontend, (
            "frontend.md must state that commits MUST include test files."
        )


# ---------------------------------------------------------------------------
# Skill path reachability
# ---------------------------------------------------------------------------


class TestBackendSkillPaths:
    """All skill file paths referenced in backend.md must resolve to existing files.

    Broken skill paths cause silent failures — the agent reads nothing,
    so skill-specific guidance (e.g. TDD cycle details) is skipped entirely.
    """

    def test_backend_tdd_skill_path_is_reachable(self) -> None:
        content = _read_agent("backend")
        # Extract absolute skill paths (starting with ~/.agent-crew) from the Skills section.
        # Relative paths like core/agents/skills/api-design.md are project-specific
        # and intentionally not checked here.
        import re
        abs_skill_paths = re.findall(r"`(~/.agent-crew[^`]+\.md)`", content)
        assert abs_skill_paths, (
            "backend.md must reference at least one absolute skill file path. "
            "Expected patterns like `~/.agent-crew/system/agents/skills/tdd.md`\n"
            "The old (broken) path `~/.agent-crew/agents/skills/tdd.md` no longer exists — "
            "the correct path is `~/.agent-crew/system/agents/skills/tdd.md`"
        )
        for raw_path in abs_skill_paths:
            # Expand ~ to the real home directory
            expanded = Path(raw_path.replace("~", str(Path.home())))
            assert expanded.exists(), (
                f"Skill file referenced in backend.md does not exist: {raw_path}\n"
                f"Expanded to: {expanded}\n"
                "Fix the path in backend.md to point to the actual location "
                "under ~/.agent-crew/system/agents/skills/"
            )
