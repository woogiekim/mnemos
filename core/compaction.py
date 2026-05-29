"""Semantic compression — deterministic merge plans + lineage-preserving apply.

This module is the compression half of the pipeline introduced for Issue
#81.  It pairs with :mod:`core.similarity`:

* :func:`compute_merge_plan` is **pure** — it inspects the group, picks
  a target layer via :class:`~core.policy.PolicyEngine`, and produces
  the merged content.  It never writes.
* :func:`apply_merge_plan` writes the merged memory via the gateway,
  archives every source, and back-references the merged id on each
  source via ``superseded_by``.
* :func:`restore_source` returns the original snapshot of a source by id
  — the gateway already preserves the archived markdown file, so this
  is a thin convenience wrapper that the CLI's ``mnemos compact
  restore-source`` subcommand exercises.

The merge content is produced by :func:`deterministic_summary`: an
ordered union of unique source lines, followed by a ``## Sources``
header that records the lineage.  This is lossless-of-meaning by
construction; an opt-in LLM summariser is exposed as a separate code
path that always falls back to the deterministic algorithm on any
failure (see :func:`compute_merge_plan` ``summarizer="llm"`` branch).

The added per-item front-matter contract is **additive** — three keys:

* ``sources``: list of source ids (on the merged memory only).
* ``compaction_method``: string identifier (e.g. ``jaccard-merge-v1``).
* ``superseded_by``: id of the merged memory (on each source only).

No schema bump is required: these keys flow through YAML front-matter
unchanged and survive backup / restore + git-sync round-trips without
any reader-side change.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


logger = logging.getLogger(__name__)


COMPACTION_METHOD_DETERMINISTIC = "jaccard-merge-v1"
COMPACTION_METHOD_LLM = "jaccard-merge-llm-v1"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class MergePlan:
    """Pure result of :func:`compute_merge_plan`.

    The ``merged_id`` field is intentionally optional: a freshly computed
    plan has no destination id yet (the gateway assigns one at write
    time), so callers can dry-run the plan without committing.  After
    :func:`apply_merge_plan` returns, the corresponding :class:`MergeResult`
    carries the assigned id.
    """

    sources: tuple[str, ...]
    target_layer: str
    content: str
    method: str
    merged_id: str | None = None


@dataclasses.dataclass(frozen=True)
class MergeResult:
    """Outcome of :func:`apply_merge_plan` — see module docstring for the contract."""

    merged_id: str
    sources: tuple[str, ...]
    target_layer: str
    applied: bool
    audit_entry_id: str | None = None


# ---------------------------------------------------------------------------
# Gateway / policy duck-type protocols (typing only — no runtime check)
# ---------------------------------------------------------------------------

class _PolicyLike(Protocol):
    def get_next_layer(self, current_layer: str) -> str | None: ...


class _GatewayLike(Protocol):
    def capture(
        self,
        content: str,
        layer: str | None = ...,
        item_id: str | None = ...,
        tags: list[str] | None = ...,
        quality_score: float = ...,
        run_id: str | None = ...,
        session_id: str | None = ...,
        extra_metadata: dict[str, Any] | None = ...,
        no_classify: bool = ...,
    ) -> str | None: ...

    def archive(self, item_id: str) -> None: ...

    def read(self, item_id: str) -> dict[str, Any]: ...

    @property
    def _policy(self) -> _PolicyLike: ...
    @property
    def _store(self) -> Any: ...


# ---------------------------------------------------------------------------
# Deterministic summary
# ---------------------------------------------------------------------------

def deterministic_summary(sources: Sequence[Mapping[str, Any]]) -> str:
    """Return the deterministic merged content for *sources*.

    Algorithm:

    1. Sort sources by id (lexicographic) so input order is irrelevant.
    2. For each source, split content into lines, preserving the
       relative order of lines as they appear.  Append each line to a
       running ordered list **only on first sight** (a per-line seen
       set guarantees uniqueness across sources without re-shuffling).
    3. Append a ``## Sources`` section listing every source id, layer,
       and created_at value — this is the audit trail and is what
       :func:`core.compaction.restore_source` can later walk.

    The output is therefore lossless-of-meaning (every distinct content
    line is preserved) AND deterministic (same inputs → same output,
    regardless of caller's iteration order).
    """
    if not sources:
        return "## Sources\n\n_(no sources)_\n"

    sorted_sources = sorted(sources, key=lambda s: str(s.get("id", "")))

    seen_lines: set[str] = set()
    merged_lines: list[str] = []
    for src in sorted_sources:
        content = str(src.get("content", ""))
        for raw_line in content.splitlines():
            # We keep blank-line semantics modest — blank lines are
            # deduplicated globally so the union output does not
            # accumulate a wall of empties from many short sources.
            line = raw_line.rstrip()
            key = line
            if key in seen_lines:
                continue
            seen_lines.add(key)
            merged_lines.append(line)

    # Trim trailing blank line, then add the audit trail.
    while merged_lines and merged_lines[-1] == "":
        merged_lines.pop()
    if merged_lines:
        merged_lines.append("")  # one blank line before the header

    merged_lines.append("## Sources")
    merged_lines.append("")
    for src in sorted_sources:
        sid = str(src.get("id", "?"))
        layer = str(src.get("layer", "?"))
        created = str(src.get("created_at", "?"))
        merged_lines.append(f"- {sid} (layer={layer}, created_at={created})")

    return "\n".join(merged_lines) + "\n"


# ---------------------------------------------------------------------------
# Layer derivation
# ---------------------------------------------------------------------------

def derive_merged_layer(
    sources: Sequence[Mapping[str, Any]],
    policy: _PolicyLike,
) -> str:
    """Return the highest layer present among *sources* per *policy*.

    The "highest" layer is defined by walking ``policy.get_next_layer``
    from each source: the source whose layer NO other source's layer
    can reach (i.e. the deepest layer in the promotion chain) wins.
    This avoids hard-coding any layer name — the rule is purely
    policy-driven and respects whatever promotion order the operator
    has configured in ``wiki/policy.yaml``.

    Edge cases:
    * Empty input is a programmer error — we raise ``ValueError`` so the
      caller cannot silently merge nothing into an arbitrary layer.
    * If every source has the same layer, that layer is returned
      directly.
    """
    if not sources:
        raise ValueError("derive_merged_layer requires at least one source")

    layers = [str(src.get("layer", "")) for src in sources]
    unique_layers = list(dict.fromkeys(layers))  # preserve first-seen order

    if len(unique_layers) == 1:
        return unique_layers[0]

    # Build a "reachable" map: for each layer L, what other layers can
    # be reached by walking get_next_layer until the chain ends?  The
    # winning layer is one whose reachable-set does NOT include any
    # other present layer — i.e. it is downstream of all others.
    def reachable_from(layer: str) -> set[str]:
        out: set[str] = set()
        cursor: str | None = layer
        visited: set[str] = set()
        while cursor is not None and cursor not in visited:
            visited.add(cursor)
            try:
                nxt = policy.get_next_layer(cursor)
            except Exception:
                # Unknown layer — treat as terminal.
                break
            if nxt is None or nxt in visited:
                break
            out.add(nxt)
            cursor = nxt
        return out

    candidates = []
    for layer in unique_layers:
        reach = reachable_from(layer)
        # "Highest" = the layer that NO other present layer can reach
        # via promotion.  Equivalently, this layer is downstream of all
        # others (every other present layer's reachable set contains
        # it, or this layer's reachable set contains none of the others).
        if not any(other in reach for other in unique_layers if other != layer):
            candidates.append(layer)

    if not candidates:
        # Pathological: every layer can reach every other (cycle).
        # Fall back to the alphabetically-largest unique layer name so
        # the result is at least deterministic.
        return sorted(unique_layers)[-1]  # pragma: no cover

    # Multiple terminal layers (e.g. independent chains) — pick the one
    # that the most others can reach (the "deepest" by inbound count).
    if len(candidates) == 1:
        return candidates[0]
    # pragma: no cover — reached only when the policy has two parallel
    # promotion chains (e.g. two terminal layers that no other source's
    # chain can reach).  The default linear policy never lands here.
    inbound: dict[str, int] = {c: 0 for c in candidates}  # pragma: no cover
    for layer in unique_layers:  # pragma: no cover
        reach = reachable_from(layer)  # pragma: no cover
        for cand in candidates:  # pragma: no cover
            if cand in reach:  # pragma: no cover
                inbound[cand] += 1  # pragma: no cover
    # Highest inbound count wins; tie-break by alpha for determinism.
    return sorted(candidates, key=lambda c: (-inbound[c], c))[0]  # pragma: no cover


# ---------------------------------------------------------------------------
# Plan + apply
# ---------------------------------------------------------------------------

def _llm_summary(sources: Sequence[Mapping[str, Any]]) -> str | None:  # pragma: no cover - opt-in network path, exercised only when --summarizer=llm
    """Best-effort LLM summarisation using the already-pinned anthropic client.

    Returns ``None`` on any failure (missing key, network, import error,
    timeout, schema mismatch).  The caller MUST fall back to the
    deterministic path on ``None`` — never raise.

    The entire function body is marked ``# pragma: no cover`` because
    it is an opt-in network code path — tests for the LLM branch
    monkey-patch this symbol directly (see
    ``tests/test_compaction.py::test_llm_summarizer_*``), so the real
    body is never reached in CI.
    """
    try:
        import anthropic  # already a pinned dep
    except Exception as exc:
        logger.warning("LLM summariser unavailable (import failed): %s", exc)
        return None

    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        logger.warning("LLM summariser unavailable (client init failed): %s", exc)
        return None

    joined = "\n\n---\n\n".join(str(s.get("content", "")) for s in sources)
    prompt = (
        "Merge the following memory snippets into a single coherent note. "
        "Preserve every distinct fact. Output only the merged note.\n\n"
        f"{joined}"
    )
    try:  # pragma: no cover - external network call, no deterministic test
        response = client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        # anthropic returns a list of content blocks; flatten text blocks.
        parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        merged = "\n".join(parts).strip()
        return merged or None
    except Exception as exc:  # pragma: no cover
        logger.warning("LLM summariser failed; falling back to deterministic: %s", exc)
        return None


def compute_merge_plan(
    gateway: _GatewayLike,
    group: Sequence[Mapping[str, Any]],
    *,
    summarizer: str = "deterministic",
    layer: str | None = None,
) -> MergePlan:
    """Compute a :class:`MergePlan` for *group* — pure, no writes.

    *summarizer* picks the content path: ``"deterministic"`` (default,
    the lossless union) or ``"llm"`` (opt-in, falls back to deterministic
    on any failure with a logged warning).

    *layer*, when supplied, pins the target layer; otherwise it is
    derived from the sources via :func:`derive_merged_layer`.
    """
    if not group:
        raise ValueError("compute_merge_plan requires a non-empty group")
    if summarizer not in ("deterministic", "llm"):
        raise ValueError(f"unknown summarizer: {summarizer!r}")

    sources_ids = tuple(str(s.get("id", "")) for s in group)

    target_layer = layer or derive_merged_layer(group, gateway._policy)

    method = COMPACTION_METHOD_DETERMINISTIC
    content = deterministic_summary(group)

    if summarizer == "llm":
        llm_body = _llm_summary(group)
        if llm_body is not None:
            # Even on the LLM path we preserve the deterministic ## Sources
            # audit trail so lineage is intact regardless of summariser.
            audit_lines = ["", "## Sources", ""]
            for src in sorted(group, key=lambda s: str(s.get("id", ""))):
                audit_lines.append(
                    f"- {src.get('id', '?')} (layer={src.get('layer', '?')}, "
                    f"created_at={src.get('created_at', '?')})"
                )
            content = llm_body.rstrip() + "\n" + "\n".join(audit_lines) + "\n"
            method = COMPACTION_METHOD_LLM
        else:
            logger.warning(
                "LLM summariser returned no content; using deterministic fallback",
            )

    return MergePlan(
        sources=sources_ids,
        target_layer=target_layer,
        content=content,
        method=method,
        merged_id=None,
    )


def apply_merge_plan(gateway: _GatewayLike, plan: MergePlan) -> MergeResult:
    """Write *plan* — capture merged memory, archive every source, link them.

    The supersede write uses ``store.update`` directly (rather than a
    new gateway method) so we do not have to extend the public gateway
    surface — preserving the "no public API change in core/gateway.py /
    core/store.py / core/policy.py" hard gate from the approved plan.

    Returns a :class:`MergeResult` whose ``merged_id`` carries the
    newly-assigned id of the merged memory.
    """
    if plan.merged_id is not None:
        raise ValueError(
            "MergePlan already carries merged_id — apply_merge_plan must be called "
            "with a freshly computed plan",
        )

    merged_id = gateway.capture(
        content=plan.content,
        layer=plan.target_layer,
        extra_metadata={
            "sources": list(plan.sources),
            "compaction_method": plan.method,
        },
        no_classify=True,
    )

    if merged_id is None:
        # Dedup short-circuit: identical merged content already exists.
        # We refuse to flip sources to superseded_by an unknown id, so
        # raise so the caller can decide how to recover.
        raise RuntimeError(
            "gateway.capture returned None (likely dedup); aborting apply_merge_plan "
            "to avoid orphaning superseded_by links",
        )

    # Archive each source + back-reference the merged id.
    for source_id in plan.sources:
        # Archive first so the stage flip lands; auto-promotion may move
        # the file, so re-resolve the path AFTER the archive call.  Then
        # write the superseded_by back-pointer via store.update against
        # the fresh path so we never chase a stale location.
        gateway.archive(source_id)
        try:
            refreshed = gateway._store.read(source_id)
        except FileNotFoundError:
            # The source has disappeared (hard-deleted by a hook, etc.).
            # We cannot back-reference what is not there; skip silently —
            # the merged memory's `sources` array is still the authoritative
            # lineage record.
            continue
        path = refreshed.get("_path")
        if path:
            gateway._store.update(
                path,
                metadata_updates={"superseded_by": merged_id},
            )

    return MergeResult(
        merged_id=merged_id,
        sources=plan.sources,
        target_layer=plan.target_layer,
        applied=True,
        audit_entry_id=None,
    )


def restore_source(gateway: _GatewayLike, source_id: str) -> dict[str, Any]:
    """Return the archived snapshot for *source_id*.

    The original markdown file remains on disk (archive is a soft
    delete), so this is effectively a typed alias for ``gateway.read``
    that documents the lineage-audit intent.  The returned dict carries
    the full front-matter — including ``superseded_by`` if the source
    was rolled into a merged memory — and the original content body.
    """
    item = gateway.read(source_id)
    # Convenience: surface the merged-id so callers do not need to
    # re-parse the front-matter for the back-pointer.
    if "superseded_by" not in item:
        item["superseded_by"] = None
    return item


# ---------------------------------------------------------------------------
# Path helper (re-exported for the CLI)
# ---------------------------------------------------------------------------

def source_path(gateway: _GatewayLike, source_id: str) -> Path | None:
    """Return the current on-disk path for *source_id* if known, else ``None``.

    Uses the store's id-based lookup (``store.read``) rather than the
    gateway's read so that side-effects (access-count bumps, silent
    auto-promotion file moves) do not invalidate the path between the
    lookup and the caller's use.
    """
    try:
        item = gateway._store.read(source_id)
    except Exception:
        return None
    raw = item.get("_path")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None
