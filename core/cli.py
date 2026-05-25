"""Click CLI — mnemos subcommands."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import click

from core.gateway import MemoryGateway
from core.output import capture_notice
from core.policy import PolicyViolationError


# ---------------------------------------------------------------------------
# Bootstrap sync helper (stdlib-only — no core.* imports)
# ---------------------------------------------------------------------------

_BOOTSTRAP_SYNC_DIRS = ("core", "agents")


def _bootstrap_sync_source(repo_root: str | None) -> None:
    """Copy updated source directories from the dev repo to the install location.

    This function uses only stdlib (shutil, pathlib) so it can run at the very
    start of ``update_cmd``, BEFORE any ``from core.* import ...`` calls.  That
    ordering ensures newly-added functions (e.g. ``migrate_policy_transient``) are
    present in ``~/.mnemos/core/`` even when the currently-running binary loaded a
    stale copy — the "bootstrap problem" where a fresh git pull updates the dev
    repo but the runtime location is never refreshed.

    Args:
        repo_root: Path to the mnemos dev repo.  When ``None`` or empty the sync
            is silently skipped (no repo root known, nothing to copy from).
    """
    if not repo_root:
        return

    install_root = Path.home() / ".mnemos"
    repo_path = Path(repo_root)

    for dir_name in _BOOTSTRAP_SYNC_DIRS:
        src = repo_path / dir_name
        dst = install_root / dir_name
        if not src.is_dir():
            continue
        # Skip when source and destination resolve to the same path (editable
        # install where the dev tree IS the runtime location).
        try:
            if src.resolve() == dst.resolve():
                continue
        except OSError:
            pass
        try:
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        except Exception:
            # Non-fatal: if the copy fails the rest of the update still runs;
            # the user will see the original import error as a warning.
            pass


def _get_gateway() -> MemoryGateway:
    """Return a MemoryGateway using MNEMOS_REPO_ROOT env var or current dir."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    return MemoryGateway(repo_root=repo_root)


def _truncate_content(s: str, width: int, full: bool = False) -> str:
    """Return a preview of *s* with an ellipsis when truncated.

    Args:
        s:     The source string to preview.
        width: Maximum number of characters to show (ignored when *full* is True).
        full:  When True, return the original string unchanged.

    Returns:
        The original string when ``full=True`` or ``len(s) <= width``.
        Otherwise, ``s[:width] + "..."``.
    """
    if full:
        return s
    if len(s) <= width:
        return s
    return s[:width] + "..."


def _echo_json(payload: object) -> None:
    """Emit stable UTF-8 JSON for machine consumers."""
    click.echo(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


@click.group()
def cli() -> None:
    """mnemos — LLM Wiki Memory OS CLI."""


@cli.command("capture")
@click.argument("content_arg", required=False, default=None, metavar="[CONTENT]")
@click.option("--layer", default=None, help="Target memory layer (default: ephemeral).")
@click.option("--content", default=None, help="Content to capture.")
@click.option("--id", "item_id", default=None, help="Optional item ID.")
@click.option("--tag", "tags", multiple=True, help="Tags to attach (repeatable).")
@click.option("--quality-score", default=0.8, type=float, help="Quality score (0.0–1.0).")
@click.option("--run-id", default=None, help="Run ID for ephemeral/working layers.")
@click.option("--session-id", default=None, help="Session ID for session layer.")
@click.option("--no-color", "no_color", is_flag=True, default=False, help="Disable ANSI color output.")
@click.option("--quiet", "quiet", is_flag=True, default=False, help="Suppress capture notification output.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-contract JSON.")
@click.option(
    "--no-classify",
    "no_classify",
    is_flag=True,
    default=False,
    help="Skip automatic tag classification after capture.",
)
def memory_capture(
    content_arg: str | None,
    layer: str | None,
    content: str | None,
    item_id: str | None,
    tags: tuple[str, ...],
    quality_score: float,
    run_id: str | None,
    session_id: str | None,
    no_color: bool,
    quiet: bool,
    as_json: bool,
    no_classify: bool,
) -> None:
    """Capture a new memory item into the target layer (default: ephemeral).

    Content may be supplied as a positional argument or via --content:

    \b
      mnemos capture "some insight"
      mnemos capture --content "some insight"
    """
    # Resolve content: --content flag takes precedence over positional argument.
    if content is None:
        content = content_arg
    if not content:
        raise click.UsageError("Missing content. Pass it as a positional argument or via --content.")

    # Defensive fallback: if --session-id was not passed, check env var set by
    # the UserPromptSubmit hook so observability correlation still works even
    # when Claude omits the flag from the injected capture protocol command.
    if session_id is None:
        session_id = os.environ.get("MNEMOS_SESSION_ID")
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
            no_classify=no_classify,
        )
        effective_layer = layer or "ephemeral"
        if as_json:
            status = "duplicate" if captured_id is None or gw.last_capture_was_duplicate else "captured"
            payload = {"status": status, "id": captured_id, "layer": effective_layer}
            if captured_id is not None:
                try:
                    from core.provider import memory_item_payload
                    payload["item"] = memory_item_payload(gw._store.read(captured_id))
                except Exception:
                    payload["item"] = None
            _echo_json(payload)
            return

        if captured_id is None:
            # In-process duplicate: same gateway instance saw this content before
            # (e.g. Stop hook firing multiple times per session).  Silent no-op.
            preview = content[:60]
            click.echo(f"[mnemos] Skipped (duplicate): \"{preview}\"")
        elif gw.last_capture_was_duplicate:
            # Cross-process duplicate (Issues #49/#50): identical content was
            # written by a previous `mnemos capture` invocation.  Return the
            # existing ID so the caller knows which record to reference.
            click.echo(f"(existing) {captured_id}")
        else:
            if not quiet:
                # Method E: CLI stdout IS the notification.
                # Without --quiet: emit both the machine-readable id line AND
                # the human-friendly notification with the real [id: <uuid>].
                # AI must NOT duplicate this output — the tool result is the notice.
                preview = content[:60]
                suffix = "..." if len(content) > 60 else ""
                click.echo(f"captured: {captured_id}")
                click.echo(
                    capture_notice(
                        f"{preview}{suffix}",
                        effective_layer,
                        item_id=captured_id,
                        no_color=no_color,
                    ),
                    color=not no_color,
                )
            else:
                # --quiet: suppress everything (reserved for scripts / migrate flows).
                # No captured: line, no notification. Used internally by hooks that
                # want silent operation.
                pass
    except PolicyViolationError as exc:
        if as_json:
            from core.provider import error_payload

            _echo_json(error_payload(
                code="policy_violation",
                message=str(exc),
                retryable=False,
            ))
            sys.exit(1)

        click.echo(f"error: policy violation — {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        if as_json:
            from core.provider import provider_error_from_exception

            _echo_json(provider_error_from_exception(exc))
            sys.exit(1)

        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("classify")
@click.argument("item_id", required=False, default=None)
@click.option("--tag", default=None, help="Tag to apply (required when classifying a single item).")
@click.option("--layer", default=None, help="Layer hint (optional).")
@click.option(
    "--all",
    "--auto",
    "classify_all",
    is_flag=True,
    default=False,
    help="Auto-classify every memory item in the store.",
)
@click.option(
    "--untagged",
    "untagged_only",
    is_flag=True,
    default=False,
    help="When used with --all, skip items that already have at least one tag.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Preview which tags would be applied without modifying any items.",
)
def memory_classify(
    item_id: str | None,
    tag: str | None,
    layer: str | None,
    classify_all: bool,
    untagged_only: bool,
    dry_run: bool,
) -> None:
    """Classify/tag a captured memory item.

    Single-item mode (default):
      mnemos classify <ITEM_ID> --tag <tag>

    Backfill mode — auto-classify every item:
      mnemos classify --all

    Backfill mode — auto-classify only untagged items:
      mnemos classify --all --untagged

    Backfill mode — preview without applying:
      mnemos classify --all --dry-run
    """
    gw = _get_gateway()

    if classify_all:
        # Backfill mode: iterate all layers, optionally skip already-tagged items.
        from core.layers import LAYER_STATIC_PATHS

        static_layers = list(LAYER_STATIC_PATHS.keys())
        dynamic_layers = ["ephemeral", "working", "session"]
        all_layers = static_layers + [l for l in dynamic_layers if l not in static_layers]

        classified_count = 0
        skipped_count = 0

        for lyr in all_layers:
            try:
                for item in gw._store.iter_layer_items(lyr):
                    iid = item.get("id") or ""
                    if not iid:
                        continue
                    existing_tags = item.get("tags") or []
                    if untagged_only and existing_tags:
                        skipped_count += 1
                        continue
                    content = item.get("content", "")
                    new_tags = gw.auto_classify(item_id=iid, content=content)
                    if new_tags:
                        classified_count += 1
                        if dry_run:
                            click.echo(f"[dry-run] would classify: {iid} → {new_tags}")
                        else:
                            click.echo(f"  classified: {iid} → {new_tags}")
            except Exception:
                continue

        if dry_run:
            click.echo(
                f"[dry-run] {classified_count} item(s) would be classified"
                + (f", {skipped_count} skipped (already tagged)" if untagged_only else "")
            )
        else:
            click.echo(
                f"[mnemos classify] {classified_count} item(s) classified"
                + (f", {skipped_count} skipped (already tagged)" if untagged_only else "")
            )
        return

    # Single-item mode
    if item_id is None:
        click.echo("error: provide an ITEM_ID or use --all for backfill", err=True)
        sys.exit(1)
    if tag is None:
        click.echo("error: --tag is required when classifying a single item", err=True)
        sys.exit(1)

    try:
        gw.classify(item_id=item_id, tag=tag, layer=layer)
        click.echo(f"classified: {item_id} with tag '{tag}'")
    except FileNotFoundError:
        click.echo(f"error: item '{item_id}' not found", err=True)
        sys.exit(1)
    except PolicyViolationError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("list")
@click.option(
    "--layer",
    "layers",
    default=None,
    help="Comma-separated list of layers to include (default: all).",
)
@click.option("--limit", default=None, type=int, help="Maximum number of items to show.")
@click.option("--full", "full", is_flag=True, default=False, help="Show full content without truncation.")
@click.option("--width", "width", default=80, type=int, help="Preview width in characters (default: 80).")
def memory_list(layers: str | None, limit: int | None, full: bool, width: int) -> None:
    """List memory items across all layers (or specified layers)."""
    gw = _get_gateway()
    layer_list = [l.strip() for l in layers.split(",")] if layers else None
    items = gw.list_all(layers=layer_list, limit=limit)
    if not items:
        click.echo("no memories found")
    else:
        for item in items:
            preview = _truncate_content(item["content"], width=width, full=full)
            click.echo(f"  [{item['layer']}] {item['item_id']}: {preview}")
    click.echo(f"[mnemos] {len(items)} memories")


@cli.command("search")
@click.argument("query", required=False, default="")
@click.option(
    "--layers",
    "--layer",
    "layers",
    default=None,
    help="Comma-separated list of layers to search (also: --layer).",
)
@click.option("--limit", default=20, type=int, help="Maximum results.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-contract JSON.")
@click.option("--fast", "fast", is_flag=True, default=False, help="Use the stable fast-search provider path.")
@click.option("--full", "full", is_flag=True, default=False, help="Show full content without truncation.")
@click.option("--width", "width", default=80, type=int, help="Preview width in characters (default: 80).")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Filter results to items with this tag (repeatable; AND logic).",
)
def memory_search(query: str, layers: str | None, limit: int, as_json: bool, fast: bool, full: bool, width: int, tags: tuple) -> None:
    """Search across memory layers.

    \b
    Examples:
      mnemos search "architecture"
      mnemos search "workflow" --layer session
      mnemos search --tag testing
      mnemos search "auth" --tag project --tag architecture
    """
    gw = _get_gateway()
    layer_list = [l.strip() for l in layers.split(",")] if layers else None
    tag_list = list(tags) if tags else None
    try:
        results = gw.search(query=query, layers=layer_list, limit=limit, tags=tag_list)
    except Exception as exc:
        if as_json:
            from core.provider import provider_error_from_exception, search_payload

            payload = search_payload(
                query=query,
                results=[],
                mode="fast" if fast else "standard",
                partial_failure=True,
            )
            payload.update(provider_error_from_exception(exc))
            payload["status"] = "degraded"
            _echo_json(payload)
            return

        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    if as_json:
        from core.provider import search_payload
        _echo_json(search_payload(
            query=query,
            results=results,
            mode="fast" if fast else "standard",
            partial_failure=False,
        ))
        return

    if not results:
        click.echo("no results found")
    else:
        for r in results:
            preview = _truncate_content(r["content"], width=width, full=full)
            click.echo(f"  [{r.get('source', '?')}] {r['item_id']}: {preview}")
    click.echo(f"[mnemos] Retrieved {len(results)} memories")


@cli.command("context")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-style JSON.")
@click.option("--render", "render", is_flag=True, default=False, help="Render a <mnemos-context> block.")
@click.option("--prompt", required=True, help="Prompt text used for deterministic keyword retrieval.")
@click.option("--session-id", default=None, help="Host session identifier.")
@click.option("--host", default="unknown", help="Host adapter name.")
@click.option("--limit", default=5, type=int, help="Maximum memories to inject.")
@click.option("--max-chars", default=1800, type=int, help="Maximum total injected content characters.")
def context_cmd(
    as_json: bool,
    render: bool,
    prompt: str,
    session_id: str | None,
    host: str,
    limit: int,
    max_chars: int,
) -> None:
    """Retrieve deterministic V1 context for host injection."""
    if as_json and render:
        raise click.UsageError("--json and --render are mutually exclusive.")
    if not as_json and not render:
        raise click.UsageError("Pass --json or --render.")

    from core.context import render_context_block, retrieve_context

    payload = retrieve_context(
        prompt=prompt,
        session_id=session_id,
        host=host,
        limit=limit,
        max_chars=max_chars,
    )
    if as_json:
        _echo_json(payload)
        return
    rendered = render_context_block(payload)
    if rendered:
        click.echo(rendered)


@cli.command("capture-transcript")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-style JSON.")
@click.option("--transcript-path", required=True, type=click.Path(exists=True, dir_okay=False), help="Transcript JSON/JSONL path.")
@click.option("--session-id", default=None, help="Host session identifier.")
@click.option("--host", default="unknown", help="Host adapter name.")
def capture_transcript_cmd(
    as_json: bool,
    transcript_path: str,
    session_id: str | None,
    host: str,
) -> None:
    """Extract deterministic durable insights from a host transcript."""
    from core.transcript import capture_transcript

    try:
        payload = capture_transcript(
            transcript_path=transcript_path,
            session_id=session_id,
            host=host,
        )
    except Exception as exc:
        from core.provider import provider_error_from_exception

        _echo_json(provider_error_from_exception(exc))
        sys.exit(1)

    if as_json:
        _echo_json(payload)
        return

    click.echo(
        "[mnemos capture-transcript] "
        f"captured={payload['captured_count']} "
        f"duplicates={payload['duplicate_count']} "
        f"skipped={payload['skipped_count']}"
    )


@cli.command("read")
@click.argument("item_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-contract JSON.")
def memory_read(item_id: str, as_json: bool) -> None:
    """Read a specific memory item by ID or path."""
    gw = _get_gateway()
    try:
        item = gw.read(item_id=item_id)
        if as_json:
            from core.provider import memory_item_payload
            _echo_json(memory_item_payload(item))
        else:
            _echo_json(item)
    except FileNotFoundError:
        if as_json:
            from core.provider import error_payload
            _echo_json(error_payload(
                code="not_found",
                message=f"item '{item_id}' not found",
                retryable=False,
            ))
            sys.exit(1)
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
@click.option("--target-layer", "--layer", default=None, help="Target layer (default: next layer).")
@click.option("--run-id", default=None, help="Run ID.")
@click.option("--session-id", default=None, help="Session ID.")
@click.option("--quiet", "quiet", is_flag=True, default=False, help="Suppress promotion notification output.")
@click.option("--no-color", "no_color", is_flag=True, default=False, help="Disable ANSI color output.")
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Bypass age and policy checks (use with caution).",
)
def memory_promote(
    item_id: str,
    target_layer: str | None,
    run_id: str | None,
    session_id: str | None,
    quiet: bool,
    no_color: bool,
    force: bool,
) -> None:
    """Promote a memory item to the next (or specified) layer.

    On success, emits 'promoted: <id>' followed by a '✻ 🧠 promoted <id> → <layer>'
    notice (suppressed when --quiet is given).  When the promotion is rejected by
    policy (e.g. already at the top layer), the policy error is printed instead.
    """
    from core.output import promote_notice
    gw = _get_gateway()
    try:
        # Resolve the effective target layer before promoting (for notice output).
        item = gw._store.read(item_id)
        current_layer = item.get("layer", "")
        effective_target = target_layer
        if effective_target is None:
            effective_target = gw._policy.get_next_layer(current_layer)

        if force:
            click.echo(
                "warning: forcing promotion — age/policy checks bypassed",
                err=True,
            )

        new_id = gw.promote(
            item_id=item_id,
            target_layer=target_layer,
            run_id=run_id,
            session_id=session_id,
            force=force,
        )
        click.echo(f"promoted: {new_id}")
        if not quiet:
            # Emit the promotion notice directly (mirrors how `capture` emits its notice).
            # The event bus also fires a post-promote event for consolidate/bg-check paths,
            # but the promote CLI command owns its notice output independently.
            click.echo(
                promote_notice(
                    item_id=new_id,
                    target_layer=effective_target or "?",
                    no_color=no_color,
                ),
                color=not no_color,
            )
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
@click.option("--force", "--yes", "-y", "force", is_flag=True, default=False, help="Skip confirmation prompt (also: --yes / -y).")
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


@cli.command("delete")
@click.argument("item_id")
def memory_delete(item_id: str) -> None:
    """Unconditionally delete a memory item by ID.

    Unlike the 'forget' command, 'delete' does not require the item to be
    archived first.  Use it to remove transient captures (ephemeral/session
    layer) that should be cleaned up immediately.

    \b
      mnemos delete <ITEM_ID>
    """
    gw = _get_gateway()
    try:
        gw.delete(item_id=item_id)
        click.echo(f"deleted: {item_id}")
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


@cli.command("capabilities")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def capabilities_cmd(as_json: bool) -> None:
    """Show stable provider capability metadata."""
    from core.provider import capabilities_payload

    payload = capabilities_payload()
    if as_json:
        _echo_json(payload)
        return
    for name, value in payload["capabilities"].items():
        click.echo(f"{name}: {value}")


@cli.group("daemon")
def daemon_cmd() -> None:
    """Manage the autonomous mnemos daemon runtime."""


@daemon_cmd.command("run")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def daemon_run_cmd(as_json: bool) -> None:
    """Run one autonomous daemon maintenance cycle."""
    from core.daemon import manage_autonomous_daemon

    manage_autonomous_daemon("run", as_json=as_json)


@daemon_cmd.command("status")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def daemon_status_cmd(as_json: bool) -> None:
    """Show autonomous daemon status."""
    from core.daemon import manage_autonomous_daemon

    manage_autonomous_daemon("status", as_json=as_json)


@daemon_cmd.command("install")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def daemon_install_cmd(as_json: bool) -> None:
    """Install the autonomous daemon launchd job."""
    from core.daemon import manage_autonomous_daemon

    manage_autonomous_daemon("install", as_json=as_json)


@daemon_cmd.command("uninstall")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def daemon_uninstall_cmd(as_json: bool) -> None:
    """Uninstall the autonomous daemon launchd job."""
    from core.daemon import manage_autonomous_daemon

    manage_autonomous_daemon("uninstall", as_json=as_json)


@cli.command("version")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output machine-readable JSON.")
def version_cmd(as_json: bool) -> None:
    """Show mnemos version and provider contract metadata."""
    from core.provider import version_payload

    payload = version_payload()
    if as_json:
        _echo_json(payload)
        return
    click.echo(str(payload["version"]))


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
    # Bootstrap sync — must run BEFORE any core.* imports so that newly-added
    # functions (e.g. migrate_policy_transient) are present in the runtime
    # ~/.mnemos/core/ even when the running binary loaded a stale copy.
    # Uses only stdlib (shutil, pathlib) to avoid the very import-failure it
    # is designed to prevent.
    _bootstrap_sync_source(repo_root or os.environ.get("MNEMOS_REPO_ROOT"))

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
@click.option("--op", default=None, help="Operation name (write mode).")
@click.option("--id", "item_id", default=None, help="Item ID (write mode).")
@click.option("--layer", default=None, help="Layer name (write mode).")
@click.option("--meta", default=None, help="Additional metadata as JSON string.")
@click.option("--tail", default=20, type=int, help="Number of entries to show in read mode (default: 20).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON lines in read mode.")
def memory_log(op: str | None, item_id: str | None, layer: str | None, meta: str | None, tail: int, as_json: bool) -> None:
    """View or append audit log entries.

    \b
    Read mode (no arguments): show recent audit entries.
      mnemos log                       # last 20 entries
      mnemos log --tail 50             # last 50 entries
      mnemos log --json                # raw JSON output

    Write mode (--op, --id, --layer required): append an entry.
      mnemos log --op capture --id <ID> --layer session
    """
    gw = _get_gateway()

    # Read mode: no write options provided
    if op is None and item_id is None and layer is None:
        obs = gw.observability
        entries = obs.read_entries(tail=tail)
        if not entries:
            click.echo("no audit log entries found")
            return
        if as_json:
            import json as _json
            for entry in entries:
                click.echo(_json.dumps(entry, ensure_ascii=False))
            return
        # Human-readable table: timestamp | event | agent | detail
        click.echo(f"{'TIMESTAMP':<22}  {'EVENT':<22}  {'AGENT':<10}  DETAIL")
        click.echo("-" * 80)
        for entry in entries:
            ts = entry.get("ts", "")[:19]
            event = entry.get("event", "")
            agent = entry.get("agent", "")
            session = entry.get("session_id", "")[:12]

            # Build a short human detail string per event type
            if event in ("hook_search", "search"):
                kws = ", ".join(entry.get("keywords", []))[:30]
                cnt = entry.get("result_count", 0)
                detail = f"kw={kws!r} results={cnt}"
            elif event == "hook_session_start":
                cnt = entry.get("memory_count", 0)
                detail = f"session={session} memories_loaded={cnt}"
            elif event == "capture":
                mid = entry.get("memory_id", "")[:16]
                elayer = entry.get("layer", "")
                tags = entry.get("tags", [])
                detail = f"id={mid} layer={elayer} tags={tags}"
            elif event == "gc":
                n = entry.get("archived_count", 0)
                dr = " [dry-run]" if entry.get("dry_run") else ""
                detail = f"archived={n}{dr}"
            elif event == "promotion":
                mid = entry.get("memory_id", "")[:16]
                fl = entry.get("from_layer", "")
                tl = entry.get("layer", "")
                detail = f"id={mid} {fl}→{tl}"
            elif event == "hook_post_tool":
                tool = entry.get("tool_name", "")
                detail = f"tool={tool} session={session}"
            else:
                detail = str(entry)[:60]

            click.echo(f"{ts:<22}  {event:<22}  {agent:<10}  {detail}")

        click.echo(f"\n[mnemos] {len(entries)} entries")
        return

    # Write mode: validate all required fields are present
    missing = [name for name, val in [("--op", op), ("--id", item_id), ("--layer", layer)] if val is None]
    if missing:
        click.echo(f"error: write mode requires all of --op, --id, --layer (missing: {', '.join(missing)})", err=True)
        sys.exit(1)

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
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Preview promotions without applying them.",
)
def memory_consolidate(dry_run: bool) -> None:
    """Sweep all memories and auto-promote eligible ones per policy.yaml.

    \b
    mnemos owns promotion entirely — no AI involvement required.
    Each memory is evaluated against the configured age_hours, access_count,
    and quality_score thresholds for its layer. Qualifying items are promoted
    to the next layer silently.

    Output:
      Promoted N memories
    """
    from core.adapters.claude import ClaudeCodeAdapter
    gw = _get_gateway()
    ClaudeCodeAdapter().subscribe_to_event_bus(gw.event_bus)
    try:
        import inspect
        sig = inspect.signature(gw.consolidate)
        if "dry_run" in sig.parameters:
            count = gw.consolidate(dry_run=dry_run)
        else:
            count = gw.consolidate()
        if dry_run:
            click.echo(f"[dry-run] Would promote {count} memories")
        else:
            click.echo(f"Promoted {count} memories")
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("gc")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would be archived without actually archiving.",
)
@click.option(
    "--layer",
    "layers",
    default=None,
    help=(
        "Comma-separated list of layers to collect from "
        "(default: all — ephemeral, working, session, project, global)."
    ),
)
@click.option(
    "--threshold",
    "gc_threshold",
    default=None,
    type=float,
    help="Minimum garbage score [0.0–1.0] to collect a memory (default: 0.7).",
)
@click.option(
    "--staleness-hours",
    "staleness_hours",
    default=None,
    type=float,
    help="Age in hours beyond which staleness starts saturating toward 1.0 (default: 24.0).",
)
@click.option(
    "--limit",
    "limit",
    default=None,
    type=int,
    help="Maximum number of memories to archive per run (default: 100).",
)
@click.option(
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help="Include score breakdowns in the output.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output provider-contract JSON.")
@click.option(
    "--install-daemon",
    "install_daemon",
    is_flag=True,
    default=False,
    help=(
        "macOS only: install a launchd plist at "
        "~/Library/LaunchAgents/com.mnemos.gc.plist that runs "
        "mnemos bg-check --quiet daily at 3 AM. "
        "GC log is written to ~/.mnemos/.logs/gc.log."
    ),
)
@click.option(
    "--uninstall-daemon",
    "uninstall_daemon",
    is_flag=True,
    default=False,
    help=(
        "macOS only: unload and remove the launchd plist installed by "
        "--install-daemon."
    ),
)
def memory_gc(
    dry_run: bool,
    layers: str | None,
    gc_threshold: float | None,
    staleness_hours: float | None,
    limit: int | None,
    verbose: bool,
    as_json: bool,
    install_daemon: bool,
    uninstall_daemon: bool,
) -> None:
    """Run G1GC-style garbage collection on memory layers.

    \b
    Scores each memory by staleness, access frequency, quality, and lifecycle
    stage. Groups memories into layer regions, prioritises the most garbage-
    heavy regions first (Garbage-First), then archives memories that exceed
    the configured score threshold.

    Archives are soft-deletes (stage=archived) — no memory is ever
    hard-deleted by this command.

    \b
    Examples:
      mnemos gc --dry-run
      mnemos gc --layer ephemeral,working --threshold 0.6
      mnemos gc --staleness-hours 48 --limit 50 --verbose
      mnemos gc --install-daemon      # install macOS launchd daily GC
      mnemos gc --uninstall-daemon    # remove the launchd daemon
    """
    from core.gc import (
        GarbageCollector,
        DEFAULT_STALENESS_HOURS,
        DEFAULT_GC_THRESHOLD,
        DEFAULT_LIMIT,
    )

    # ── Daemon install/uninstall (macOS launchd) ─────────────────────────────
    if install_daemon or uninstall_daemon:
        from core.daemon import manage_gc_daemon
        manage_gc_daemon(install=install_daemon, uninstall=uninstall_daemon)
        return

    gw = _get_gateway()
    repo_root = gw._root

    layer_list = [l.strip() for l in layers.split(",")] if layers else None

    gc = GarbageCollector(
        repo_root=repo_root,
        staleness_hours=staleness_hours if staleness_hours is not None else DEFAULT_STALENESS_HOURS,
        gc_threshold=gc_threshold if gc_threshold is not None else DEFAULT_GC_THRESHOLD,
        limit=limit if limit is not None else DEFAULT_LIMIT,
        layers=layer_list,
    )

    try:
        report = gc.run(dry_run=dry_run)
    except Exception as exc:
        if as_json:
            from core.provider import provider_error_from_exception

            _echo_json(provider_error_from_exception(exc))
            sys.exit(1)

        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    if as_json:
        _echo_json({
            "status": "dry_run" if dry_run else "completed",
            "archived_count": report.archived,
            "regions_processed": report.regions_processed,
            "items": report.archived_items,
        })
        return

    for line in report.summary_lines():
        click.echo(line)

    if verbose and report.archived_items:
        click.echo("")
        click.echo("Score breakdowns:")
        for item in report.archived_items:
            bd = item.get("score_breakdown", {})
            click.echo(
                f"  {item['item_id']}"
                f"  staleness={bd.get('staleness', 0):.3f}"
                f"  access={bd.get('access', 0):.3f}"
                f"  quality={bd.get('quality', 0):.3f}"
                f"  stage={bd.get('stage', 0):.3f}"
            )


@cli.command("bg-check")
@click.option(
    "--interval",
    "interval_minutes",
    default=None,
    type=int,
    help="Throttle interval in minutes (default: 5). Pass 0 to force a run.",
)
@click.option(
    "--no-gc",
    "gc_disabled",
    is_flag=True,
    default=False,
    help="Skip garbage-collection phase.",
)
@click.option(
    "--no-promote",
    "promote_disabled",
    is_flag=True,
    default=False,
    help="Skip auto-promotion phase.",
)
@click.option(
    "--no-dedup",
    "dedup_disabled",
    is_flag=True,
    default=False,
    help="Skip duplicate-detection phase.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Bypass throttle and always run (useful for testing).",
)
@click.option(
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help="Print a summary even when there is no activity.",
)
@click.option(
    "--quiet",
    "quiet",
    is_flag=True,
    default=False,
    help=(
        "Suppress all output (including the background-activity context block). "
        "Useful when called from daemons or crew:update where stdout must stay clean."
    ),
)
def bg_check_cmd(
    interval_minutes: int | None,
    gc_disabled: bool,
    promote_disabled: bool,
    dedup_disabled: bool,
    force: bool,
    verbose: bool,
    quiet: bool,
) -> None:
    """Run autonomous background maintenance (GC + auto-promote + dedup).

    \b
    This command is intended to be called by the PostToolUse hook after every
    Claude tool call.  It is throttled (runs at most once per --interval
    minutes) and silent unless it actually does something.

    When activity occurs, a <mnemos-context type="background-activity"> block
    is emitted to stdout so Claude sees a brief summary injected into context.
    Pass --quiet to suppress all output (useful in daemons and crew:update).

    \b
    Examples:
      mnemos bg-check                   # throttled, silent unless active
      mnemos bg-check --force --verbose # always run, always print summary
      mnemos bg-check --no-gc           # skip GC phase
      mnemos bg-check --quiet           # completely silent (daemon / update use)
    """
    from core.bg import (
        run_background_check,
        DEFAULT_INTERVAL_MINUTES,
    )

    gw = _get_gateway()
    repo_root = str(gw._root)

    effective_interval = DEFAULT_INTERVAL_MINUTES if interval_minutes is None else interval_minutes

    result = run_background_check(
        repo_root=repo_root,
        interval_minutes=effective_interval,
        gc_enabled=not gc_disabled,
        auto_promote_enabled=not promote_disabled,
        dedup_enabled=not dedup_disabled,
        force=force or (interval_minutes == 0),
    )

    if quiet:
        # --quiet: suppress all output regardless of activity
        return

    if not result.ran:
        # Throttled — completely silent (no output)
        return

    if result.has_activity:
        click.echo(result.to_context_block())
    elif verbose:
        click.echo(
            f"[mnemos bg] check complete — nothing to do "
            f"({result.elapsed_ms:.0f} ms)"
        )


@cli.command("audit")
@click.option("--tail", "tail", default=20, type=int, help="Number of most recent entries to show (default: 20).")
@click.option("--limit", "limit", default=None, type=int, help="Alias for --tail (convention from psql, gh, docker logs, etc.).")
@click.option("--session", "session_id", default=None, help="Filter by session ID.")
@click.option("--event", "events", default=None, help="Comma-separated event type filter (e.g. hook_search,capture).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON lines instead of a table.")
def audit_cmd(tail: int, limit: int | None, session_id: str | None, events: str | None, as_json: bool) -> None:
    """Show recent observability log entries (hook calls, captures, searches).

    \b
    Examples:
      mnemos audit                         # last 20 events
      mnemos audit --tail 50               # last 50 events
      mnemos audit --limit 50              # same as --tail (conventional alias)
      mnemos audit --session SESSION_ID    # filter by session
      mnemos audit --event hook_search,capture  # filter by event type
      mnemos audit --json                  # raw JSONL output
    """
    # --limit is a conventional alias for --tail (psql, gh, docker logs, etc.)
    # When --limit is provided it takes precedence over the --tail default.
    effective_tail = limit if limit is not None else tail

    gw = _get_gateway()
    obs = gw.observability

    event_list = [e.strip() for e in events.split(",")] if events else None

    entries = obs.read_entries(
        tail=effective_tail,
        session_id=session_id,
        events=event_list,
    )

    if not entries:
        click.echo("no observability entries found")
        return

    if as_json:
        import json as _json
        for entry in entries:
            click.echo(_json.dumps(entry, ensure_ascii=False))
        return

    # Human-readable table: timestamp | event | agent | detail
    click.echo(f"{'TIMESTAMP':<22}  {'EVENT':<22}  {'AGENT':<10}  DETAIL")
    click.echo("-" * 80)
    for entry in entries:
        ts = entry.get("ts", "")[:19]  # drop sub-second / Z
        event = entry.get("event", "")
        agent = entry.get("agent", "")
        session = entry.get("session_id", "")[:12]

        # Build a short human detail string per event type
        if event in ("hook_search", "search"):
            kws = ", ".join(entry.get("keywords", []))[:30]
            cnt = entry.get("result_count", 0)
            detail = f"kw={kws!r} results={cnt}"
        elif event == "hook_session_start":
            cnt = entry.get("memory_count", 0)
            detail = f"session={session} memories_loaded={cnt}"
        elif event == "capture":
            mid = entry.get("memory_id", "")[:16]
            layer = entry.get("layer", "")
            tags = entry.get("tags", [])
            detail = f"id={mid} layer={layer} tags={tags}"
        elif event == "gc":
            n = entry.get("archived_count", 0)
            dr = " [dry-run]" if entry.get("dry_run") else ""
            detail = f"archived={n}{dr}"
        elif event == "promotion":
            mid = entry.get("memory_id", "")[:16]
            fl = entry.get("from_layer", "")
            tl = entry.get("layer", "")
            detail = f"id={mid} {fl}→{tl}"
        elif event == "hook_post_tool":
            tool = entry.get("tool_name", "")
            detail = f"tool={tool} session={session}"
        else:
            detail = str(entry)[:60]

        click.echo(f"{ts:<22}  {event:<22}  {agent:<10}  {detail}")

    click.echo(f"\n[mnemos] {len(entries)} entries")


@cli.command("stats")
@click.option("--days", default=7, type=int, help="Lookback window in days (default: 7).")
def stats_cmd(days: int) -> None:
    """Show a usage dashboard: captures, searches, top keywords, top memories.

    \b
    Aggregates observability.jsonl and prints:
      - Captures in the last N days (by layer)
      - Hook search activity per day
      - Top 5 most-searched keywords
      - Top 5 most-surfaced memory IDs
      - Last GC timestamp and archived count
      - Total memories per layer (all-time)
    """
    import collections
    from pathlib import Path as _Path

    gw = _get_gateway()
    obs = gw.observability
    stats = obs.aggregate_stats(days=days)

    # Count total memories per layer from the filesystem (live counts, not just log)
    from core.layers import LAYER_STATIC_PATHS
    layer_counts: dict[str, int] = {}
    root = _Path(gw._root)
    for layer, rel_path in LAYER_STATIC_PATHS.items():
        layer_dir = root / rel_path
        if layer_dir.exists():
            layer_counts[layer] = len(list(layer_dir.glob("*.md")))
        else:
            layer_counts[layer] = 0

    click.echo("=" * 60)
    click.echo("  mnemos Usage Dashboard")
    click.echo("=" * 60)

    # -- Memory inventory (live) --
    click.echo("")
    click.echo("MEMORY INVENTORY (current):")
    total_live = 0
    for layer, count in sorted(layer_counts.items()):
        click.echo(f"  {layer:<15} {count:>5} memories")
        total_live += count
    click.echo(f"  {'TOTAL':<15} {total_live:>5} memories")

    # -- Capture activity in window --
    click.echo("")
    click.echo(f"CAPTURES (last {days} days):")
    captures = stats["captures_by_layer"]
    if captures:
        for layer, count in sorted(captures.items(), key=lambda x: -x[1]):
            click.echo(f"  {layer:<15} {count:>5}")
    else:
        click.echo("  (none)")

    # -- Search/hook activity --
    click.echo("")
    click.echo(f"HOOK SEARCH ACTIVITY (last {days} days):")
    spd = stats["searches_per_day"]
    if spd:
        for day in sorted(spd.keys()):
            bar = "#" * min(spd[day], 40)
            click.echo(f"  {day}  {bar} ({spd[day]})")
    else:
        click.echo("  (no hook searches logged)")
    click.echo(f"  Total hook calls: {stats['hook_calls']}")

    # -- Top keywords --
    click.echo("")
    click.echo(f"TOP KEYWORDS (last {days} days):")
    if stats["top_keywords"]:
        for i, (kw, cnt) in enumerate(stats["top_keywords"], 1):
            click.echo(f"  {i}. {kw:<20} {cnt:>4}x")
    else:
        click.echo("  (no keywords tracked yet)")

    # -- Top surfaced memories --
    click.echo("")
    click.echo(f"TOP SURFACED MEMORIES (last {days} days):")
    if stats["top_surfaced_memories"]:
        for i, (mid, cnt) in enumerate(stats["top_surfaced_memories"], 1):
            click.echo(f"  {i}. {mid:<36}  surfaced {cnt}x")
    else:
        click.echo("  (no memories surfaced yet)")

    # -- GC info --
    click.echo("")
    click.echo("GARBAGE COLLECTION:")
    if stats["last_gc_ts"]:
        click.echo(f"  Last GC: {stats['last_gc_ts'][:19]}")
        click.echo(f"  Archived: {stats['last_gc_count']} memories")
    else:
        click.echo("  (no GC recorded)")

    click.echo("")
    click.echo(f"  Observability log: {obs._log_path}")
    click.echo(f"  Total log entries: {stats['total_entries']}")
    click.echo("=" * 60)


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
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Preview what would be ingested without writing to the memory store.",
)
def memory_ingest_claude_md(
    project_root: str | None,
    run_id: str,
    skip_memory_files: bool,
    dry_run: bool,
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
    from core.adapters.claude import ClaudeCodeAdapter

    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    gw = _get_gateway()

    # Wire the ClaudeCode adapter's in-process event handlers so that captures
    # to global/project layers emit a promotion-style notice to stdout.
    adapter = ClaudeCodeAdapter()
    adapter.subscribe_to_event_bus(gw.event_bus)

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
                dry_run=dry_run,
            )
        except PolicyViolationError as exc:
            click.echo(f"error: policy violation — {exc}", err=True)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(1)

        if dry_run:
            for fp in dedup_claude["created"]:
                click.echo(f"[dry-run] would ingest: {fp}")
            for fp in dedup_claude["updated"]:
                click.echo(f"[dry-run] would update: {fp}")
            for fp in dedup_claude["skipped"]:
                click.echo(f"[dry-run] would skip (unchanged): {fp}")
            total_claude = sum(len(v) for v in dedup_claude.values())
            click.echo(
                f"[dry-run] total: {len(dedup_claude['created'])} new, "
                f"{len(dedup_claude['updated'])} updated, "
                f"{len(dedup_claude['skipped'])} skipped"
            )
        else:
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
                dedup = agent.run_scanner_results_dedup(
                    memory_results, run_id=run_id, dry_run=dry_run
                )
            except PolicyViolationError as exc:
                click.echo(f"error: policy violation — {exc}", err=True)
                sys.exit(1)
            except Exception as exc:
                click.echo(f"error: {exc}", err=True)
                sys.exit(1)

            if dry_run:
                for fp in dedup["created"]:
                    click.echo(f"[dry-run] would ingest: {fp}")
                for fp in dedup["updated"]:
                    click.echo(f"[dry-run] would update: {fp}")
                for fp in dedup["skipped"]:
                    click.echo(f"[dry-run] would skip (unchanged): {fp}")
                total = sum(len(v) for v in dedup.values())
                click.echo(
                    f"[dry-run] total: {len(dedup['created'])} new, "
                    f"{len(dedup['updated'])} updated, "
                    f"{len(dedup['skipped'])} skipped"
                )
            else:
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


@cli.group("sync")
def sync_group() -> None:
    """Multi-host Obsidian vault git sync commands.

    Requires ``storage.backend: obsidian`` in ``mnemos.yml`` and
    ``storage.sync.enabled: true`` (or the relevant subcommand handles it).

    \b
    Subcommands:
      mnemos sync pull                              Manual pull
      mnemos sync push                              Manual push
      mnemos sync status                            Show ahead/behind/dirty state
      mnemos sync init --remote <url>               Bootstrap git repo + remote
      mnemos sync continue                          Resume after conflict resolution
    """


def _get_obsidian_backend():
    """Return the active ObsidianBackend or raise SystemExit with an error."""
    from core.config import get_backend_config
    from core.obsidian import ObsidianBackend
    from core.fts import FTSIndex

    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    cfg = get_backend_config(repo_root)
    if cfg.backend != "obsidian" or not cfg.vault_path:
        click.echo(
            "error: mnemos sync requires storage.backend: obsidian in mnemos.yml",
            err=True,
        )
        raise SystemExit(1)
    state_dir = Path(repo_root) / ".agent" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fts = FTSIndex(db_path=str(state_dir / "fts.db"))
    return ObsidianBackend(vault_path=cfg.vault_path, fts=fts, sync_config=cfg.sync)


@sync_group.command("pull")
def sync_pull_cmd() -> None:
    """Pull the latest changes from the remote (manual, bypasses rate limit)."""
    from core.obsidian import SyncConflictError

    backend = _get_obsidian_backend()
    try:
        backend.sync_pull()
        click.echo("[mnemos sync] pull complete")
    except SyncConflictError as exc:
        click.echo(f"error: sync conflict — {exc}", err=True)
        click.echo(
            "Resolve the conflict in your editor, then run: mnemos sync continue",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@sync_group.command("push")
def sync_push_cmd() -> None:
    """Push local commits to the remote."""
    backend = _get_obsidian_backend()
    try:
        backend.sync_push()
        click.echo("[mnemos sync] push complete")
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@sync_group.command("status")
def sync_status_cmd() -> None:
    """Show ahead/behind counts, dirty state, and last pull/push timestamps."""
    backend = _get_obsidian_backend()
    try:
        stat = backend.sync_status()
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"branch:       {stat.get('branch', '?')}")
    click.echo(f"upstream:     {stat.get('upstream') or '(none)'}")
    click.echo(f"ahead:        {stat.get('ahead', 0)}")
    click.echo(f"behind:       {stat.get('behind', 0)}")
    click.echo(f"dirty:        {stat.get('dirty', False)}")
    click.echo(f"untracked:    {stat.get('untracked', 0)}")

    last_pull = stat.get("last_pull_ts", 0.0)
    last_push = stat.get("last_push_ts", 0.0)
    click.echo(f"last_pull_ts: {last_pull:.0f}" + (" (never)" if last_pull == 0.0 else ""))
    click.echo(f"last_push_ts: {last_push:.0f}" + (" (never)" if last_push == 0.0 else ""))
    click.echo(f"sync_enabled: {stat.get('sync_enabled', False)}")
    click.echo(f"sync_remote:  {stat.get('sync_remote', 'origin')}")
    click.echo(f"sync_branch:  {stat.get('sync_branch', 'main')}")


@sync_group.command("init")
@click.option("--remote", "remote_url", required=True, help="Remote git URL (e.g. git@github.com:user/vault.git).")
@click.option("--branch", "branch_name", default="main", help="Branch to track (default: main).")
def sync_init_cmd(remote_url: str, branch_name: str) -> None:
    """Bootstrap: ensure vault is a git repo, add the remote, fetch, set tracking.

    Idempotent — safe to re-run.
    """
    import core.git as _git
    from core.config import get_backend_config

    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    cfg = get_backend_config(repo_root)
    if cfg.backend != "obsidian" or not cfg.vault_path:
        click.echo(
            "error: mnemos sync init requires storage.backend: obsidian in mnemos.yml",
            err=True,
        )
        sys.exit(1)

    vault_path = cfg.vault_path
    remote_name = cfg.sync.remote if cfg.sync.remote else "origin"

    try:
        # 1. git init (idempotent)
        _git.init(vault_path)
        click.echo(f"[mnemos sync] git repo at {vault_path}")

        # 2. Add / update remote
        _git.set_remote(vault_path, remote_name, remote_url)
        click.echo(f"[mnemos sync] remote '{remote_name}' → {remote_url}")

        # 3. Fetch (best-effort — non-fatal)
        try:
            _git.fetch(vault_path, remote_name)
            click.echo(f"[mnemos sync] fetched from {remote_name}")
        except _git.GitCommandError as fetch_exc:
            click.echo(
                f"warning: git fetch failed: {fetch_exc.stderr.strip()}", err=True
            )

        # 4. Set upstream tracking (best-effort — non-fatal)
        try:
            _git.set_upstream(vault_path, remote_name, branch_name)
            click.echo(f"[mnemos sync] tracking {remote_name}/{branch_name}")
        except _git.GitCommandError:
            # Branch may not exist yet on the remote — not fatal
            click.echo(
                f"[mnemos sync] note: could not set upstream tracking "
                f"— push once to create the branch"
            )

        click.echo("[mnemos sync] init complete")
    except _git.GitNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except _git.GitCommandError as exc:
        click.echo(f"error: git error (rc={exc.returncode}): {exc.stderr}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@sync_group.command("continue")
def sync_continue_cmd() -> None:
    """Resume after manually resolving a sync conflict.

    After editing the conflicted files in Obsidian / your editor:
    1. Verify no conflict markers remain.
    2. Run ``git rebase --continue`` internally.
    3. Clean up _sync_conflict.md and transient/conflict-*.md.
    """
    from core.obsidian import SyncConflictError
    import core.git as _git

    backend = _get_obsidian_backend()
    try:
        backend.sync_continue()
        click.echo("[mnemos sync] conflict resolved — rebase complete")
    except SyncConflictError as exc:
        click.echo(f"error: cannot continue — {exc}", err=True)
        sys.exit(1)
    except _git.GitCommandError as exc:
        click.echo(
            f"error: git rebase --continue failed (rc={exc.returncode}): {exc.stderr}",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)


@cli.command("migrate")
@click.option(
    "--from",
    "from_backend",
    required=False,
    default=None,
    type=click.Choice(["default", "obsidian"], case_sensitive=False),
    help="Source backend (default or obsidian). Required unless --uuid-to-slug is set.",
)
@click.option(
    "--to",
    "to_backend",
    required=False,
    default=None,
    type=click.Choice(["default", "obsidian"], case_sensitive=False),
    help="Target backend (default or obsidian). Required unless --uuid-to-slug is set.",
)
@click.option(
    "--vault-path",
    "vault_path",
    required=False,
    default=None,
    help="Path to the Obsidian vault (required for both obsidian source and target).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be migrated/renamed without writing anything.",
)
@click.option(
    "--uuid-to-slug",
    "uuid_to_slug",
    is_flag=True,
    default=False,
    help="Rename UUID-named vault files to human-readable slug names. "
         "Requires --vault-path. --from/--to are ignored when this flag is set.",
)
@click.option(
    "--safe-filenames",
    "safe_filenames",
    is_flag=True,
    default=False,
    help="Rename legacy unsafe default-store filenames to canonical safe names.",
)
@click.option(
    "--commit",
    "do_commit",
    is_flag=True,
    default=False,
    help="Stage and commit renamed files after uuid-to-slug migration.",
)
def memory_migrate(
    from_backend: str | None,
    to_backend: str | None,
    vault_path: str | None,
    dry_run: bool,
    uuid_to_slug: bool,
    safe_filenames: bool,
    do_commit: bool,
) -> None:
    """Migrate memory items between the default backend and an Obsidian vault.

    \b
    Directions:
      mnemos migrate --from default --to obsidian --vault-path ~/vault
      mnemos migrate --from obsidian --to default --vault-path ~/vault

    \b
    UUID-to-slug rename (in-place vault rename):
      mnemos migrate --uuid-to-slug --vault-path ~/vault
      mnemos migrate --uuid-to-slug --vault-path ~/vault --dry-run
      mnemos migrate --uuid-to-slug --vault-path ~/vault --commit

    \b
    Idempotent: items whose (id, content_hash) already match in the target are
    skipped.  Use --dry-run to preview what would be migrated.

    \b
    Multi-host sync is out of scope.  The vault path is whatever you configure;
    iCloud / git-based sync is your responsibility.
    """
    from pathlib import Path as _Path
    from core.store import MemoryStore
    from core.obsidian import ObsidianBackend, OBSIDIAN_LAYERS
    from core.fts import FTSIndex
    from core.layers import LAYER_STATIC_PATHS

    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")

    if safe_filenames:
        store = MemoryStore(repo_root=repo_root)
        changes = store.migrate_unsafe_filenames(dry_run=dry_run)
        action = "would rename" if dry_run else "renamed"
        click.echo(f"[mnemos migrate] {action} {len(changes)} unsafe filename(s)")
        for change in changes:
            click.echo(f"  {change['from']} -> {change['to']}")
        return

    # ── UUID-to-slug mode ─────────────────────────────────────────────────
    if uuid_to_slug:
        if not vault_path:
            raise click.UsageError("--vault-path is required when --uuid-to-slug is set.")
        resolved_vault = str(_Path(vault_path).expanduser().resolve())
        backend = ObsidianBackend(vault_path=resolved_vault)
        stats = backend.rename_uuid_to_slug(dry_run=dry_run, commit=do_commit)

        renamed = stats["renamed"]
        skipped = stats["skipped"]
        renames = stats["renames"]

        if dry_run:
            would_rename = [r for r in renames if r["reason"] != "skipped"]
            would_skip = [r for r in renames if r["reason"] == "skipped"]
            click.echo(f"[DRY RUN] Would rename {renamed} file(s):")
            for r in would_rename:
                click.echo(f"  {r['layer']}/{r['old_name']} → {r['layer']}/{r['new_name']}")
            if would_skip:
                click.echo(f"{len(would_skip)} file(s) would be skipped (already slug-named).")
        else:
            actual_renamed = [r for r in renames if r["reason"] != "skipped"]
            click.echo(f"Renamed {renamed} file(s), skipped {skipped} (already slug-named).")
            for r in actual_renamed:
                click.echo(f"  {r['layer']}/{r['old_name']} → {r['layer']}/{r['new_name']}")
        return

    # ── Validate required options for non-uuid-to-slug modes ─────────────
    if not from_backend:
        raise click.UsageError("--from is required when --uuid-to-slug is not set.")
    if not to_backend:
        raise click.UsageError("--to is required when --uuid-to-slug is not set.")
    if not vault_path:
        raise click.UsageError("--vault-path is required.")

    resolved_vault = str(_Path(vault_path).expanduser().resolve())

    # ── Build source and target backends ──────────────────────────────────
    state_dir = _Path(repo_root) / ".agent" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fts = FTSIndex(db_path=str(state_dir / "fts.db"))

    def make_backend(name: str):
        if name == "obsidian":
            return ObsidianBackend(vault_path=resolved_vault, fts=fts)
        return MemoryStore(repo_root=repo_root)

    src = make_backend(from_backend)
    dst = make_backend(to_backend)

    # ── Determine all layers to iterate ───────────────────────────────────
    all_layers = OBSIDIAN_LAYERS  # covers all known layer names

    # ── Build a (id, content_hash) index of the target ────────────────────
    existing: dict[str, str] = {}  # id → content_hash
    for layer in all_layers:
        try:
            for item in dst.iter_layer_items(layer):
                item_id = item.get("id", "")
                h = item.get("content_hash", "")
                if item_id:
                    existing[item_id] = h
        except Exception:
            pass

    # ── Migrate ───────────────────────────────────────────────────────────
    total_migrated = 0
    total_skipped = 0
    planned: list[tuple[str, str, str]] = []  # (layer, item_id, action)

    for layer in all_layers:
        try:
            items = list(src.iter_layer_items(layer))
        except Exception:
            continue
        for item in items:
            item_id = item.get("id", "")
            if not item_id:
                continue
            content = item.get("content", "")
            existing_hash = existing.get(item_id)

            # Compute source content_hash
            import hashlib, unicodedata, re as _re
            nfkc = unicodedata.normalize("NFKC", content)
            src_hash = hashlib.sha256(
                _re.sub(r"\s+", " ", nfkc.strip()).lower().encode("utf-8")
            ).hexdigest()

            if existing_hash == src_hash:
                total_skipped += 1
                planned.append((layer, item_id, "skip"))
                continue

            planned.append((layer, item_id, "migrate"))
            total_migrated += 1

    if dry_run:
        click.echo(
            f"[dry-run] Would migrate {total_migrated} item(s) "
            f"from {from_backend} → {to_backend} (vault: {resolved_vault})"
        )
        for layer, item_id, action in planned:
            if action == "migrate":
                click.echo(f"  would migrate [{layer}] {item_id}")
        if total_skipped:
            click.echo(f"  {total_skipped} item(s) already up-to-date (would skip)")
        return

    # ── Execute writes ────────────────────────────────────────────────────
    actual_migrated = 0
    actual_skipped = 0
    for layer in all_layers:
        try:
            items = list(src.iter_layer_items(layer))
        except Exception:
            continue
        for item in items:
            item_id = item.get("id", "")
            if not item_id:
                continue
            content = item.get("content", "")

            import hashlib, unicodedata, re as _re
            nfkc = unicodedata.normalize("NFKC", content)
            src_hash = hashlib.sha256(
                _re.sub(r"\s+", " ", nfkc.strip()).lower().encode("utf-8")
            ).hexdigest()

            if existing.get(item_id) == src_hash:
                actual_skipped += 1
                continue

            # Build metadata (strip internal keys)
            meta = {k: v for k, v in item.items() if k not in ("content", "_path")}
            meta.setdefault("id", item_id)
            meta.setdefault("layer", layer)
            try:
                dst.write(
                    layer=layer,
                    item_id=item_id,
                    content=content,
                    metadata=meta,
                )
                actual_migrated += 1
            except Exception as exc:
                click.echo(f"  warning: failed to migrate {item_id}: {exc}", err=True)

    click.echo(
        f"Migrated {actual_migrated} item(s) from {from_backend} → {to_backend}"
    )
    if actual_skipped:
        click.echo(f"Skipped {actual_skipped} already up-to-date item(s)")
