"""Unified inspection-UI payload assembly, HTML rendering, and desktop launcher (issue #83).

This module is the user-facing unification of three existing read-only surfaces:

* the issue-#67/#68 domain-relationship graph (:mod:`core.graphview`),
* the issue-#80 raw-memory inspect surface (:mod:`core.inspectview`),
* a NEW policy-cohesion panel surfacing
  :func:`core.cohesion.aggregate_policy_cohesion`, which until now had no
  CLI/UI surface.

It is **presentation only** — it never mutates the cohesion / policy / gateway /
store models nor any on-disk format (persisting derived domains/policies is the
separate issue #84). Every payload section is produced by REUSING the existing
builders; no graph or memory payload logic is reimplemented here.

The wire format (:func:`build_unified_payload` return value) is::

    {
      "schema_version": 1,
      "generated_at": "<ISO-8601 UTC>",
      "graph": {                       # build_graph_payload output, edge-capped
        ...,
        "relationships": [...],        # capped copy (graphview output untouched)
        "edge_density": {
          "max_edges_per_node": <int>,
          "edge_weight_threshold": <float>,
          "total_edges": <int>,        # post-threshold, pre-cap count
          "shown_edges": <int>,        # retained after the per-node top-N cap
        },
      },
      "memory": { ... },               # build_inspect_payload output
      "policy_cohesion": {"clusters": [ ... ]},  # PolicyCohesion.to_dict() list
    }

Desktop delivery: the unified HTML is hosted in a native OS window via the
OPTIONAL ``pywebview`` extra (``pip install 'mnemos[ui]'``). The launcher
(:func:`launch_app`) is the ONLY pywebview-touching code in the codebase and
imports ``webview`` LAZILY, so ``import core.unifiedview`` and every non-launcher
function work with the base package (no pywebview installed).

Design constraints (mirror :mod:`core.graphview` / :mod:`core.inspectview`):

* Pure functions, no network, no global mutable state (the launcher aside).
* No mutation of the reused builders' outputs — :func:`_cap_edges_by_node`
  operates on a COPY of the relationship list.
* Schema-version pinned to ``1``; bump in lock-step with the template parser.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from core import cohesion, graphview, inspectview
from core.policy import PolicyEngine


# Wire-format schema version this module is compatible with. The HTML template
# parser pins the same value; bump both together when the wire format changes.
_COMPATIBLE_SCHEMA_VERSION: int = 1

# Default character cap on drill-down previews; mirrors graphview / inspectview.
_DEFAULT_PREVIEW_WIDTH: int = 240

# Default per-node incident-edge cap. The real store is a 49-node / ~33k-edge
# hairball; capping each node to its top-8 incident edges keeps the canvas
# readable (<=392 of 33,671 edges retained) out of the box.
_DEFAULT_MAX_EDGES_PER_NODE: int = 8

# Default edge-weight floor. 0.0 keeps every edge (only the per-node top-N cap
# applies); a positive value drops weak edges before the cap.
_DEFAULT_EDGE_WEIGHT_THRESHOLD: float = 0.0

# Single placeholder substituted by :func:`render_html` into ``ui.html``.
_UI_DATA_PLACEHOLDER: str = "__UI_DATA_JSON__"


class PywebviewNotInstalled(RuntimeError):
    """Raised by :func:`launch_app` when the optional ``[ui]`` extra is absent.

    Carries an actionable hint pointing the operator at ``pip install
    'mnemos[ui]'`` or the headless ``--output`` fallback.
    """


def _cap_edges_by_node(
    relationships: list[dict[str, Any]],
    max_edges_per_node: int,
    edge_weight_threshold: float,
) -> tuple[list[dict[str, Any]], int, int]:
    """Return ``(capped_edges, total_edges, shown_edges)`` without mutating input.

    Two-stage reduction over a COPY of *relationships* (the graphview output is
    never mutated):

    1. **Threshold filter** — drop every edge whose ``weight`` is strictly below
       *edge_weight_threshold*. ``total_edges`` counts the survivors of this
       stage (post-threshold, pre-cap).
    2. **Per-node top-N cap** — greedily keep edges in DESCENDING weight order,
       admitting an edge only while NEITHER endpoint has already reached its
       *max_edges_per_node* incident-edge budget. This guarantees every node's
       degree in the result is ``<= max_edges_per_node`` while preferring the
       heaviest edges. ``shown_edges`` counts the admitted edges.

    A non-positive *max_edges_per_node* disables the cap (all post-threshold
    edges are shown). Original input order is preserved in the returned list.
    """
    # Work on a shallow copy of the list; each edge dict is read-only here.
    pool = [dict(edge) for edge in relationships]

    # Stage 1 — weight threshold.
    survivors = [
        edge for edge in pool if float(edge.get("weight", 0.0)) >= edge_weight_threshold
    ]
    total_edges = len(survivors)

    # Stage 2 — per-node top-N cap.
    if max_edges_per_node is None or max_edges_per_node <= 0:
        return survivors, total_edges, total_edges

    # Greedy admission by descending weight. Sort indices (not the edges) by
    # weight so the original list order can be restored at the end; ties keep
    # their original relative order for deterministic output.
    order = sorted(
        range(len(survivors)),
        key=lambda i: float(survivors[i].get("weight", 0.0)),
        reverse=True,
    )

    degree: dict[str, int] = {}
    keep_indices: set[int] = set()
    for i in order:
        edge = survivors[i]
        endpoints = [n for n in (edge.get("source_id"), edge.get("target_id")) if n is not None]
        # Admit only if every endpoint still has budget; a self-loop (single
        # distinct endpoint) is admitted while that node has budget.
        if all(degree.get(n, 0) < max_edges_per_node for n in endpoints):
            keep_indices.add(i)
            for n in endpoints:
                degree[n] = degree.get(n, 0) + 1

    shown = [survivors[idx] for idx in sorted(keep_indices)]
    return shown, total_edges, len(shown)


def build_unified_payload(
    items: list[dict[str, Any]],
    policy_engine: PolicyEngine,
    *,
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
    max_edges_per_node: int = _DEFAULT_MAX_EDGES_PER_NODE,
    edge_weight_threshold: float = _DEFAULT_EDGE_WEIGHT_THRESHOLD,
) -> dict[str, Any]:
    """Assemble the unified wire-format payload by REUSING the existing builders.

    Args:
        items: Persisted memory dicts as yielded by
            :meth:`core.store.MarkdownStore.iter_layer_items` — the same shape
            consumed by :func:`core.inspectview.build_inspect_payload`. The
            cohesion builders read only ``id`` / ``layer`` / ``content`` /
            ``tags`` and tolerate the extra fields.
        policy_engine: An initialised :class:`~core.policy.PolicyEngine`,
            threaded through to both the memory payload and the policy-cohesion
            aggregation (read-only).
        preview_width: Character cap for drill-down previews (ignored when
            *full* is ``True``).
        full: When ``True``, embed full memory content unaltered.
        max_edges_per_node: Per-node incident-edge cap (default 8). ``<= 0``
            disables the cap.
        edge_weight_threshold: Drop edges below this weight before the cap
            (default 0.0 — keep all).

    Returns:
        A JSON-serialisable dict — see the module docstring for the field map.
        Every value is a plain dict/list/scalar (no dataclass instances leak).
    """
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Graph section — reuse graphview verbatim, then attach a capped COPY of the
    # relationships plus an honest edge-density summary. graphview's output is
    # never mutated.
    graph_payload = graphview.build_graph_payload(
        items, preview_width=preview_width, full=full
    )
    capped_edges, total_edges, shown_edges = _cap_edges_by_node(
        graph_payload["relationships"],
        max_edges_per_node,
        edge_weight_threshold,
    )
    graph_section = {
        **graph_payload,
        "relationships": capped_edges,
        "edge_density": {
            "max_edges_per_node": max_edges_per_node,
            "edge_weight_threshold": edge_weight_threshold,
            "total_edges": total_edges,
            "shown_edges": shown_edges,
        },
    }

    # Memory section — reuse inspectview verbatim.
    memory_section = inspectview.build_inspect_payload(
        items, policy_engine, preview_width=preview_width, full=full
    )

    # Policy-cohesion section — reuse cohesion verbatim; serialise the
    # dataclasses to plain dicts so nothing leaks into the wire format.
    clusters = [
        cluster.to_dict()
        for cluster in cohesion.aggregate_policy_cohesion(items, policy_engine)
    ]

    return {
        "schema_version": _COMPATIBLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "graph": graph_section,
        "memory": memory_section,
        "policy_cohesion": {"clusters": clusters},
    }


def render_html(payload: dict[str, Any], template_path: Path | None = None) -> str:
    """Return the full HTML string with *payload* embedded in ``ui.html``.

    The template MUST contain the literal placeholder ``__UI_DATA_JSON__`` inside
    its ``<script id="ui-data" type="application/json">…</script>`` block.
    :func:`render_html` substitutes that placeholder once with
    ``json.dumps(payload, ensure_ascii=False)`` (mirrors
    :func:`core.inspectview.render_html`).

    Args:
        payload: The wire-format dict produced by :func:`build_unified_payload`.
        template_path: Optional override pointing at an alternative HTML
            template. Defaults to ``core/templates/ui.html``.

    Returns:
        The rendered HTML string (UTF-8 safe).
    """
    import json

    if template_path is not None:
        template_text = Path(template_path).read_text(encoding="utf-8")
    else:
        from importlib.resources import files

        template_text = (
            files("core.templates").joinpath("ui.html").read_text("utf-8")
        )

    payload_json = json.dumps(payload, ensure_ascii=False)
    return template_text.replace(_UI_DATA_PLACEHOLDER, payload_json)


def write_unified_html(
    items: list[dict[str, Any]],
    output_path: Path,
    policy_engine: PolicyEngine,
    *,
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
    max_edges_per_node: int = _DEFAULT_MAX_EDGES_PER_NODE,
    edge_weight_threshold: float = _DEFAULT_EDGE_WEIGHT_THRESHOLD,
    template_path: Path | None = None,
) -> Path:
    """Build the payload, render the HTML, and write it to *output_path*.

    This is the headless file writer used by the ``mnemos ui --output`` path; it
    NEVER touches pywebview. Parent directories are created when missing.

    Returns:
        The absolute, resolved path of the written file.
    """
    payload = build_unified_payload(
        items,
        policy_engine,
        preview_width=preview_width,
        full=full,
        max_edges_per_node=max_edges_per_node,
        edge_weight_threshold=edge_weight_threshold,
    )
    html = render_html(payload, template_path=template_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path.resolve()


def launch_app(html: str, *, title: str = "mnemos") -> None:
    """Host *html* in a native pywebview desktop window.

    This is the ONLY pywebview-touching code in the codebase. ``webview`` is
    imported LAZILY inside the function so the base package (no ``[ui]`` extra)
    imports and runs every other function in this module without pywebview
    installed.

    Args:
        html: The rendered unified-UI HTML string.
        title: Native window title.

    Raises:
        PywebviewNotInstalled: When the optional ``[ui]`` extra is not installed
            (``import webview`` raises :class:`ImportError`). The message is
            actionable — it names ``pip install 'mnemos[ui]'`` and the
            ``--output`` headless fallback.
    """
    try:
        import webview
    except ImportError as exc:
        raise PywebviewNotInstalled(
            "pywebview is not installed. Install the UI extra with: "
            "pip install 'mnemos[ui]'  — or render to a file with: "
            "mnemos ui --output ./mnemos-ui.html"
        ) from exc

    webview.create_window(title, html=html)
    webview.start()
