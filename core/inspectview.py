"""Memory-inspection payload assembly and HTML rendering (issue #80).

Sibling module to :mod:`core.graphview` (issue #68). Where ``graphview`` surfaces
the cohesion-derived domain graph, ``inspectview`` surfaces the *raw* memory
inventory: for every memory item the embedded payload carries the trust,
provenance, and lifecycle metadata the operator needs to answer the six
acceptance criteria from issue #80.

The wire format (:func:`build_inspect_payload` return value) is a single
JSON-serialisable dict with:

* ``schema_version`` / ``generated_at`` — wire stability anchors.
* ``layers`` — the canonical layer chain from ``repo/wiki/policy.yaml``.
  Embedding the array (rather than letting the UI derive it from items)
  guarantees the filter controls render even when zero items occupy a layer.
* ``stages`` — :data:`core.policy.VALID_STAGES` (13 lifecycle values), same
  rationale.
* ``memories`` — one dict per memory item carrying every field needed for the
  six ACs. ``promotable`` + ``next_layer`` are precomputed via
  :meth:`core.policy.PolicyEngine.check_promotion_eligible` so the UI never
  re-derives policy thresholds.

Design constraints (mirrors ``core.graphview`` § module docstring):

* Pure functions, no network, no global mutable state.
* No new schema fields invented for issue #80 — every surfaced field already
  exists in the persisted YAML front-matter or is computed from existing
  fields via the policy engine.
* Schema-version guard: the embedded ``schema_version`` is pinned to ``1``;
  any future wire-format change MUST bump this constant in lock-step with
  the template's parser.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from core import policy


# Wire-format schema version this module is compatible with. The HTML template
# parser pins the same value; bump both together when the wire format changes.
_COMPATIBLE_SCHEMA_VERSION: int = 1

# Default character cap on ``memory.content`` (drill-down preview). The
# ``--full`` flag bypasses this. Mirrors :data:`core.graphview._DEFAULT_PREVIEW_WIDTH`
# so the two CLI surfaces feel identical.
_DEFAULT_PREVIEW_WIDTH: int = 240

# Single placeholder substituted by :func:`render_html` into the template.
_INSPECT_DATA_PLACEHOLDER: str = "__INSPECT_DATA_JSON__"

# Canonical layer order surfaced in the embedded ``layers`` array. Mirrors the
# top-to-bottom promotion chain in ``repo/wiki/policy.yaml`` (transient through
# global). The UI iterates this array to render filter checkboxes even when
# zero items occupy a layer.
_CANONICAL_LAYERS: list[str] = [
    "transient",
    "ephemeral",
    "working",
    "session",
    "project",
    "global",
]


# Visible-character cap on the derived ``display_title``. Unicode code points
# (not bytes) are counted so Korean syllables and emoji each count as 1 — a
# user-facing "~80 visible characters" rather than a UTF-8 byte budget.
_DISPLAY_TITLE_MAX_CHARS: int = 80


# Canonical 8-4-4-4-12 UUID shape. Used by the display-title fallback to tell
# slug-shaped ids (``feat-display-title-85``) from opaque UUID ids
# (``9e6f…-a5b1-…``) — only the former is human-meaningful as a fallback label.
_UUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _derive_display_title(content: str, id_: str) -> str:
    """Return a short, human-readable label for a memory item.

    Derivation rule (issue #85):

    1. Take the FIRST non-empty line of *content*.
    2. Strip leading Markdown heading markers (``#``/``##``/...) and any
       surrounding whitespace.
    3. Collapse internal runs of whitespace to a single space.
    4. Truncate at :data:`_DISPLAY_TITLE_MAX_CHARS` *visible* characters
       (Unicode code points, NOT bytes — works for Korean and emoji).
    5. If *content* yields no meaningful first line, fall back to *id_*:
       use it as-is when it looks like a slug (contains a hyphen and is NOT
       a UUID by shape), otherwise return the raw *id_* string. Always a
       :class:`str` (empty when both inputs are empty).

    Pure function. No I/O, no policy lookups, trivially testable in isolation.
    """
    # ---- step 1: first non-empty line of content -----------------------
    text = content or ""
    first_line = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            first_line = stripped
            break

    if first_line:
        # ---- step 2: strip leading Markdown heading markers ------------
        # ``#``, ``##``, ``###`` ... followed by optional whitespace. Only
        # leading hash runs count as heading syntax — a hash mid-string is
        # left alone.
        cleaned = re.sub(r"^#+\s*", "", first_line).strip()
        if cleaned:
            # ---- step 3: collapse internal whitespace ------------------
            collapsed = re.sub(r"\s+", " ", cleaned)
            # ---- step 4: Unicode-safe truncation -----------------------
            # Slicing a ``str`` is by code point in Python 3, so a simple
            # length compare + slice gives correct visible-character math
            # for Korean syllables and BMP emoji alike.
            if len(collapsed) > _DISPLAY_TITLE_MAX_CHARS:
                return collapsed[:_DISPLAY_TITLE_MAX_CHARS] + "..."
            return collapsed

    # ---- step 5: id-based fallback -------------------------------------
    candidate = (id_ or "").strip()
    if not candidate:
        return ""
    if "-" in candidate and not _UUID_RE.match(candidate):
        # Slug-shaped id (``feat-foo-85``) — readable enough as a label.
        return candidate
    # Raw id (UUID, or single token with no hyphen). No prettier option.
    return candidate


def _truncate_preview(content: str, width: int, full: bool) -> tuple[str, bool]:
    """Return ``(maybe_truncated_content, truncated_flag)``.

    Mirrors the behaviour of :func:`core.graphview._truncate_preview` but also
    returns whether truncation occurred so the payload can carry an explicit
    ``preview_truncated`` boolean. The HTML uses that flag to label clipped
    previews without having to compare lengths client-side.
    """
    if full:
        return content, False
    if len(content) <= width:
        return content, False
    return content[:width] + "...", True


def _project_item(
    item: dict[str, Any],
    policy_engine: policy.PolicyEngine,
    preview_width: int,
    full: bool,
) -> dict[str, Any]:
    """Project a single persisted memory item into its wire-format record.

    Field order is fixed (matches the PRD payload contract) so the embedded
    JSON is deterministic across runs — useful both for the test suite's
    field-presence assertions and for any diff-based audit tooling.
    """
    raw_content = item.get("content", "") or ""
    content, truncated = _truncate_preview(raw_content, preview_width, full)

    layer = item.get("layer", "") or ""
    stage = item.get("stage", "") or ""

    # promotable / next_layer reuse the PolicyEngine — no threshold
    # duplication. When the layer is unknown to policy.yaml the engine
    # returns False; the unknown-layer branch propagates that as
    # ``promotable=False, next_layer=None`` without touching get_next_layer
    # (which would raise on an unknown layer).
    promotable = bool(policy_engine.check_promotion_eligible(item))
    next_layer = policy_engine.get_next_layer(layer) if promotable else None

    raw_tags = item.get("tags", []) or []
    tags = [t for t in raw_tags if isinstance(t, str)]

    item_id = item.get("id") or ""
    if not item_id:
        raw_path = item.get("_path", "")
        if raw_path:
            item_id = Path(raw_path).stem

    # display_title is derived from the RAW content (not the truncated
    # preview) so the heading remains stable regardless of preview_width.
    display_title = _derive_display_title(raw_content, item_id)

    return {
        "id": item_id,
        "display_title": display_title,
        "layer": layer,
        "stage": stage,
        "tags": tags,
        "content": content,
        "preview_truncated": truncated,
        "quality_score": item.get("quality_score"),
        "access_count": item.get("access_count", 0),
        "run_id": item.get("run_id"),
        "session_id": item.get("session_id"),
        "created_at": item.get("created_at"),
        "content_hash": item.get("content_hash"),
        "path": item.get("_path", ""),
        "promotable": promotable,
        "next_layer": next_layer,
        "archived": stage == "archived",
    }


def build_inspect_payload(
    items: list[dict[str, Any]],
    policy_engine: policy.PolicyEngine,
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
) -> dict[str, Any]:
    """Construct the wire-format payload the inspect template consumes.

    Args:
        items: Persisted memory dicts as yielded by
            :meth:`core.store.MarkdownStore.iter_layer_items` — every YAML
            front-matter key is preserved, plus ``content`` and ``_path``.
        policy_engine: An initialised :class:`~core.policy.PolicyEngine`. The
            CLI threads its existing instance through so we never re-read
            ``policy.yaml`` from disk on every render.
        preview_width: Character cap for ``memory.content``. Ignored when
            *full* is ``True``.
        full: When ``True``, ``memory.content`` carries the entire content
            verbatim regardless of *preview_width*.

    Returns:
        A JSON-serialisable dict — see module docstring for the field list.
    """
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    memories = [
        _project_item(item, policy_engine, preview_width, full) for item in items
    ]

    return {
        "schema_version": _COMPATIBLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "layers": list(_CANONICAL_LAYERS),
        "stages": list(policy.VALID_STAGES),
        "memories": memories,
    }


def render_html(
    payload: dict[str, Any],
    template_path: Path | None = None,
) -> str:
    """Return the full HTML string with *payload* embedded in the template.

    The template MUST contain the literal placeholder ``__INSPECT_DATA_JSON__``
    inside a ``<script id="inspect-data" type="application/json">…</script>``
    block. :func:`render_html` substitutes that placeholder once with
    ``json.dumps(payload, ensure_ascii=False)``.

    Args:
        payload: The wire-format dict produced by :func:`build_inspect_payload`.
        template_path: Optional override pointing at an alternative HTML
            template. Defaults to ``core/templates/inspect.html``.

    Returns:
        The rendered HTML string. UTF-8 safe; the caller is expected to
        write it with ``encoding="utf-8"``.
    """
    import json

    if template_path is not None:
        template_text = Path(template_path).read_text(encoding="utf-8")
    else:
        from importlib.resources import files

        template_text = (
            files("core.templates").joinpath("inspect.html").read_text("utf-8")
        )

    payload_json = json.dumps(payload, ensure_ascii=False)
    return template_text.replace(_INSPECT_DATA_PLACEHOLDER, payload_json)


def write_inspect_html(
    items: list[dict[str, Any]],
    output_path: Path,
    policy_engine: policy.PolicyEngine,
    *,
    preview_width: int = _DEFAULT_PREVIEW_WIDTH,
    full: bool = False,
    template_path: Path | None = None,
) -> Path:
    """Build the payload, render the HTML, and write it to *output_path*.

    Args:
        items: Persisted memory dicts (see :func:`build_inspect_payload`).
        output_path: Destination ``.html`` file. Parent directories are
            created when missing.
        policy_engine: Initialised :class:`~core.policy.PolicyEngine`.
        preview_width: Character cap for ``memory.content`` (default 240).
        full: When ``True``, embed full memory content unaltered.
        template_path: Optional override for the HTML template path.

    Returns:
        The absolute, resolved path of the written file.
    """
    payload = build_inspect_payload(
        items,
        policy_engine,
        preview_width=preview_width,
        full=full,
    )
    html = render_html(payload, template_path=template_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path.resolve()
