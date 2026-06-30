"""Final-memory distillation — persist derived domains + aggregated policies (#84).

This module is the *persistence / management* layer that issue #67's
read-only cohesion derivation (:mod:`core.cohesion`) lacks. It turns the
ephemeral, on-demand views produced by :func:`core.cohesion.derive_domains`
and :func:`core.cohesion.aggregate_policy_cohesion` into **managed, durable
memory artifacts** with lineage, lifecycle-aware layers, deterministic
idempotent re-builds, and sync/backup safety.

It deliberately reuses three existing seams rather than duplicating logic:

* :func:`core.cohesion.derive_domains` / :func:`core.cohesion.aggregate_policy_cohesion`
  produce the artifact *bodies* (read-only over the item dicts).
* :func:`core.compaction.derive_merged_layer` derives each artifact's layer
  from its sources via :class:`~core.policy.PolicyEngine` (never hard-coded).
* ``gateway.capture(extra_metadata=..., no_classify=True, item_id=...)`` is the
  single persistence seam, so FTS / audit / event-bus all run normally and no
  public API on the gateway / store / policy / cohesion / backup modules changes.

Lineage is **non-destructive and bidirectional** — the deliberate difference
from #81 compaction:

* The artifact carries ``sources: [...]`` plus an ``artifact_kind`` marker
  (``"domain"`` | ``"policy"``).
* Each source gains an APPEND-ONLY ``distilled_into: [...]`` back-pointer.
  Sources are NEVER archived, superseded, or forgotten — domains / policies are
  an *additive* higher layer over the originals (#81 supersedes-and-archives;
  #84 only annotates).

Determinism + idempotency:

* The artifact id is ``uuid5(namespace, "{kind}:{sorted source ids}")`` so the
  same kind over the same source-set always yields the same id and body.
* Re-running is a no-op: :func:`apply_domain_plan` / :func:`apply_policy_plan`
  skip when ``store.read(artifact_id)`` already resolves, so no duplicate
  artifact is written and ``distilled_into`` is never double-appended.

Feedback-loop guard: the planners EXCLUDE items that already carry an
``artifact_kind`` of ``"domain"`` or ``"policy"`` from the source pool, so a
distilled artifact can never become a source of a future re-derivation. (The
``distilled:`` marker-tag namespace is additionally outside
:data:`core.cohesion._POLICY_PREFIXES`, so policy cohesion ignores artifacts.)

The new per-item front-matter is additive YAML — ``backup.SCHEMA_VERSION`` is
NOT bumped and the fields ride through backup/restore + git-sync round-trips
without any reader-side change.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence

from core import cohesion
from core.compaction import _GatewayLike, _PolicyLike, derive_merged_layer


#: Stable namespace for deterministic artifact ids. Generated once and frozen —
#: changing it would change every artifact id, so it is part of the contract.
_DISTILL_NAMESPACE = uuid.UUID("6d6e656d-6f73-8400-a000-646973746c31")

#: artifact_kind discriminators.
KIND_DOMAIN = "domain"
KIND_POLICY = "policy"

#: distillation_method identifiers recorded on each artifact.
DISTILL_METHOD_DOMAIN = "domain-distill-v1"
DISTILL_METHOD_POLICY = "policy-distill-v1"

#: Marker-tag namespace for distilled artifacts. Outside cohesion._POLICY_PREFIXES
#: so re-derivation never treats an artifact as a policy theme.
_DISTILL_TAG_PREFIX = "distilled"

#: Metadata key for the append-only back-pointer written on each source.
DISTILLED_INTO_KEY = "distilled_into"

#: artifact_kind values that must never re-enter the source pool.
_ARTIFACT_KINDS = frozenset({KIND_DOMAIN, KIND_POLICY})


# --------------------------------------------------------------------------- #
# Plan data classes (pure — produced without writing)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class DistillPlan:
    """A single would-be artifact — the pure result of a planner.

    ``artifact_id`` is deterministic, so a plan can be compared / dry-run
    without writing. ``existing`` is set by the planner when the artifact id
    already resolves in the store, so the CLI's review output can flag which
    plans an ``apply`` would skip.
    """

    kind: str
    artifact_id: str
    label: str
    sources: tuple[str, ...]
    layer: str
    content: str
    method: str
    tag: str
    extra_metadata: dict[str, Any]
    existing: bool = False


@dataclasses.dataclass(frozen=True)
class DistillResult:
    """Outcome of applying a single :class:`DistillPlan`."""

    kind: str
    artifact_id: str
    sources: tuple[str, ...]
    layer: str
    applied: bool


@dataclasses.dataclass(frozen=True)
class DueAutoDistillResult:
    """Outcome of draining a due automatic distillation cycle."""

    ran: bool
    counter_before: int
    report: dict[str, dict[str, int]] | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Source-pool collection (read-only; feedback-loop guard lives here)
# --------------------------------------------------------------------------- #
def collect_source_items(
    gateway: _GatewayLike,
    layers: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return live memory items eligible to be distilled.

    Walks every layer (or the named subset) via the store's
    ``iter_layer_items`` — the same collection path the compact CLI uses — and
    EXCLUDES any item already carrying an ``artifact_kind`` of ``"domain"`` or
    ``"policy"``. This is the feedback-loop guard: a distilled artifact can
    never become a source of a later re-derivation.
    """
    from core.layers import LAYER_STATIC_PATHS

    static_layers = list(LAYER_STATIC_PATHS.keys())
    dynamic_layers = ["ephemeral", "working", "session"]
    all_layers = static_layers + dynamic_layers
    if layers:
        all_layers = [layer for layer in all_layers if layer in layers]

    items: list[dict[str, Any]] = []
    for layer in all_layers:
        try:
            layer_items = list(gateway._store.iter_layer_items(layer))
        except Exception:  # pragma: no cover - defensive; iter_layer_items is best-effort
            # iter_layer_items is best-effort — silently skip layers that error.
            continue
        for item in layer_items:
            if item.get("artifact_kind") in _ARTIFACT_KINDS:
                continue
            items.append(item)

    return items


# --------------------------------------------------------------------------- #
# Deterministic id + content
# --------------------------------------------------------------------------- #
def _artifact_id(kind: str, source_ids: Sequence[str]) -> str:
    """Return the deterministic artifact id for *kind* over *source_ids*."""
    key = f"{kind}:{','.join(sorted(source_ids))}"
    return str(uuid.uuid5(_DISTILL_NAMESPACE, key))


def _sources_block(sources: Sequence[dict[str, Any]]) -> list[str]:
    """Return the deterministic ``## Sources`` audit lines (mirrors #81)."""
    lines = ["## Sources", ""]
    for src in sorted(sources, key=lambda s: str(s.get("id", ""))):
        sid = str(src.get("id", "?"))
        layer = str(src.get("layer", "?"))
        created = str(src.get("created_at", "?"))
        lines.append(f"- {sid} (layer={layer}, created_at={created})")
    return lines


def _domain_content(domain: cohesion.Domain, sources: Sequence[dict[str, Any]]) -> str:
    """Return the deterministic markdown body for a domain artifact."""
    lines = [f"# Domain: {domain.label}", ""]
    lines.append(f"- key: {domain.key}")
    lines.append(f"- cohesion_score: {domain.cohesion_score}")
    lines.append(f"- layers: {', '.join(domain.layers) if domain.layers else '(none)'}")
    lines.append(f"- tags: {', '.join(domain.tags) if domain.tags else '(none)'}")
    lines.append("")
    lines.extend(_sources_block(sources))

    return "\n".join(lines) + "\n"


def _policy_content(
    cluster: cohesion.PolicyCohesion, sources: Sequence[dict[str, Any]]
) -> str:
    """Return the deterministic markdown body for a policy artifact."""
    lines = [f"# Policy: {cluster.theme}", ""]
    lines.append(f"- recurrence: {cluster.recurrence}")
    lines.append(f"- layers: {', '.join(cluster.layers) if cluster.layers else '(none)'}")
    lines.append(f"- suggested_layer: {cluster.suggested_layer or '(none)'}")
    lines.append("")
    lines.extend(_sources_block(sources))

    return "\n".join(lines) + "\n"


def _index_items_by_id(items: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a ``{id: item}`` map for the items that carry an id."""
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        iid = item.get("id")
        if iid:
            indexed[str(iid)] = item
    return indexed


def _artifact_exists(gateway: _GatewayLike, artifact_id: str) -> bool:
    """Return ``True`` when *artifact_id* already resolves in the store.

    Uses ``store.read`` (side-effect free) rather than ``gateway.read`` so the
    idempotency probe never bumps access_count or triggers auto-promotion.
    """
    try:
        gateway._store.read(artifact_id)
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Domain planning + apply
# --------------------------------------------------------------------------- #
def compute_domain_plan(
    gateway: _GatewayLike,
    *,
    layers: Sequence[str] | None = None,
) -> list[DistillPlan]:
    """Build would-be domain artifacts WITHOUT writing (pure planner).

    Derives domains over the eligible source pool via
    :func:`core.cohesion.derive_domains`, then for each domain computes the
    deterministic artifact id, layer (via :func:`derive_merged_layer`), body,
    and additive metadata. Solo / id-less domains are skipped — distilling a
    domain with no member ids would produce an empty lineage.
    """
    items = collect_source_items(gateway, layers)
    indexed = _index_items_by_id(items)
    domains = cohesion.derive_domains(items)

    plans: list[DistillPlan] = []
    for domain in domains:
        member_sources = [indexed[mid] for mid in domain.member_ids if mid in indexed]
        if not member_sources:  # pragma: no cover - defensive; domain ids always index
            continue

        source_ids = tuple(str(s.get("id")) for s in member_sources)
        artifact_id = _artifact_id(KIND_DOMAIN, source_ids)
        layer = derive_merged_layer(member_sources, gateway._policy)
        content = _domain_content(domain, member_sources)

        extra_metadata = {
            "artifact_kind": KIND_DOMAIN,
            "distillation_method": DISTILL_METHOD_DOMAIN,
            "sources": sorted(source_ids),
            "cohesion_schema_version": cohesion.SCHEMA_VERSION,
            "domain_key": domain.key,
        }
        plans.append(
            DistillPlan(
                kind=KIND_DOMAIN,
                artifact_id=artifact_id,
                label=domain.label,
                sources=tuple(sorted(source_ids)),
                layer=layer,
                content=content,
                method=DISTILL_METHOD_DOMAIN,
                tag=f"{_DISTILL_TAG_PREFIX}:{KIND_DOMAIN}",
                extra_metadata=extra_metadata,
                existing=_artifact_exists(gateway, artifact_id),
            )
        )

    return plans


def apply_domain_plan(gateway: _GatewayLike, plan: DistillPlan) -> DistillResult:
    """Persist a domain *plan* — idempotent, non-destructive lineage."""
    return _apply_plan(gateway, plan)


# --------------------------------------------------------------------------- #
# Policy planning + apply
# --------------------------------------------------------------------------- #
def compute_policy_plan(
    gateway: _GatewayLike,
    *,
    policy: _PolicyLike | None = None,
    layers: Sequence[str] | None = None,
) -> list[DistillPlan]:
    """Build would-be policy artifacts WITHOUT writing (pure planner).

    Aggregates recurring policy themes over the eligible source pool via
    :func:`core.cohesion.aggregate_policy_cohesion`. The artifact layer is
    derived from the cluster's source items (not from the cohesion-suggested
    layer) so the PolicyEngine promotion chain — not a hard-coded value — is
    the single source of truth, consistent with #81 compaction.
    """
    items = collect_source_items(gateway, layers)
    indexed = _index_items_by_id(items)
    effective_policy = policy if policy is not None else gateway._policy
    clusters = cohesion.aggregate_policy_cohesion(items, effective_policy)

    plans: list[DistillPlan] = []
    for cluster in clusters:
        member_sources = [indexed[mid] for mid in cluster.member_ids if mid in indexed]
        if not member_sources:  # pragma: no cover - defensive; cluster ids always index
            continue

        source_ids = tuple(str(s.get("id")) for s in member_sources)
        artifact_id = _artifact_id(KIND_POLICY, source_ids)
        layer = derive_merged_layer(member_sources, effective_policy)
        content = _policy_content(cluster, member_sources)

        extra_metadata = {
            "artifact_kind": KIND_POLICY,
            "distillation_method": DISTILL_METHOD_POLICY,
            "sources": sorted(source_ids),
            "cohesion_schema_version": cohesion.SCHEMA_VERSION,
            "policy_theme": cluster.theme,
            "recurrence": cluster.recurrence,
            "suggested_layer": cluster.suggested_layer,
        }
        plans.append(
            DistillPlan(
                kind=KIND_POLICY,
                artifact_id=artifact_id,
                label=cluster.theme,
                sources=tuple(sorted(source_ids)),
                layer=layer,
                content=content,
                method=DISTILL_METHOD_POLICY,
                tag=f"{_DISTILL_TAG_PREFIX}:{KIND_POLICY}",
                extra_metadata=extra_metadata,
                existing=_artifact_exists(gateway, artifact_id),
            )
        )

    return plans


def apply_policy_plan(gateway: _GatewayLike, plan: DistillPlan) -> DistillResult:
    """Persist a policy *plan* — idempotent, non-destructive lineage."""
    return _apply_plan(gateway, plan)


# --------------------------------------------------------------------------- #
# Shared apply path
# --------------------------------------------------------------------------- #
def _apply_plan(gateway: _GatewayLike, plan: DistillPlan) -> DistillResult:
    """Write *plan* idempotently and append non-destructive back-links.

    Skip-if-exists: when the deterministic artifact id already resolves in the
    store, nothing is written and no ``distilled_into`` back-link is touched —
    so a second run is a true no-op.
    """
    if _artifact_exists(gateway, plan.artifact_id):
        return DistillResult(
            kind=plan.kind,
            artifact_id=plan.artifact_id,
            sources=plan.sources,
            layer=plan.layer,
            applied=False,
        )

    gateway.capture(
        content=plan.content,
        layer=plan.layer,
        item_id=plan.artifact_id,
        tags=[plan.tag],
        extra_metadata=plan.extra_metadata,
        no_classify=True,
    )

    for source_id in plan.sources:
        _append_distilled_into(gateway, source_id, plan.artifact_id)

    return DistillResult(
        kind=plan.kind,
        artifact_id=plan.artifact_id,
        sources=plan.sources,
        layer=plan.layer,
        applied=True,
    )


def _append_distilled_into(
    gateway: _GatewayLike, source_id: str, artifact_id: str
) -> None:
    """Append *artifact_id* to *source_id*'s ``distilled_into`` list.

    Non-destructive and append-only: the existing list is preserved, the new
    id is added only when absent (dedupe), and the source is never archived,
    superseded, or moved. The path is re-resolved via ``store.read`` so a
    silent auto-promotion move never strands the back-link write.
    """
    try:
        source = gateway._store.read(source_id)
    except FileNotFoundError:
        # The source disappeared (hard-deleted by a hook, etc.). The artifact's
        # own `sources` array remains the authoritative lineage record.
        return

    existing = source.get(DISTILLED_INTO_KEY)
    distilled_into = list(existing) if isinstance(existing, list) else []
    if artifact_id in distilled_into:
        return
    distilled_into.append(artifact_id)

    path = source.get("_path")
    if path:
        gateway._store.update(path, metadata_updates={DISTILLED_INTO_KEY: distilled_into})


# --------------------------------------------------------------------------- #
# Audit navigation
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Automatic distillation (#87) — orchestration + state-file sidecar
# --------------------------------------------------------------------------- #
#: Path to the persistent counter sidecar. Always under the user's HOME so it
#: rides through MNEMOS_REPO_ROOT changes and pytest's ``isolate_home``.
_DISTILL_STATE_RELPATH = ".mnemos/.distill-state.json"

#: Default state-file payload used whenever the sidecar is missing or corrupt.
_DEFAULT_DISTILL_STATE: dict[str, Any] = {
    "captures_since_last_distill": 0,
    "last_distill_at": "",
}


def _state_path() -> Path:
    """Return the absolute path to ``~/.mnemos/.distill-state.json``.

    ``Path.home()`` resolution is delegated, so ``isolate_home`` redirects the
    sidecar to the test's tmp HOME automatically; production callers land at
    the real ``~/.mnemos/`` directory.
    """
    return Path.home() / ".mnemos" / ".distill-state.json"


def _read_distill_state(path: Path) -> dict[str, Any]:
    """Return ``{captures_since_last_distill, last_distill_at}``.

    Tolerant of every failure mode: a missing file, an unreadable file, a
    non-JSON payload, or a JSON payload whose schema doesn't match — all
    collapse to :data:`_DEFAULT_DISTILL_STATE`. The auto-distill subscriber
    relies on this never raising so that ``capture()`` cannot be broken by
    a corrupt sidecar.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_DEFAULT_DISTILL_STATE)

    if not isinstance(data, dict):
        return dict(_DEFAULT_DISTILL_STATE)

    counter_raw = data.get("captures_since_last_distill", 0)
    if isinstance(counter_raw, bool) or not isinstance(counter_raw, int):
        counter = 0
    else:
        counter = counter_raw

    last = data.get("last_distill_at", "")
    if not isinstance(last, str):
        last = ""

    return {
        "captures_since_last_distill": counter,
        "last_distill_at": last,
    }


def _write_distill_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically rewrite the sidecar via tempfile + os.replace.

    The parent directory (``~/.mnemos/``) is created on demand. Writes are
    crash-safe under concurrency: a partial write is never observable by a
    concurrent reader because ``os.replace`` swaps the file atomically. The
    tempfile is cleaned up on ``OSError`` before re-raising.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".distill-state.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(state, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp_path = tmp.name
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _gateway_distill_interval(gateway: _GatewayLike) -> int:
    raw_interval = getattr(gateway, "_distill_interval", 25)
    if isinstance(raw_interval, bool) or not isinstance(raw_interval, int):
        return 25
    if raw_interval <= 0:
        return 25
    return raw_interval


def _log_due_auto_distill(
    gateway: _GatewayLike,
    *,
    trigger: str,
    success: bool,
    interval: int,
    counter_before: int,
    report: dict[str, dict[str, int]] | None = None,
    error: str = "",
) -> None:
    obs = getattr(gateway, "_obs", None)
    if obs is None:
        return

    domains_applied = 0
    policies_applied = 0
    if report:
        domains_applied = report.get("domains", {}).get("applied", 0)
        policies_applied = report.get("policies", {}).get("applied", 0)

    try:
        obs.log_auto_distill(
            success=success,
            error=error,
            trigger=trigger,
            interval=interval,
            counter_before=counter_before,
            domains_applied=domains_applied,
            policies_applied=policies_applied,
        )
    except Exception:  # pragma: no cover - observability is best-effort
        pass


def run_due_auto_distill(
    gateway: _GatewayLike,
    *,
    trigger: str = "background",
) -> DueAutoDistillResult:
    """Run auto-distill only when the durable capture counter is due.

    ``capture()`` owns only the cheap counter bump. This maintenance seam drains
    the expensive planner/apply work later and resets the sidecar only after a
    successful run. Failures are swallowed and leave the due counter intact so a
    later background check can retry.
    """
    state_path = _state_path()
    state = _read_distill_state(state_path)
    counter_before = int(state.get("captures_since_last_distill", 0))
    interval = _gateway_distill_interval(gateway)

    if not getattr(gateway, "_distill_enabled", True):
        return DueAutoDistillResult(ran=False, counter_before=counter_before)
    if counter_before < interval:
        return DueAutoDistillResult(ran=False, counter_before=counter_before)
    if getattr(gateway, "_in_auto_distill", False):
        return DueAutoDistillResult(ran=False, counter_before=counter_before)

    previous_in_auto = getattr(gateway, "_in_auto_distill", False)
    try:
        setattr(gateway, "_in_auto_distill", True)
        report = run_auto_distill(gateway)
    except Exception as exc:  # noqa: BLE001
        state["captures_since_last_distill"] = counter_before
        try:
            _write_distill_state(state_path, state)
        except Exception:  # pragma: no cover - state-file IO failure
            pass
        _log_due_auto_distill(
            gateway,
            trigger=trigger,
            success=False,
            interval=interval,
            counter_before=counter_before,
            error=str(exc),
        )
        return DueAutoDistillResult(
            ran=True,
            counter_before=counter_before,
            error=str(exc),
        )
    finally:
        setattr(gateway, "_in_auto_distill", previous_in_auto)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        _write_distill_state(
            state_path,
            {
                "captures_since_last_distill": 0,
                "last_distill_at": now_iso,
            },
        )
    except Exception:  # pragma: no cover - state-file IO failure
        pass
    _log_due_auto_distill(
        gateway,
        trigger=trigger,
        success=True,
        interval=interval,
        counter_before=counter_before,
        report=report,
    )

    return DueAutoDistillResult(
        ran=True,
        counter_before=counter_before,
        report=report,
    )


def run_auto_distill(gateway: _GatewayLike) -> dict[str, dict[str, int]]:
    """Run domain + policy distill in apply mode and return a small report.

    Walks the existing planner → apply pipeline once for domains and once for
    policies. Per-plan exceptions are swallowed (broad ``Exception``) and
    counted in the ``errors`` field so that one bad plan does not derail the
    rest of the sweep. The returned report shape is:

    ``{"domains":  {"planned", "applied", "skipped", "errors"},
       "policies": {"planned", "applied", "skipped", "errors"}}``

    ``planned`` counts the plans returned by the planner; ``applied`` counts
    plans whose write actually occurred; ``skipped`` counts idempotent
    no-ops (skip-if-exists from #84); ``errors`` counts per-plan exceptions.
    """
    report: dict[str, dict[str, int]] = {
        "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
    }

    # ── Domain pipeline ───────────────────────────────────────────────────
    try:
        domain_plans = compute_domain_plan(gateway)
    except Exception:
        report["domains"]["errors"] += 1
        domain_plans = []

    # Auto-distill skips single-source domain plans: a 1-member domain
    # conveys no aggregation value and would inflate the artifact count of
    # an otherwise quiet store on the first capture. The explicit
    # ``mnemos distill domains apply`` CLI is unaffected — it still produces
    # 1-source artifacts when the operator runs it deliberately.
    domain_plans = [p for p in domain_plans if len(p.sources) >= 2]

    report["domains"]["planned"] = len(domain_plans)
    for plan in domain_plans:
        try:
            result = apply_domain_plan(gateway, plan)
        except Exception:
            report["domains"]["errors"] += 1
            continue
        if result.applied:
            report["domains"]["applied"] += 1
        else:
            report["domains"]["skipped"] += 1

    # ── Policy pipeline ───────────────────────────────────────────────────
    try:
        policy_plans = compute_policy_plan(gateway)
    except Exception:
        report["policies"]["errors"] += 1
        policy_plans = []

    # Same single-source filter as domains, applied to policy plans so an
    # automatic fire never aggregates a 1-source policy theme.
    policy_plans = [p for p in policy_plans if len(p.sources) >= 2]

    report["policies"]["planned"] = len(policy_plans)
    for plan in policy_plans:
        try:
            result = apply_policy_plan(gateway, plan)
        except Exception:
            report["policies"]["errors"] += 1
            continue
        if result.applied:
            report["policies"]["applied"] += 1
        else:
            report["policies"]["skipped"] += 1

    return report


def restore_distilled_source(gateway: _GatewayLike, source_id: str) -> dict[str, Any]:
    """Return the snapshot for *source_id* with its ``distilled_into`` surfaced.

    Mirrors :func:`core.compaction.restore_source`: a thin, side-effect-free
    read (via the store, not the gateway) that documents the audit intent. The
    returned dict always carries a ``distilled_into`` key (an empty list when
    the source has not yet contributed to any distilled artifact).
    """
    item = gateway._store.read(source_id)
    existing = item.get(DISTILLED_INTO_KEY)
    item[DISTILLED_INTO_KEY] = list(existing) if isinstance(existing, list) else []
    return item
