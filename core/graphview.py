"""Domain-relationship graph-view payload assembly and HTML rendering (issue #68).

Consumes :mod:`core.cohesion` (issue #67) — never duplicates the model. The
public surface (:func:`build_graph_payload`, :func:`render_html`,
:func:`write_graph_html`) is the source of truth for the ``mnemos graph`` CLI
subcommand and for the embedded canvas force-directed renderer in
``core/templates/graph.html``.

The wire format (:func:`build_graph_payload` return value) is:

* ``schema_version`` / ``generated_at`` / ``domains`` / ``relationships`` —
  pass-through from :meth:`core.cohesion.DomainGraph.to_dict`.
* ``memories`` — issue-#68-specific drill-down map: every id that appears in
  any ``domain.member_ids`` is keyed to a small ``{id, layer, tags,
  content_preview}`` record.

Design constraints (see ``{TASK_DIR}/context/design-spec.md`` for the full
spec):

* Pure functions, no network, no global mutable state.
* No mutation of :mod:`core.cohesion`.
* Schema-version guard: :data:`core.cohesion.SCHEMA_VERSION` must equal ``1``;
  anything else is a hard ``ValueError``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import cohesion


# Wire-format schema version this module is compatible with. Mirrors
# :data:`core.cohesion.SCHEMA_VERSION`; if cohesion bumps its version, this
# module must be updated in lock-step (the guard below fails loud).
_COMPATIBLE_SCHEMA_VERSION: int = 1

# Default character cap on ``memory.content_preview``. The ``--full`` flag
# bypasses this. Matches ``design-spec.md`` § 6 and the ``mnemos graph
# --preview-width`` default.
_DEFAULT_PREVIEW_WIDTH: int = 240

# Single placeholder substituted by :func:`render_html` into the template.
_GRAPH_DATA_PLACEHOLDER: str = "__GRAPH_DATA_JSON__"


def _truncate_preview(content: str, width: int, full: bool) -> str:
    """Return *content* truncated to *width* chars + ``"..."`` when over.

    Mirrors the behaviour of :func:`core.cli._truncate_content` but kept local
    so :mod:`core.graphview` has no dependency on :mod:`core.cli` (the
    direction of the dependency is the other way around).
    """
    if full:
        return content
    if len(content) <= width:
        return content
    return content[:width] + "..."


def _default_template_path() -> Path:
    """Return the path to the bundled HTML template."""
    return Path(__file__).resolve().parent / "templates" / "graph.html"


def build_graph_payload(
    items: list[dict],
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
) -> dict[str, Any]:
    """Construct the wire-format payload the HTML template consumes.

    Args:
        items: Memory item dicts in the cohesion-expected shape
            (``{"id", "content", "tags", "layer"}``). The CLI projects
            ``MemoryGateway.list_all()`` rows into this shape before calling
            this function.
        preview_width: Character cap for ``content_preview``. Ignored when
            *full* is ``True``.
        full: When ``True``, ``content_preview`` carries the entire content
            verbatim regardless of *preview_width*.

    Returns:
        A JSON-serialisable dict with keys ``schema_version`` /
        ``generated_at`` / ``domains`` / ``relationships`` (pass-through
        from :meth:`DomainGraph.to_dict`) plus ``memories``: a map keyed by
        every id appearing in any ``domain.member_ids`` to a small lookup
        record.

    Raises:
        ValueError: When the underlying :class:`~core.cohesion.DomainGraph`
            carries a ``schema_version`` other than 1.
    """
    graph = cohesion.build_domain_graph(items)
    if graph.schema_version != _COMPATIBLE_SCHEMA_VERSION:
        raise ValueError(
            "schema_version mismatch: expected "
            f"{_COMPATIBLE_SCHEMA_VERSION}, got {graph.schema_version}"
        )

    graph_dict = graph.to_dict()

    # Index items by id once so the memories map can look up content/layer/tags
    # without a quadratic scan.
    item_index: dict[str, dict[str, Any]] = {}
    for it in items:
        item_id = it.get("id")
        if isinstance(item_id, str) and item_id:
            item_index[item_id] = it

    # Union of every member_ids across every domain — drill-down covers exactly
    # the ids the user can click on.
    member_ids: list[str] = []
    seen: set[str] = set()
    for d in graph_dict["domains"]:
        for mid in d.get("member_ids", []):
            if mid not in seen:
                seen.add(mid)
                member_ids.append(mid)

    memories: dict[str, dict[str, Any]] = {}
    for mid in member_ids:
        src = item_index.get(mid, {})
        content = src.get("content", "") or ""
        layer = src.get("layer", "") or ""
        raw_tags = src.get("tags", []) or []
        tags = [t for t in raw_tags if isinstance(t, str)]
        memories[mid] = {
            "id": mid,
            "layer": layer,
            "tags": tags,
            "content_preview": _truncate_preview(content, preview_width, full),
        }

    return {**graph_dict, "memories": memories}


def render_html(
    payload: dict[str, Any],
    template_path: Path | None = None,
) -> str:
    """Return the full HTML string with *payload* embedded in the template.

    The template MUST contain the literal placeholder ``__GRAPH_DATA_JSON__``
    inside the ``<script id="graph-data" type="application/json">…</script>``
    block. :func:`render_html` substitutes that placeholder once with
    ``json.dumps(payload, ensure_ascii=False)``.

    Args:
        payload: The wire-format dict produced by :func:`build_graph_payload`.
        template_path: Optional override pointing at an alternative HTML
            template. Defaults to ``core/templates/graph.html``.

    Returns:
        The rendered HTML string. UTF-8 safe; the caller is expected to
        write it with ``encoding="utf-8"``.
    """
    import json

    path = template_path if template_path is not None else _default_template_path()
    template_text = Path(path).read_text(encoding="utf-8")
    payload_json = json.dumps(payload, ensure_ascii=False)
    return template_text.replace(_GRAPH_DATA_PLACEHOLDER, payload_json)


def write_graph_html(
    items: list[dict],
    output_path: Path,
    *,
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
    template_path: Path | None = None,
) -> Path:
    """Build the payload, render the HTML, and write it to *output_path*.

    Args:
        items: Memory items in the cohesion-expected shape.
        output_path: Destination ``.html`` file. Parent directories are
            created when missing.
        preview_width: Character cap for ``content_preview`` (default 240).
        full: When ``True``, embed full memory content unaltered.
        template_path: Optional override for the HTML template path.

    Returns:
        The absolute, resolved path of the written file.
    """
    payload = build_graph_payload(items, preview_width=preview_width, full=full)
    html = render_html(payload, template_path=template_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path.resolve()
