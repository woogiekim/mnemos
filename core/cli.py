"""Click CLI — mnemos subcommands."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from core.gateway import MemoryGateway
from core.output import capture_notice
from core.policy import PolicyViolationError


def _get_gateway() -> MemoryGateway:
    """Return a MemoryGateway using MNEMOS_REPO_ROOT env var or current dir."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    return MemoryGateway(repo_root=repo_root)


@click.group()
def cli() -> None:
    """mnemos — LLM Wiki Memory OS CLI."""


@cli.command("capture")
@click.option("--layer", default=None, help="Target memory layer (default: ephemeral).")
@click.option("--content", required=True, help="Content to capture.")
@click.option("--id", "item_id", default=None, help="Optional item ID.")
@click.option("--tag", "tags", multiple=True, help="Tags to attach (repeatable).")
@click.option("--quality-score", default=0.8, type=float, help="Quality score (0.0–1.0).")
@click.option("--run-id", default=None, help="Run ID for ephemeral/working layers.")
@click.option("--session-id", default=None, help="Session ID for session layer.")
@click.option("--no-color", "no_color", is_flag=True, default=False, help="Disable ANSI color output.")
def memory_capture(
    layer: str | None,
    content: str,
    item_id: str | None,
    tags: tuple[str, ...],
    quality_score: float,
    run_id: str | None,
    session_id: str | None,
    no_color: bool,
) -> None:
    """Capture a new memory item into the target layer (default: ephemeral)."""
    gw = _get_gateway()
    try:
        captured_id = gw.capture(
            layer=layer,
            content=content,
            item_id=item_id,
            tags=list(tags),
            quality_score=quality_score,
            run_id=run_id,
            session_id=session_id,
        )
        effective_layer = layer or "ephemeral"
        if captured_id is None:
            preview = content[:60]
            click.echo(f"[mnemos] Skipped (duplicate): \"{preview}\"")
        else:
            click.echo(f"captured: {captured_id}")
            preview = content[:60]
            suffix = "..." if len(content) > 60 else ""
            click.echo(capture_notice(f"{preview}{suffix}", effective_layer, no_color=no_color))
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("classify")
@click.argument("item_id")
@click.option("--tag", required=True, help="Tag to apply.")
@click.option("--layer", default=None, help="Layer hint (optional).")
def memory_classify(item_id: str, tag: str, layer: str | None) -> None:
    """Classify/tag a captured memory item."""
    gw = _get_gateway()
    try:
        gw.classify(item_id=item_id, tag=tag, layer=layer)
        click.echo(f"classified: {item_id} with tag '{tag}'")
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)
    except PolicyViolationError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("search")
@click.argument("query")
@click.option(
    "--layers",
    default=None,
    help="Comma-separated list of layers to search.",
)
@click.option("--limit", default=20, type=int, help="Maximum results.")
def memory_search(query: str, layers: str | None, limit: int) -> None:
    """Search across memory layers."""
    gw = _get_gateway()
    layer_list = [l.strip() for l in layers.split(",")] if layers else None
    results = gw.search(query=query, layers=layer_list, limit=limit)
    if not results:
        click.echo("no results found")
    else:
        for r in results:
            click.echo(f"  [{r.get('source', '?')}] {r['item_id']}: {r['content'][:80]}")
    click.echo(f"[mnemos] Retrieved {len(results)} memories")


@cli.command("read")
@click.argument("item_id")
def memory_read(item_id: str) -> None:
    """Read a specific memory item by ID or path."""
    gw = _get_gateway()
    try:
        item = gw.read(item_id=item_id)
        click.echo(json.dumps(item, default=str, indent=2))
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("use")
@click.argument("item_id")
def memory_use(item_id: str) -> None:
    """Mark a memory item as 'in use'."""
    gw = _get_gateway()
    try:
        gw.use(item_id=item_id)
        click.echo(f"used: {item_id}")
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("edit")
@click.argument("item_id")
@click.option("--content", required=True, help="New content.")
def memory_update(item_id: str, content: str) -> None:
    """Update content of an existing memory item."""
    gw = _get_gateway()
    try:
        gw.update(item_id=item_id, content=content)
        click.echo(f"updated: {item_id}")
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("promote")
@click.argument("item_id")
@click.option("--target-layer", default=None, help="Target layer (default: next layer).")
@click.option("--run-id", default=None, help="Run ID.")
@click.option("--session-id", default=None, help="Session ID.")
def memory_promote(
    item_id: str,
    target_layer: str | None,
    run_id: str | None,
    session_id: str | None,
) -> None:
    """Promote a memory item to the next (or specified) layer."""
    gw = _get_gateway()
    try:
        new_id = gw.promote(
            item_id=item_id,
            target_layer=target_layer,
            run_id=run_id,
            session_id=session_id,
        )
        click.echo(f"promoted: {new_id}")
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("demote")
@click.argument("item_id")
@click.option("--target-layer", required=True, help="Target layer.")
@click.option("--run-id", default=None, help="Run ID.")
@click.option("--session-id", default=None, help="Session ID.")
def memory_demote(
    item_id: str,
    target_layer: str,
    run_id: str | None,
    session_id: str | None,
) -> None:
    """Demote a memory item to a lower layer."""
    gw = _get_gateway()
    try:
        gw.demote(
            item_id=item_id,
            target_layer=target_layer,
            run_id=run_id,
            session_id=session_id,
        )
        click.echo(f"demoted: {item_id} → {target_layer}")
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("archive")
@click.argument("item_id")
def memory_archive(item_id: str) -> None:
    """Archive a memory item (soft-delete, retain content)."""
    gw = _get_gateway()
    try:
        gw.archive(item_id=item_id)
        click.echo(f"archived: {item_id}")
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("forget")
@click.argument("item_id")
@click.option("--force", is_flag=True, default=False, help="Skip confirmation prompt.")
def memory_forget(item_id: str, force: bool) -> None:
    """Hard-delete a memory item (requires archived state)."""
    gw = _get_gateway()
    if not force:
        click.confirm(f"Hard-delete '{item_id}'? This cannot be undone.", abort=True)
    try:
        gw.forget(item_id=item_id)
        click.echo(f"forgotten: {item_id}")
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)


@cli.command("doctor")
def doctor_cmd() -> None:
    """Check all detected host adapters for missing hooks and auto-repair them.

    Scans each known host adapter (Claude Code, Cursor) using is_present() to
    detect whether the host is active. For every detected adapter, verifies that
    all expected hooks and managed config blocks are registered. Missing hooks
    are automatically re-registered without requiring any flags.

    \b
    Output format per adapter:
      ClaudeCodeAdapter ... OK
      CursorAdapter ....... FIXED (re-registered 2 hook(s))
      SomeAdapter ......... SKIPPED (not detected)
    """
    from core.adapters import ClaudeCodeAdapter, CursorAdapter

    home = Path.home()
    adapters = [ClaudeCodeAdapter(), CursorAdapter()]

    all_ok = True
    for adapter in adapters:
        label = adapter.name
        dots = "." * max(1, 24 - len(label))

        if not adapter.is_present(home):
            click.echo(f"{label} {dots} SKIPPED (not detected)")
            continue

        ok, missing = adapter.verify_hooks(home)
        if ok:
            click.echo(f"{label} {dots} OK")
        else:
            # Auto-repair: re-register all hooks without requiring a flag
            adapter.install(home)
            count = len(missing)
            click.echo(f"{label} {dots} FIXED (re-registered {count} hook(s))")
            all_ok = False

    if all_ok:
        click.echo("[mnemos] All hooks verified — nothing to repair.")
    else:
        click.echo("[mnemos] doctor complete — missing hooks have been re-registered.")


@cli.command("install")
@click.argument("path", default=".", type=click.Path())
def install_cmd(path: str) -> None:
    """Scaffold a mnemos wiki repo structure at PATH (default: current directory)."""
    from core.install import install
    install(Path(path))
    click.echo(f"mnemos installed at {path}")


@cli.command("update")
@click.option(
    "--repo-root",
    "repo_root",
    default=None,
    envvar="MNEMOS_REPO_ROOT",
    help="Path to the mnemos source repo (default: MNEMOS_REPO_ROOT env var).",
)
@click.option(
    "--skip-git-pull",
    is_flag=True,
    default=False,
    help="Skip 'git pull origin main'.",
)
@click.option(
    "--skip-pipx",
    is_flag=True,
    default=False,
    help="Skip 'pipx reinstall mnemos'.",
)
def update_cmd(repo_root: str | None, skip_git_pull: bool, skip_pipx: bool) -> None:
    """Self-update mnemos: pull latest source, reinstall, and refresh managed config blocks.

    \b
    Steps performed:
      1. git pull origin main            (in the mnemos source repo)
      2. pipx reinstall mnemos
      3. Replace managed blocks in:
           ~/.claude/settings.json       (hook entries)
           ~/.claude/CLAUDE.md           (<!-- mnemos-start --> block)
           ~/.cursor/rules               (<!-- mnemos:start --> block)
      4. Print a unified diff of each changed block.
    """
    from core.updater import run_update

    exit_code = run_update(
        repo_root=repo_root,
        skip_git_pull=skip_git_pull,
        skip_pipx=skip_pipx,
    )
    sys.exit(exit_code)


@cli.command("uninstall")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (non-interactive mode).",
)
@click.option(
    "--purge",
    is_flag=True,
    default=False,
    help="Also run 'pipx uninstall mnemos' after config cleanup.",
)
def uninstall_cmd(yes: bool, purge: bool) -> None:
    """Remove all mnemos-managed entries from host config files.

    \b
    Removes managed sections from:
      ~/.claude/settings.json       (PostToolUse / UserPromptSubmit hook entries)
      ~/.claude/CLAUDE.md           (<!-- mnemos-start --> ... <!-- mnemos-end --> block)
      ~/.cursor/rules[.md]          (<!-- mnemos:start --> ... <!-- mnemos:end --> block)
      ~/.zshrc                      (export MNEMOS_REPO_ROOT=... line)

    Shows a unified diff of what will be removed, then prompts for confirmation
    unless --yes is given.  Use --purge to also uninstall the pipx package.
    """
    from core.uninstaller import run_uninstall

    exit_code = run_uninstall(yes=yes, purge=purge)
    sys.exit(exit_code)


@cli.command("log")
@click.option("--op", required=True, help="Operation name.")
@click.option("--id", "item_id", required=True, help="Item ID.")
@click.option("--layer", required=True, help="Layer name.")
@click.option("--meta", default=None, help="Additional metadata as JSON string.")
def memory_log(op: str, item_id: str, layer: str, meta: str | None) -> None:
    """Manually append an entry to the audit log."""
    gw = _get_gateway()
    metadata = None
    if meta:
        try:
            metadata = json.loads(meta)
        except json.JSONDecodeError as exc:
            click.echo(f"error: invalid JSON metadata — {exc}", err=True)
            sys.exit(1)
    gw.log(operation=op, item_id=item_id, layer=layer, metadata=metadata)
    click.echo(f"logged: {op} {item_id} ({layer})")


@cli.command("consolidate")
def memory_consolidate() -> None:
    """Sweep all memories and auto-promote eligible ones per policy.yaml.

    \b
    mnemos owns promotion entirely — no AI involvement required.
    Each memory is evaluated against the configured age_hours, access_count,
    and quality_score thresholds for its layer. Qualifying items are promoted
    to the next layer silently.

    Output:
      Promoted N memories
    """
    gw = _get_gateway()
    try:
        count = gw.consolidate()
        click.echo(f"Promoted {count} memories")
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("ingest-claude-md")
@click.option(
    "--project-root",
    "project_root",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True),
    help="Project root directory to scan for CLAUDE.md (default: current directory).",
)
@click.option("--run-id", "run_id", default="claude-md-ingest", help="Run ID for layer scoping.")
@click.option(
    "--skip-files",
    "skip_memory_files",
    is_flag=True,
    default=False,
    help="Skip syncing ~/.claude/projects/*/memory/*.md files.",
)
def memory_ingest_claude_md(
    project_root: str | None,
    run_id: str,
    skip_memory_files: bool,
) -> None:
    """Discover and ingest CLAUDE.md files and project memory files into memory.

    Scans the following locations:

    \b
      ~/.claude/CLAUDE.md                        → layer "global",  source_scope "global"
      <project-root>/CLAUDE.md                   → layer "project", source_scope "project"
      ~/.claude/projects/*/memory/*.md           → layer "global",  source_scope "claude_memory"
                                                   (dedup: skip/update/create by content hash)

    Missing files and directories are silently skipped.
    Pass --skip-files to omit the ~/.claude/projects memory sync.
    """
    from agents.scanner import ClaudeMdScanner
    from agents.ingest import IngestAgent

    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    gw = _get_gateway()
    scanner = ClaudeMdScanner(project_root=root)
    agent = IngestAgent(gateway=gw)

    # ── CLAUDE.md ingestion (global + project) ──────────────────────────
    scan_results = scanner.discover()
    if not scan_results:
        click.echo("no CLAUDE.md files found — skipping CLAUDE.md ingestion")
    else:
        try:
            dedup_claude = agent.run_scanner_results_dedup(
                scan_results,
                run_id=run_id,
                source_type="claude_md",
            )
        except PolicyViolationError as exc:
            click.echo(f"error: policy violation — {exc}", err=True)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)

        for item_id in dedup_claude["created"]:
            click.echo(f"ingested: {item_id}")
        for item_id in dedup_claude["updated"]:
            click.echo(f"claude-md updated: {item_id}")
        for item_id in dedup_claude["skipped"]:
            click.echo(f"claude-md skipped (unchanged): {item_id}")
        total_claude = sum(len(v) for v in dedup_claude.values())
        click.echo(
            f"claude-md: {len(dedup_claude['created'])} created, "
            f"{len(dedup_claude['updated'])} updated, "
            f"{len(dedup_claude['skipped'])} skipped "
            f"({total_claude} file(s) processed)"
        )

    # ── ~/.claude/projects/*/memory/*.md sync (with dedup) ──────────────
    if not skip_memory_files:
        memory_results = scanner.discover_memory_files()
        if not memory_results:
            click.echo("no project memory files found — skipping memory sync")
        else:
            try:
                dedup = agent.run_scanner_results_dedup(memory_results, run_id=run_id)
            except PolicyViolationError as exc:
                click.echo(f"error: policy violation — {exc}", err=True)
                sys.exit(1)
            except Exception as exc:
                click.echo(f"error: {exc}", err=True)
                sys.exit(1)

            for item_id in dedup["created"]:
                click.echo(f"memory created: {item_id}")
            for item_id in dedup["updated"]:
                click.echo(f"memory updated: {item_id}")
            for item_id in dedup["skipped"]:
                click.echo(f"memory skipped (unchanged): {item_id}")
            total = sum(len(v) for v in dedup.values())
            click.echo(
                f"memory-sync: {len(dedup['created'])} created, "
                f"{len(dedup['updated'])} updated, "
                f"{len(dedup['skipped'])} skipped "
                f"({total} file(s) processed)"
            )
