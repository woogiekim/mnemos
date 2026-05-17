"""Tests for Click CLI commands."""
import pytest
import yaml
import json
from pathlib import Path
from click.testing import CliRunner


@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal repo structure and return path."""
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
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")

    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    """Return CLI runner and the cli function, with MNEMOS_REPO_ROOT set."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


class TestMemoryCaptureCommand:
    def test_memory_capture_command(self, runner, cli_with_repo, repo_root):
        """capture must store an item and print its ID."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "CLI test content"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower() or len(result.output.strip()) > 0

        # Verify file was created
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_memory_capture_with_id(self, runner, cli_with_repo, repo_root):
        """capture with --id must use the provided ID."""
        result = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "global",
                "--content", "Item with custom ID",
                "--id", "custom-cli-001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "custom-cli-001" in result.output

    def test_memory_capture_invalid_layer_exits_nonzero(self, runner, cli_with_repo):
        """capture with invalid layer must exit with error."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "invalid", "--content", "x"],
        )
        assert result.exit_code != 0 or "error" in result.output.lower()

    def test_memory_capture_notification_short_content(self, runner, cli_with_repo):
        """capture notification must not add '...' when content <= 60 chars.

        New emoji-based format (no (layer) suffix):  ✻ 🧠 <brief>
        """
        content = "Short insight"
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert content in result.output
        assert "..." not in result.output
        # New format omits the (layer) suffix — the emoji identifies the layer
        assert "(global)" not in result.output

    def test_memory_capture_notification_long_content(self, runner, cli_with_repo):
        """capture notification must truncate content > 60 chars with '...'.

        New emoji-based format (no (layer) suffix):  ✻ 🧠 <brief>...
        """
        content = "A" * 80  # 80 chars, more than 60
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        preview = content[:60]
        assert f"{preview}..." in result.output
        # New format omits the (layer) suffix — the emoji identifies the layer
        assert "(global)" not in result.output

    def test_memory_capture_notification_exactly_60_chars(self, runner, cli_with_repo):
        """capture notification must not add '...' when content is exactly 60 chars."""
        content = "B" * 60  # exactly 60 chars
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert content in result.output
        assert "..." not in result.output


class TestMemoryListCommand:
    def test_list_empty_store(self, runner, cli_with_repo):
        """list on empty store must exit 0 and report 0 memories."""
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        assert "[mnemos] 0 memories" in result.output

    def test_list_shows_captured_items(self, runner, cli_with_repo):
        """list must show items that have been captured."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "List test item", "--id", "list-001"],
        )
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        assert "list-001" in result.output
        assert "[mnemos] 1 memories" in result.output

    def test_list_layer_filter(self, runner, cli_with_repo):
        """--layer must restrict output to the specified layer."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Global item", "--id", "list-global"],
        )
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "project", "--content", "Project item", "--id", "list-project"],
        )
        result = runner.invoke(cli_with_repo, ["list", "--layer", "global"])
        assert result.exit_code == 0, result.output
        assert "list-global" in result.output
        assert "list-project" not in result.output

    def test_list_limit(self, runner, cli_with_repo):
        """--limit must cap the number of items returned."""
        for i in range(5):
            runner.invoke(
                cli_with_repo,
                ["capture", "--layer", "global", "--content", f"Item {i}", "--id", f"limit-{i:03d}"],
            )
        result = runner.invoke(cli_with_repo, ["list", "--limit", "2"])
        assert result.exit_code == 0, result.output
        assert "[mnemos] 2 memories" in result.output

    def test_list_output_format(self, runner, cli_with_repo):
        """list output lines must follow '  [layer] item_id: content' format."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Format check content", "--id", "fmt-001"],
        )
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.startswith("  [")]
        assert len(lines) >= 1
        assert "[global]" in lines[0]
        assert "fmt-001" in lines[0]


class TestMemorySearchCommand:
    def test_memory_search_command(self, runner, cli_with_repo, repo_root):
        """search must return results for indexed content."""
        # First capture something
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "searchable content about pandas"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "pandas"],
        )
        assert result.exit_code == 0, result.output

    def test_memory_search_empty_returns_zero(self, runner, cli_with_repo):
        """search on empty store must exit 0 with empty output."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "nonexistent-xyzzy"],
        )
        assert result.exit_code == 0

    def test_memory_search_notification_with_results(self, runner, cli_with_repo):
        """search must print '[mnemos] Retrieved N memories' after results."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "notification test about dolphins"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "dolphins"],
        )
        assert result.exit_code == 0, result.output
        assert "[mnemos] Retrieved" in result.output
        assert "memories" in result.output
        # Notification must appear at the end
        lines = result.output.strip().splitlines()
        assert lines[-1].startswith("[mnemos] Retrieved")

    def test_memory_search_notification_no_results(self, runner, cli_with_repo):
        """search must print '[mnemos] Retrieved 0 memories' when no results."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "zzz-no-match-xyzzy-999"],
        )
        assert result.exit_code == 0, result.output
        assert "[mnemos] Retrieved 0 memories" in result.output

    def test_memory_search_notification_count_matches(self, runner, cli_with_repo):
        """search notification N must match the number of results returned."""
        content = "unique-kiwi-memory-for-count-test"
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "kiwi-memory-for-count"],
        )
        assert result.exit_code == 0, result.output
        # Extract N from "[mnemos] Retrieved N memories"
        import re
        match = re.search(r"\[mnemos\] Retrieved (\d+) memories", result.output)
        assert match is not None, f"Notification not found in: {result.output}"
        n = int(match.group(1))
        # Count result lines (lines starting with "  [")
        result_lines = [l for l in result.output.splitlines() if l.startswith("  [")]
        assert n == len(result_lines), (
            f"Notification said {n} but found {len(result_lines)} result lines"
        )


class TestListTruncation:
    """Tests for list command truncation behaviour (--full / --width)."""

    LONG_CONTENT = "A" * 100  # 100 chars, exceeds default width of 80

    def test_list_default_truncates_at_80_with_ellipsis(self, runner, cli_with_repo):
        """list default output must truncate content at 80 chars and append '...'."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-list-001"],
        )
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if "trunc-list-001" in l]
        assert len(lines) == 1, f"Expected one result line, got: {result.output}"
        line = lines[0]
        # Should end with '...' and content portion should be exactly 80 chars
        assert line.endswith("..."), f"Expected '...' suffix, got: {line!r}"
        # The 80-char truncated portion must appear in the line
        assert self.LONG_CONTENT[:80] in line

    def test_list_default_no_ellipsis_when_content_fits(self, runner, cli_with_repo):
        """list must NOT append '...' when content length == width (boundary)."""
        exact_content = "B" * 80  # exactly 80 chars
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", exact_content, "--id", "trunc-list-exact"],
        )
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if "trunc-list-exact" in l]
        assert len(lines) == 1
        assert not lines[0].endswith("..."), f"Should not end with '...' for exact-width content: {lines[0]!r}"

    def test_list_full_shows_complete_content(self, runner, cli_with_repo):
        """--full must show complete content without any truncation."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-list-full"],
        )
        result = runner.invoke(cli_with_repo, ["list", "--full"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if "trunc-list-full" in l]
        assert len(lines) == 1
        assert self.LONG_CONTENT in lines[0], "Full content must appear untruncated"
        assert not lines[0].endswith("..."), "--full must not add ellipsis"

    def test_list_width_option_widens_preview(self, runner, cli_with_repo):
        """--width 200 must truncate at 200 chars, not 80."""
        content_200 = "C" * 150  # 150 chars, fits in 200, exceeds 80
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content_200, "--id", "trunc-list-width"],
        )
        # Default: should truncate at 80
        result_default = runner.invoke(cli_with_repo, ["list"])
        lines_default = [l for l in result_default.output.splitlines() if "trunc-list-width" in l]
        assert lines_default[0].endswith("..."), "Default must truncate 150-char content at 80"

        # --width 200: 150 chars fits in 200, no ellipsis
        result_wide = runner.invoke(cli_with_repo, ["list", "--width", "200"])
        assert result_wide.exit_code == 0, result_wide.output
        lines_wide = [l for l in result_wide.output.splitlines() if "trunc-list-width" in l]
        assert len(lines_wide) == 1
        assert content_200 in lines_wide[0], "--width 200 must show full content for 150-char item"
        assert not lines_wide[0].endswith("..."), "--width 200 must not add ellipsis for 150-char item"

    def test_list_width_option_truncates_beyond_width(self, runner, cli_with_repo):
        """--width 40 must truncate at 40 chars with '...'."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-list-w40"],
        )
        result = runner.invoke(cli_with_repo, ["list", "--width", "40"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if "trunc-list-w40" in l]
        assert len(lines) == 1
        assert lines[0].endswith("..."), "--width 40 must truncate 100-char content and add '...'"
        assert self.LONG_CONTENT[:40] in lines[0]


class TestSearchTruncation:
    """Tests for search command truncation behaviour (--full / --width).

    Note: search result ``content`` is the full YAML file text (frontmatter +
    body), which is always longer than 80 chars and contains newlines.  The
    truncation tests therefore verify the *presence* of the ``...`` marker in
    the overall output block and the *absence* of it under ``--full``, rather
    than asserting which particular output line ends with ``...``.
    """

    LONG_CONTENT = "unique-search-trunc-marker " + "X" * 80  # > 80 chars, searchable

    def test_search_default_truncates_with_ellipsis(self, runner, cli_with_repo):
        """search default output must truncate the YAML file text and append '...'."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-srch-001"],
        )
        result = runner.invoke(cli_with_repo, ["search", "unique-search-trunc-marker"])
        assert result.exit_code == 0, result.output
        # The truncated preview must end with '...' somewhere in the output block
        # before the '[mnemos]' footer.
        body = result.output.split("[mnemos]")[0]
        assert "..." in body, (
            f"Expected '...' in output body (content > 80 chars should be truncated): {result.output!r}"
        )

    def test_search_full_shows_complete_content(self, runner, cli_with_repo):
        """--full must show the complete YAML file text without any '...' truncation."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-srch-full"],
        )
        result = runner.invoke(cli_with_repo, ["search", "--full", "unique-search-trunc-marker"])
        assert result.exit_code == 0, result.output
        # The memory content must appear untruncated somewhere in the output
        assert self.LONG_CONTENT in result.output, (
            "--full must include complete original content in output"
        )
        # No '...' truncation marker should appear in the body
        body = result.output.split("[mnemos]")[0]
        assert "..." not in body, f"--full must not add ellipsis: {result.output!r}"

    def test_search_width_widens_truncation_point(self, runner, cli_with_repo):
        """--width 500 must show more content than the default 80-char truncation."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-srch-width"],
        )
        # Default: shorter preview
        result_default = runner.invoke(cli_with_repo, ["search", "unique-search-trunc-marker"])
        body_default = result_default.output.split("[mnemos]")[0]

        # --width 500: the YAML file text is ~300 chars so it fits; no ellipsis
        result_wide = runner.invoke(cli_with_repo, ["search", "--width", "500", "unique-search-trunc-marker"])
        assert result_wide.exit_code == 0, result_wide.output
        body_wide = result_wide.output.split("[mnemos]")[0]

        # The wider preview must show more content (longer body)
        assert len(body_wide) >= len(body_default), (
            "--width 500 body should be at least as long as default body"
        )
        # With width 500 the full YAML text fits → no ellipsis
        assert "..." not in body_wide, (
            f"--width 500 should not truncate a ~300-char file: {result_wide.output!r}"
        )

    def test_search_narrow_width_truncates_with_ellipsis(self, runner, cli_with_repo):
        """--width 40 must truncate the file text at 40 chars and add '...'."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.LONG_CONTENT, "--id", "trunc-srch-w40"],
        )
        result = runner.invoke(cli_with_repo, ["search", "--width", "40", "unique-search-trunc-marker"])
        assert result.exit_code == 0, result.output
        body = result.output.split("[mnemos]")[0]
        assert "..." in body, (
            f"--width 40 must truncate long file text and add '...': {result.output!r}"
        )


class TestIssue29ListSearchEllipsis:
    """Regression tests for issue #29: list/search must append '...' on truncation.

    The original bug: ``mnemos list`` and ``mnemos search`` truncated content at
    ~80 chars without any visual indicator, so users could not tell whether an
    entry ended there or was cut off.  The fix (via ``_truncate_content``) ensures
    ``...`` is always appended when the content exceeds the display width.
    """

    # Mirrors the exact reproduction case from issue #29 (195-char string)
    ROUTING_CONTENT = (
        "Routing: questions now route through crew:agent (historian for session-state Q, "
        "analyst for codebase Q); CLAUDE.md carve-out narrowed; see core/rules/agent-routing.md"
    )

    def test_list_appends_ellipsis_for_195_char_content(self, runner, cli_with_repo):
        """list must show '...' for the 195-char content from issue #29 reproduction."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.ROUTING_CONTENT, "--id", "issue29-list"],
        )
        result = runner.invoke(cli_with_repo, ["list"])
        assert result.exit_code == 0, result.output
        lines = [ln for ln in result.output.splitlines() if "issue29-list" in ln]
        assert len(lines) == 1, f"Expected one result line, got: {result.output}"
        assert lines[0].endswith("..."), (
            f"195-char content must be truncated with '...': {lines[0]!r}"
        )

    def test_search_appends_ellipsis_for_195_char_content(self, runner, cli_with_repo):
        """search must show '...' for the 195-char content from issue #29 reproduction."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.ROUTING_CONTENT, "--id", "issue29-search"],
        )
        result = runner.invoke(cli_with_repo, ["search", "crew:agent"])
        assert result.exit_code == 0, result.output
        body = result.output.split("[mnemos]")[0]
        assert "..." in body, (
            f"195-char content must show '...' in search output: {result.output!r}"
        )

    def test_list_full_flag_shows_complete_195_char_content(self, runner, cli_with_repo):
        """list --full must show the entire 195-char string without truncation."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", self.ROUTING_CONTENT, "--id", "issue29-full"],
        )
        result = runner.invoke(cli_with_repo, ["list", "--full"])
        assert result.exit_code == 0, result.output
        assert self.ROUTING_CONTENT in result.output, (
            "list --full must show the complete 195-char content"
        )


class TestMemoryPromoteCommand:
    def test_memory_promote_command(self, runner, cli_with_repo, repo_root):
        """promote must move item to next layer."""
        # Capture in project layer
        cap_result = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "project",
                "--content", "Item to promote",
                "--id", "promote-001",
            ],
        )
        assert cap_result.exit_code == 0, cap_result.output

        # Promote it
        result = runner.invoke(
            cli_with_repo,
            ["promote", "promote-001"],
        )
        assert result.exit_code == 0, result.output


class TestMemoryForgetCommand:
    def test_memory_forget_requires_archived(self, runner, cli_with_repo, repo_root):
        """forget on non-archived item must fail with error."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "forget-me", "--id", "forget-001"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["forget", "--force", "forget-001"],
        )
        # Should fail because item is not archived
        assert result.exit_code != 0 or "error" in result.output.lower() or "policy" in result.output.lower()

    def test_memory_forget_after_archive_succeeds(self, runner, cli_with_repo, repo_root):
        """forget after archive must succeed."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "archive then forget", "--id", "forget-002"],
        )
        runner.invoke(
            cli_with_repo,
            ["archive", "forget-002"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["forget", "--force", "forget-002"],
        )
        assert result.exit_code == 0, result.output


class TestMemoryDeleteCommand:
    """Tests for `mnemos delete` CLI command (issue #33)."""

    def test_delete_removes_item(self, runner, cli_with_repo, repo_root):
        """delete must remove the item from the store."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "delete me", "--id", "del-001"],
        )
        result = runner.invoke(cli_with_repo, ["delete", "del-001"])
        assert result.exit_code == 0, result.output
        assert "deleted: del-001" in result.output

        # Item must no longer exist
        matches = list((repo_root / "wiki" / "global").glob("del-001.md"))
        assert len(matches) == 0

    def test_delete_no_archive_required(self, runner, cli_with_repo, repo_root):
        """delete must succeed on a non-archived item (unlike 'forget')."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "transient capture", "--id", "del-002"],
        )
        # Item is in 'stored' stage — forget would fail, delete must succeed
        result = runner.invoke(cli_with_repo, ["delete", "del-002"])
        assert result.exit_code == 0, result.output
        assert "deleted: del-002" in result.output

    def test_delete_nonexistent_item_exits_nonzero(self, runner, cli_with_repo):
        """delete on a missing item must exit with a non-zero code."""
        result = runner.invoke(cli_with_repo, ["delete", "nonexistent-xyz-999"])
        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_delete_item_not_in_list_after_deletion(self, runner, cli_with_repo):
        """item must not appear in 'list' output after deletion."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "gone after delete", "--id", "del-003"],
        )
        runner.invoke(cli_with_repo, ["delete", "del-003"])
        result = runner.invoke(cli_with_repo, ["list"])
        assert "del-003" not in result.output

    def test_delete_ephemeral_item(self, runner, cli_with_repo, repo_root):
        """delete must work on ephemeral-layer items."""
        capture_result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "ephemeral", "--content", "ephemeral transient",
             "--id", "del-eph-001", "--run-id", "test-run"],
        )
        assert capture_result.exit_code == 0, capture_result.output
        result = runner.invoke(cli_with_repo, ["delete", "del-eph-001"])
        assert result.exit_code == 0, result.output
        assert "deleted: del-eph-001" in result.output


class TestConsolidateCommand:
    """Tests for `mnemos consolidate` CLI command (Part 2 of issue #11)."""

    def test_consolidate_exits_zero(self, runner, cli_with_repo):
        """consolidate must exit 0 even on an empty store."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output

    def test_consolidate_output_format(self, runner, cli_with_repo):
        """consolidate must print 'Promoted N memories'."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Promoted" in result.output
        assert "memories" in result.output

    def test_consolidate_promotes_eligible_items(self, runner, cli_with_repo, repo_root):
        """consolidate must promote items that meet policy thresholds and report count."""
        import re
        # Capture items in project layer (zero thresholds in test policy → all eligible)
        for i in range(2):
            runner.invoke(
                cli_with_repo,
                [
                    "capture",
                    "--layer", "project",
                    "--content", f"Consolidate CLI test item {i}",
                    "--quality-score", "0.9",
                ],
            )

        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output

        match = re.search(r"Promoted (\d+) memories", result.output)
        assert match is not None, f"Expected 'Promoted N memories' in: {result.output}"
        n = int(match.group(1))
        assert n >= 2

        # Items must now be in global layer
        global_files = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(global_files) >= 2

    def test_consolidate_empty_store_reports_zero(self, runner, cli_with_repo):
        """consolidate on empty store must report 'Promoted 0 memories'."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Promoted 0 memories" in result.output


class TestCaptureEphemeralDefault:
    """CLI tests for ephemeral-first capture default (issue #12)."""

    def test_capture_without_layer_defaults_to_ephemeral(self, runner, cli_with_repo, repo_root):
        """capture without --layer must write to .agent/runs/.../scratch/ and report ephemeral.

        New emoji-based notification format omits the (layer) suffix.
        The ephemeral layer maps to no emoji (ephemeral is not user-visible in ✻
        notification), so the output contains 'captured' but no (ephemeral) text.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "Ephemeral CLI default test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower()
        # New format omits (layer) suffix — ephemeral captures may show the ✻
        # notification or just the 'captured: <id>' line without a layer name
        assert "(ephemeral)" not in result.output

        # File must exist somewhere under .agent/runs/{run_id}/scratch/
        agent_runs = repo_root / ".agent" / "runs"
        matches = list(agent_runs.rglob("*.md"))
        assert len(matches) >= 1, f"Expected ephemeral file under {agent_runs}"

    def test_capture_explicit_layer_still_works(self, runner, cli_with_repo, repo_root):
        """capture with --layer global must still write to wiki/global/.

        New emoji-based notification format: ✻ 🧠 <brief>  (no (global) suffix).
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Explicit global layer", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output  # global emoji must appear
        assert "(global)" not in result.output  # suffix is gone in new format
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_capture_layer_is_optional_flag(self, runner, cli_with_repo):
        """capture must accept --content alone (--layer is now optional)."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "No layer flag provided"],
        )
        assert result.exit_code == 0, result.output


class TestCapturePositionalArg:
    """Tests for positional content argument in capture command (issue #28)."""

    def test_capture_positional_arg_works(self, runner, cli_with_repo, repo_root):
        """capture with a positional argument must store an item and print its ID."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "Positional content argument test"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower()

        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_capture_positional_arg_without_layer(self, runner, cli_with_repo, repo_root):
        """capture with positional argument and no --layer must default to ephemeral."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "Positional no layer test"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower()

    def test_capture_flag_overrides_positional(self, runner, cli_with_repo, repo_root):
        """--content flag must take precedence over positional argument."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Flag content", "Positional content"],
        )
        assert result.exit_code == 0, result.output
        # The --content flag wins; the item should be stored
        assert "captured" in result.output.lower()

        # Confirm the item content is the flag value, not the positional
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1
        file_text = matches[0].read_text()
        assert "Flag content" in file_text
        assert "Positional content" not in file_text

    def test_capture_no_content_shows_error(self, runner, cli_with_repo):
        """capture with no content (no positional arg and no --content) must exit nonzero."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global"],
        )
        assert result.exit_code != 0

    def test_capture_positional_with_flags(self, runner, cli_with_repo, repo_root):
        """capture positional arg must work alongside other flags like --quiet and --id."""
        result = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "global",
                "--id", "pos-arg-001",
                "--quiet",
                "Quiet positional content",
            ],
        )
        assert result.exit_code == 0, result.output
        # --quiet suppresses output; file must still be created
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1


class TestCaptureANSIOutput:
    """Tests for colored italic ANSI output in capture command (issue #15)."""

    def test_capture_notice_contains_ansi_by_default(self, runner, cli_with_repo):
        """capture notice must include ANSI escape codes when --no-color is not set."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "ANSI styled output test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" in result.output

    def test_capture_notice_no_color_flag_strips_ansi(self, runner, cli_with_repo):
        """--no-color flag must produce plain text output without ANSI codes.

        New emoji-based format: ✻ 🧠 <brief>  (no (global) suffix).
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Plain text output test", "--no-color"],
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" not in result.output
        # New format omits (layer) suffix
        assert "(global)" not in result.output

    def test_capture_notice_no_color_env_strips_ansi(self, runner, cli_with_repo):
        """NO_COLOR env var must produce plain text output without ANSI codes.

        New emoji-based format: ✻ 🧠 <brief>  (no (global) suffix).
        """
        import os
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "NO_COLOR env test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" not in result.output
        # New format omits (layer) suffix
        assert "(global)" not in result.output

    def test_capture_notice_project_layer_uses_blue(self, runner, cli_with_repo):
        """project layer notice must use blue ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "project", "--content", "Project layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[94m" in result.output  # bright blue

    def test_capture_notice_session_layer_uses_gray(self, runner, cli_with_repo):
        """session layer notice must use gray ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "session", "--content", "Session layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[90m" in result.output  # gray

    def test_capture_notice_global_layer_uses_yellow(self, runner, cli_with_repo):
        """global layer notice must use yellow/gold ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Global layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[33m" in result.output  # yellow/gold

    def test_capture_notice_includes_dim_and_italic(self, runner, cli_with_repo):
        """capture notice must include DIM and ITALIC ANSI codes."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "session", "--content", "Dim italic style test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[2m" in result.output  # dim
        assert "\033[3m" in result.output  # italic
        assert "\033[0m" in result.output  # reset

    def test_capture_notice_has_prefix_symbol(self, runner, cli_with_repo):
        """capture notice must include ✻ prefix symbol."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Prefix symbol test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "✻" in result.output
        assert "🧠" in result.output


class TestCaptureQuietFlag:
    """Tests for --quiet flag on the capture command.

    Method E (closes #22): reversed --quiet semantics.

    Without --quiet (default):
        The CLI emits BOTH the machine-readable 'captured: <uuid>' line AND
        the human-friendly '✻ <emoji> <brief> [id: <uuid>]' notification.
        This CLI output IS the canonical notification — AI must not duplicate it.

    With --quiet:
        Suppresses ALL output (both 'captured:' and the notification line).
        Reserved for scripts / migrate flows that manage their own output.
    """

    def test_capture_quiet_suppresses_notice(self, runner, cli_with_repo):
        """--quiet must suppress the capture_notice line (no ✻ 🧠).

        Method E: --quiet suppresses everything including the notification.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Quiet flag test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" not in result.output
        assert "✻" not in result.output

    def test_capture_quiet_suppresses_captured_id_line(self, runner, cli_with_repo):
        """--quiet must suppress the 'captured: <id>' line too (Method E behavior).

        Under Method E, --quiet is reserved for scripts and suppresses all stdout.
        Unlike the pre-Method-E behavior where --quiet still printed 'captured:',
        the new contract is: --quiet = no output at all.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Quiet ID output test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        # --quiet must produce no output at all
        assert result.output.strip() == "", (
            "--quiet should suppress all output (Method E); got: " + repr(result.output)
        )

    def test_capture_quiet_produces_no_output(self, runner, cli_with_repo):
        """--quiet must produce zero lines of output (Method E).

        Pre-Method-E tests asserted exactly one line ('captured: <id>').
        Under Method E, --quiet suppresses everything — the capture still
        happens, but nothing is printed to stdout.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Single line output test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 0, (
            "--quiet should produce no output under Method E; got: " + repr(result.output)
        )

    def test_capture_without_quiet_still_shows_notice(self, runner, cli_with_repo):
        """Without --quiet, capture_notice line must still be present."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Notice still shown test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "✻" in result.output

    def test_capture_quiet_with_ephemeral_layer(self, runner, cli_with_repo):
        """--quiet must suppress all output for ephemeral layer too (Method E)."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "Ephemeral quiet test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" not in result.output
        assert result.output.strip() == "", (
            "--quiet should produce no output under Method E"
        )

    # ------------------------------------------------------------------
    # Method E specific: without --quiet, notification includes [id: <uuid>]
    # ------------------------------------------------------------------

    def test_capture_without_quiet_emits_notification_line(self, runner, cli_with_repo):
        """Without --quiet, capture must emit the ✻ <emoji> notification line (Method E).

        The CLI output IS the canonical notification. AI must not duplicate it.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "session", "--content", "Method E notification test",
             "--no-color", "--id", "method-e-001"],
        )
        assert result.exit_code == 0, result.output
        assert "✻" in result.output
        assert "💡" in result.output  # session layer emoji

    def test_capture_without_quiet_notification_includes_id_suffix(self, runner, cli_with_repo):
        """Without --quiet, the notification line must include [id: <uuid>] suffix (Method E).

        The [id: <uuid>] suffix proves the capture actually executed and the id
        is real — AI text output cannot fabricate this because it comes from the
        tool call result.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "ID suffix test", "--no-color",
             "--id", "method-e-id-suffix-001"],
        )
        assert result.exit_code == 0, result.output
        assert "[id: method-e-id-suffix-001]" in result.output, (
            "Notification line must include [id: <uuid>] suffix under Method E; "
            "got: " + repr(result.output)
        )

    def test_capture_without_quiet_emits_both_captured_line_and_notification(self, runner, cli_with_repo):
        """Without --quiet, capture must emit BOTH 'captured: <id>' AND the notification.

        Method E makes the CLI stdout the canonical notification channel. Both
        lines must be present so the AI's tool result shows the id clearly AND
        the human-friendly notice.
        """
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "project", "--content", "Both lines test", "--no-color",
             "--id", "method-e-both-001"],
        )
        assert result.exit_code == 0, result.output
        assert "captured: method-e-both-001" in result.output, (
            "Must emit 'captured: <id>' line"
        )
        assert "✻" in result.output and "💾" in result.output, (
            "Must emit notification line with project emoji 💾"
        )
        assert "[id: method-e-both-001]" in result.output, (
            "Notification must carry [id: <uuid>] suffix"
        )


class TestAuditLimitAlias:
    """Tests for --limit as an alias for --tail on the audit command (closes #31).

    Users expect --limit based on convention from psql, gh, docker logs, etc.
    Both --tail and --limit must control the same entry count, with --limit
    taking precedence when both are supplied.
    """

    def _setup_obs_entries(self, repo_root, count: int = 10) -> None:
        """Write synthetic observability.jsonl entries to the test repo."""
        import json as _json
        from datetime import datetime, timezone

        # Observability log lives at wiki/observability.jsonl (MemoryGateway path).
        obs_path = repo_root / "wiki" / "observability.jsonl"
        obs_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(count):
            entry = {
                "ts": f"2026-05-17T10:00:{i:02d}Z",
                "event": "capture",
                "agent": "test",
                "session_id": "sess-001",
                "memory_id": f"item-{i:03d}",
                "layer": "global",
                "tags": [],
            }
            lines.append(_json.dumps(entry))
        obs_path.write_text("\n".join(lines) + "\n")

    def test_audit_limit_alias_exits_zero(self, runner, cli_with_repo, repo_root):
        """--limit must be accepted without error (exit code 0)."""
        self._setup_obs_entries(repo_root, count=10)
        result = runner.invoke(cli_with_repo, ["audit", "--limit", "5"])
        assert result.exit_code == 0, result.output

    def test_audit_tail_still_works(self, runner, cli_with_repo, repo_root):
        """--tail must still be accepted without regression (exit code 0)."""
        self._setup_obs_entries(repo_root, count=10)
        result = runner.invoke(cli_with_repo, ["audit", "--tail", "5"])
        assert result.exit_code == 0, result.output

    def test_audit_limit_returns_same_count_as_tail(self, runner, cli_with_repo, repo_root):
        """--limit N and --tail N must return the same number of entries."""
        self._setup_obs_entries(repo_root, count=10)
        result_tail = runner.invoke(cli_with_repo, ["audit", "--tail", "3"])
        result_limit = runner.invoke(cli_with_repo, ["audit", "--limit", "3"])
        assert result_tail.exit_code == 0, result_tail.output
        assert result_limit.exit_code == 0, result_limit.output
        # Both should show the same entry count footer
        assert "3 entries" in result_tail.output
        assert "3 entries" in result_limit.output

    def test_audit_limit_overrides_tail_default(self, runner, cli_with_repo, repo_root):
        """--limit must override the default --tail value (20)."""
        self._setup_obs_entries(repo_root, count=10)
        # With 10 entries, --limit 5 should return 5 (not the default 20)
        result = runner.invoke(cli_with_repo, ["audit", "--limit", "5"])
        assert result.exit_code == 0, result.output
        assert "5 entries" in result.output

    def test_audit_limit_when_both_provided_limit_wins(self, runner, cli_with_repo, repo_root):
        """When both --limit and --tail are supplied, --limit takes precedence."""
        self._setup_obs_entries(repo_root, count=10)
        # --limit 3 should win over --tail 7
        result = runner.invoke(cli_with_repo, ["audit", "--tail", "7", "--limit", "3"])
        assert result.exit_code == 0, result.output
        assert "3 entries" in result.output

    def test_audit_limit_with_json_flag(self, runner, cli_with_repo, repo_root):
        """--limit must also work with --json output mode."""
        self._setup_obs_entries(repo_root, count=10)
        result = runner.invoke(cli_with_repo, ["audit", "--limit", "4", "--json"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert len(lines) == 4, f"Expected 4 JSON lines, got {len(lines)}: {result.output}"


class TestLogDualMode:
    """Tests for dual-mode `mnemos log` command (closes #40).

    With no arguments, `log` reads the audit log (same as `audit`).
    With --op, --id, --layer, it appends an entry (existing write behaviour).
    Partial write arguments must produce a helpful error.
    """

    def _setup_obs_entries(self, repo_root, count: int = 5) -> None:
        """Write synthetic observability.jsonl entries to the test repo."""
        import json as _json

        obs_path = repo_root / "wiki" / "observability.jsonl"
        obs_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(count):
            entry = {
                "ts": f"2026-05-17T10:00:{i:02d}Z",
                "event": "capture",
                "agent": "test",
                "session_id": "sess-001",
                "memory_id": f"item-{i:03d}",
                "layer": "session",
                "tags": [],
            }
            lines.append(_json.dumps(entry))
        obs_path.write_text("\n".join(lines) + "\n")

    # --- Read mode ---

    def test_log_no_args_exits_zero(self, runner, cli_with_repo, repo_root):
        """mnemos log with no args must exit 0 (read mode)."""
        self._setup_obs_entries(repo_root)
        result = runner.invoke(cli_with_repo, ["log"])
        assert result.exit_code == 0, result.output

    def test_log_no_args_shows_table_header(self, runner, cli_with_repo, repo_root):
        """Read mode must print the TIMESTAMP/EVENT/AGENT/DETAIL header."""
        self._setup_obs_entries(repo_root)
        result = runner.invoke(cli_with_repo, ["log"])
        assert "TIMESTAMP" in result.output
        assert "EVENT" in result.output

    def test_log_no_args_shows_entries(self, runner, cli_with_repo, repo_root):
        """Read mode must display existing audit entries."""
        self._setup_obs_entries(repo_root, count=3)
        result = runner.invoke(cli_with_repo, ["log"])
        assert result.exit_code == 0, result.output
        assert "3 entries" in result.output

    def test_log_tail_limits_entries(self, runner, cli_with_repo, repo_root):
        """--tail must limit the number of entries shown in read mode."""
        self._setup_obs_entries(repo_root, count=10)
        result = runner.invoke(cli_with_repo, ["log", "--tail", "3"])
        assert result.exit_code == 0, result.output
        assert "3 entries" in result.output

    def test_log_json_flag_outputs_jsonl(self, runner, cli_with_repo, repo_root):
        """--json flag in read mode must output one JSON object per line."""
        import json as _json

        self._setup_obs_entries(repo_root, count=4)
        result = runner.invoke(cli_with_repo, ["log", "--json"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.strip().splitlines() if l.strip()]
        assert len(lines) == 4
        # Each line must be valid JSON
        for line in lines:
            parsed = _json.loads(line)
            assert "event" in parsed

    def test_log_no_args_empty_store_prints_message(self, runner, cli_with_repo):
        """Read mode with no entries must print a friendly empty message."""
        result = runner.invoke(cli_with_repo, ["log"])
        assert result.exit_code == 0, result.output
        assert "no audit log entries found" in result.output

    # --- Write mode ---

    def test_log_write_mode_exits_zero(self, runner, cli_with_repo):
        """Write mode with all three required flags must exit 0."""
        result = runner.invoke(
            cli_with_repo,
            ["log", "--op", "capture", "--id", "item-abc", "--layer", "session"],
        )
        assert result.exit_code == 0, result.output
        assert "logged:" in result.output

    def test_log_write_mode_confirms_entry(self, runner, cli_with_repo):
        """Write mode output must echo the op, id, and layer."""
        result = runner.invoke(
            cli_with_repo,
            ["log", "--op", "search", "--id", "item-xyz", "--layer", "project"],
        )
        assert result.exit_code == 0, result.output
        assert "search" in result.output
        assert "item-xyz" in result.output
        assert "project" in result.output

    # --- Partial / error cases ---

    def test_log_partial_args_op_only_exits_nonzero(self, runner, cli_with_repo):
        """Providing only --op (without --id and --layer) must exit non-zero."""
        result = runner.invoke(cli_with_repo, ["log", "--op", "capture"])
        assert result.exit_code != 0

    def test_log_partial_args_shows_helpful_error(self, runner, cli_with_repo):
        """Partial write args must print a message naming the missing flags."""
        result = runner.invoke(cli_with_repo, ["log", "--op", "capture"])
        combined = (result.output or "") + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "--id" in combined or "missing" in combined.lower()

    def test_log_partial_args_op_and_id_only(self, runner, cli_with_repo):
        """Providing --op and --id but not --layer must exit non-zero."""
        result = runner.invoke(
            cli_with_repo, ["log", "--op", "capture", "--id", "item-1"]
        )
        assert result.exit_code != 0
