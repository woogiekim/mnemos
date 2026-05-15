"""Click CLI — memory-* subcommands."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from mnemos.gateway import MemoryGateway
from mnemos.policy import PolicyViolationError


def _get_gateway() -> MemoryGateway:
    """Return a MemoryGateway using MNEMOS_REPO_ROOT env var or current dir."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    return MemoryGateway(repo_root=repo_root)


@click.group()
def cli() -> None:
    """mnemos — LLM Wiki Memory OS CLI."""


@cli.command("memory-capture")
@click.option("--layer", required=True, help="Target memory layer.")
@click.option("--content", required=True, help="Content to capture.")
@click.option("--id", "item_id", default=None, help="Optional item ID.")
@click.option("--tag", "tags", multiple=True, help="Tags to attach (repeatable).")
@click.option("--quality-score", default=0.8, type=float, help="Quality score (0.0–1.0).")
@click.option("--run-id", default=None, help="Run ID for ephemeral/working layers.")
@click.option("--session-id", default=None, help="Session ID for session layer.")
def memory_capture(
    layer: str,
    content: str,
    item_id: str | None,
    tags: tuple[str, ...],
    quality_score: float,
    run_id: str | None,
    session_id: str | None,
) -> None:
    """Capture a new memory item into the target layer."""
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
        click.echo(f"captured: {captured_id}")
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("memory-classify")
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


@cli.command("memory-search")
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
        return
    for r in results:
        click.echo(f"  [{r.get('source', '?')}] {r['item_id']}: {r['content'][:80]}")


@cli.command("memory-read")
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


@cli.command("memory-use")
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


@cli.command("memory-update")
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


@cli.command("memory-promote")
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


@cli.command("memory-demote")
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


@cli.command("memory-archive")
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


@cli.command("memory-forget")
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


@cli.command("install")
@click.argument("path", default=".", type=click.Path())
def install_cmd(path: str) -> None:
    """Scaffold a mnemos wiki repo structure at PATH (default: current directory)."""
    from mnemos.install import install
    install(Path(path))
    click.echo(f"mnemos installed at {path}")


@cli.command("memory-log")
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


@cli.command("memory-ingest-claude-md")
@click.option(
    "--project-root",
    "project_root",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True),
    help="Project root directory to scan for CLAUDE.md (default: current directory).",
)
@click.option("--run-id", "run_id", default="claude-md-ingest", help="Run ID for layer scoping.")
def memory_ingest_claude_md(project_root: str | None, run_id: str) -> None:
    """Discover and ingest CLAUDE.md files into memory.

    Scans two locations:

    \b
      ~/.claude/CLAUDE.md      → layer "global",  source_scope "global"
      <project-root>/CLAUDE.md → layer "project", source_scope "project"

    Missing files are silently skipped.
    """
    from agents.scanner import ClaudeMdScanner
    from agents.ingest import IngestAgent

    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    gw = _get_gateway()
    scanner = ClaudeMdScanner(project_root=root)
    agent = IngestAgent(gateway=gw)

    scan_results = scanner.discover()
    if not scan_results:
        click.echo("no CLAUDE.md files found — nothing ingested")
        return

    try:
        captured_ids = agent.run_scanner_results(scan_results, run_id=run_id)
    except PolicyViolationError as exc:
        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    for item_id in captured_ids:
        click.echo(f"ingested: {item_id}")
    click.echo(f"done: {len(captured_ids)} item(s) ingested")
