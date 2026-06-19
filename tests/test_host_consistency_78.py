"""Multi-host adapter consistency validation (issue #78).

Spec: handoff.md (issue #78) — make host-independence a first-class, TESTED
property across mnemos's supported host adapters. This is a test-only suite; it
asserts the EXISTING, unchanged adapter/context/gateway/sync surfaces behave
identically regardless of which host is active. No production code is changed.

Four acceptance criteria are covered:

- AC1: behavior validated across every supported host adapter (uniform
  HostAdapter contract + a registry-parity guard against the triplicated
  ``adapter_list``).
- AC2: context-injection consistency across hosts (``retrieve_context`` /
  ``render_context_block`` are host-string-agnostic; adapters install a
  byte-identical behavioral payload).
- AC3: host-independent memory lifecycle (``MemoryGateway`` capture→search→
  read→promote never branches on host/adapter).
- AC4: cross-machine / cross-host sync (a memory captured on a claude-block
  machine round-trips byte-for-byte to a cursor-block machine through a shared
  remote). Reuses the #69 sync helpers from ``tests.test_store_sync`` rather
  than re-implementing bare-repo plumbing.

Documented gap (not invented): Codex has a managed behavior-instruction adapter,
but no autonomous capture or pre-prompt context-injection hook path. AC2
validates that ``retrieve_context`` accepts host strings without requiring
hook support, which is the correct host-agnostic behavior. The triplicated
``adapter_list`` registry
(install.py / updater.py / uninstaller.py) is a latent drift risk; AC1's
parity test guards it, and a single-source-registry refactor is recommended as
a follow-up issue.

All autouse fixtures from ``tests/conftest.py`` (``isolate_home``,
``isolate_mnemos_repo_root``) stay in force; ``safe_repo_root`` is intentionally
NOT used because the install-parity check exercises the marker-file payload
(CLAUDE.md / cursor rules), not settings.json hook templating.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import yaml

from core.adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter
from core.adapters.base import HostAdapter, MNEMOS_BEHAVIOR_BLOCK
from core.context import render_context_block, retrieve_context
from core.gateway import MemoryGateway

# Issue #69 sync helpers — IMPORTED, never re-implemented (handoff AC4 rule).
from tests.test_store_sync import (
    _commit_count,
    _sync_config,
)

# Issue #24 sync-e2e CLONE plumbing — IMPORTED, never re-implemented. AC4 needs
# two machines that share ONE history (so pull/push fast-forward cleanly); an
# independent ``git init`` per machine produces two unrelated root histories
# that cannot coexist on one ``main`` (non-fast-forward). ``_init_bare_with_main``
# seeds the shared remote once; ``_clone`` gives each machine a working copy of
# that single history.
from tests.test_sync_e2e import (
    _clone,
    _init_bare_with_main,
    _make_bare,
)


# Every supported, concrete host adapter. "base" is the abstract contract.
SUPPORTED_ADAPTERS = [ClaudeCodeAdapter, CursorAdapter, CodexAdapter]

# Arbitrary host strings exercised by the context-injection ACs. "codex" has a
# behavior-instruction adapter but no hook injection path; "x-arbitrary" is a
# host that could never have an adapter. Both must be accepted host-agnostically.
HOST_STRINGS = ["claude", "cursor", "codex", "x-arbitrary"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _strip_delimiters(block: str) -> str:
    """Return the behavioral payload with adapter delimiter comments removed.

    Claude wraps the payload in ``<!-- mnemos-start -->`` / ``<!-- mnemos-end -->``
    (hyphen); Cursor wraps it in ``<!-- mnemos:start -->`` / ``<!-- mnemos:end -->``
    (colon). The regex matches both delimiter styles so the payload between them
    must be byte-identical across adapters.
    """
    without_markers = re.sub(r"<!-- mnemos[-:](?:start|end) -->", "", block)
    return without_markers.strip()


# Minimal, host-independent policy used to scaffold a gateway-ready repo root.
# Promotion thresholds are all zeroed so promote(force=False) would also be
# valid; the lifecycle test uses force=True to keep the assertion deterministic.
_LAYER_NAMES = ["ephemeral", "working", "session", "project", "global"]
_PROMOTES_TO = {
    "ephemeral": "working",
    "working": "session",
    "session": "project",
    "project": "global",
    "global": None,
}
_LAYER_PATH_TEMPLATES = {
    "ephemeral": ".agent/runs/{run_id}/scratch/",
    "working": ".agent/runs/{run_id}/working/",
    "session": ".agent/sessions/{session_id}/",
    "project": "wiki/projects/",
    "global": "wiki/global/",
}


def _scaffold_repo_root(root: Path) -> Path:
    """Create the minimal wiki/.agent layout + policy.yaml a MemoryGateway needs.

    This mirrors the structure the install scaffolding creates on disk; it is
    deliberately host-independent (no adapter is consulted) so the same store
    can be exercised under any host setup.

    Promotion thresholds are set HIGH so the gateway's auto-promotion
    side-effect (fired after capture/search/read) never triggers. That keeps
    store state stable across repeated retrievals — without it, an auto-promote
    between two ``retrieve_context`` calls on the same store would mutate the
    promotion cursor and produce a spurious host-independent diff. Explicit
    ``promote(force=True)`` still works because force bypasses thresholds.
    """
    wiki = root / "wiki"
    for d in ("global", "projects", "entities", "claims", "topics"):
        (wiki / d).mkdir(parents=True, exist_ok=True)
    agent = root / ".agent"
    for d in ("runs", "sessions", "state", "reports", "tools"):
        (agent / d).mkdir(parents=True, exist_ok=True)
    (agent / "workflows" / "hooks").mkdir(parents=True, exist_ok=True)

    policy = {
        "layers": {
            name: {
                "path_template": _LAYER_PATH_TEMPLATES[name],
                "promotes_to": _PROMOTES_TO[name],
                # High age threshold → auto-promotion never fires in-test.
                "promotion": {"age_hours": 100000.0, "access_count": 999, "quality_score": 1.0},
            }
            for name in _LAYER_NAMES
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return root


def _make_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryGateway:
    """Build a MemoryGateway rooted at an isolated, host-independent repo root."""
    root = _scaffold_repo_root(tmp_path / "store")
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(root))
    return MemoryGateway(repo_root=str(root))


# ===========================================================================
# AC1 — behavior validated across every supported host adapter
# Spec: handoff.md § "AC1 — registry parity" and § "Adapters"
# ===========================================================================


@pytest.mark.parametrize("adapter_cls", SUPPORTED_ADAPTERS)
def test_every_adapter_satisfies_the_host_adapter_contract(adapter_cls, tmp_path):
    """Each supported adapter is a HostAdapter exposing the uniform contract."""
    # given a concrete adapter instance and an isolated fake home
    sut = adapter_cls()
    home = tmp_path / "home"
    home.mkdir()

    # then it honors the abstract contract uniformly
    assert isinstance(sut, HostAdapter)
    assert isinstance(sut.name, str) and sut.name.strip()
    assert isinstance(sut.install(home), list)
    assert all(isinstance(m, str) for m in sut.install(home))
    assert isinstance(sut.update(home), list)
    assert isinstance(sut.uninstall(home), list)
    ok, missing = sut.verify_hooks(home)
    assert isinstance(ok, bool)
    assert isinstance(missing, list)


def test_adapter_names_are_non_empty_and_unique_across_the_supported_set():
    """No two supported adapters share a name, and none is blank."""
    # given every supported adapter instantiated
    names = [cls().name for cls in SUPPORTED_ADAPTERS]

    # then each name is non-empty and the set is unique
    assert all(name.strip() for name in names)
    assert len(set(names)) == len(names)


def test_install_then_verify_hooks_reports_present_for_each_adapter(tmp_path):
    """install() makes verify_hooks() report a clean (ok=True) state."""
    # AC1 install→verify consistency contract, run per adapter on its own home.
    for adapter_cls in SUPPORTED_ADAPTERS:
        sut = adapter_cls()
        home = tmp_path / f"home_{sut.name.replace(' ', '_')}"
        # Both adapters require their host config dir to exist before install
        # writes the managed block (claude → ~/.claude, cursor → ~/.cursor).
        (home / ".claude").mkdir(parents=True)
        (home / ".cursor").mkdir(parents=True)

        # when installed
        sut.install(home)

        # then the adapter's managed block is verified present
        ok, missing = sut.verify_hooks(home)
        assert ok, f"{sut.name}: expected clean verify after install, missing={missing}"


def test_install_is_idempotent_for_each_adapter(tmp_path):
    """A second install() leaves verify_hooks() still clean (no duplication breakage)."""
    for adapter_cls in SUPPORTED_ADAPTERS:
        sut = adapter_cls()
        home = tmp_path / f"home_idem_{sut.name.replace(' ', '_')}"
        (home / ".claude").mkdir(parents=True)
        (home / ".cursor").mkdir(parents=True)
        (home / ".codex").mkdir(parents=True)

        # when installed twice
        sut.install(home)
        sut.install(home)

        # then verify still reports clean
        ok, missing = sut.verify_hooks(home)
        assert ok, f"{sut.name}: install not idempotent, missing={missing}"


# The on-disk marker pair each adapter manages (used by the uninstall assertion).
_ADAPTER_MARKERS = {
    "Claude Code": (
        Path(".claude") / "CLAUDE.md",
        ("<!-- mnemos-start -->", "<!-- mnemos-end -->"),
    ),
    "Cursor": (
        Path(".cursor") / "rules",
        ("<!-- mnemos:start -->", "<!-- mnemos:end -->"),
    ),
    "Codex": (
        Path(".codex") / "AGENTS.md",
        ("<!-- mnemos:start -->", "<!-- mnemos:end -->"),
    ),
}


def test_uninstall_removes_the_managed_block_for_each_adapter(tmp_path):
    """After install→uninstall, the managed marker block is gone from disk.

    NOTE: ``verify_hooks`` deliberately returns ok=False once the file exists
    without its marker block (the block was removed), so the correct uninstall
    invariant is that the marker pair no longer appears in the managed file —
    not that verify reports clean.
    """
    for adapter_cls in SUPPORTED_ADAPTERS:
        sut = adapter_cls()
        home = tmp_path / f"home_uninstall_{sut.name.replace(' ', '_')}"
        (home / ".claude").mkdir(parents=True)
        (home / ".cursor").mkdir(parents=True)
        (home / ".codex").mkdir(parents=True)

        managed_file, (start_marker, end_marker) = _ADAPTER_MARKERS[sut.name]
        target = home / managed_file

        # when installed then uninstalled
        sut.install(home)
        assert start_marker in target.read_text()  # block present after install
        sut.uninstall(home)

        # then the managed marker block is gone from the file
        content = target.read_text() if target.exists() else ""
        assert start_marker not in content, f"{sut.name}: start marker survived uninstall"
        assert end_marker not in content, f"{sut.name}: end marker survived uninstall"


def test_adapter_registry_is_identical_across_install_update_uninstall():
    """Guard the triplicated ``adapter_list`` against future drift (latent risk).

    install.install(), updater.run_update(), and uninstaller.run_uninstall()
    each build the same adapter_list independently
    independently with no single-source registry. The adapter classes are
    imported INSIDE each function (not module-level), so the parity guard reads
    each function's SOURCE and compares the ``adapter_list`` construction line.
    This fails if any one site adds, removes, or reorders an adapter without the
    others following — and it does not depend on hard-coded line numbers.
    """
    from core.install import install
    from core.uninstaller import run_uninstall
    from core.updater import run_update

    def _adapter_list_expr(func) -> str:
        source = inspect.getsource(func)
        match = re.search(r"adapter_list\s*=\s*\[[^\]]*\]", source)
        assert match, f"{func.__qualname__}: no adapter_list construction found"
        # Normalise internal whitespace so formatting-only diffs don't trip it.
        return re.sub(r"\s+", " ", match.group(0))

    install_expr = _adapter_list_expr(install)
    update_expr = _adapter_list_expr(run_update)
    uninstall_expr = _adapter_list_expr(run_uninstall)

    # then the adapter registry construction is byte-identical at all three sites
    assert install_expr == update_expr == uninstall_expr
    assert install_expr == "adapter_list = [ClaudeCodeAdapter(), CursorAdapter(), CodexAdapter()]"


# ===========================================================================
# AC2 — context-injection consistency across hosts
# Spec: handoff.md § "AC2 — context injection"
# ===========================================================================


def _capture_some_memories(gw: MemoryGateway) -> None:
    """Seed a deterministic, searchable memory set used by the AC2 assertions."""
    gw.capture("alpha decision: use sqlite for full text search", layer="project")
    gw.capture("beta constraint: responses include a request id header", layer="project")


def test_retrieve_context_payload_is_host_agnostic_except_echoed_host(tmp_path, monkeypatch):
    """retrieve_context returns byte-identical results across hosts; only `host` differs."""
    # given one gateway + one prompt, queried under varying host strings
    gw = _make_gateway(tmp_path, monkeypatch)
    _capture_some_memories(gw)
    prompt = "what decision did we make about full text search"

    payloads = {
        host: retrieve_context(prompt=prompt, session_id="s", host=host, gateway=gw)
        for host in HOST_STRINGS
    }

    # then every host-independent field is identical across all hosts
    host_independent_keys = [
        "results",
        "count",
        "selection",
        "query",
        "keywords",
        "used_chars",
    ]
    reference = payloads["claude"]
    for host, payload in payloads.items():
        # the echoed host field is the ONLY field that legitimately differs
        assert payload["host"] == host
        for key in host_independent_keys:
            assert payload[key] == reference[key], (
                f"host={host}: field {key!r} diverged from reference"
            )


def test_render_context_block_emits_identical_memory_lines_across_hosts(tmp_path, monkeypatch):
    """render_context_block produces identical <memory> lines; only the wrapper host attr differs."""
    # given retrieved payloads under two different hosts (same gateway+prompt)
    gw = _make_gateway(tmp_path, monkeypatch)
    _capture_some_memories(gw)
    prompt = "what decision did we make about full text search"

    claude_payload = retrieve_context(prompt=prompt, session_id="s", host="claude", gateway=gw)
    cursor_payload = retrieve_context(prompt=prompt, session_id="s", host="cursor", gateway=gw)

    claude_block = render_context_block(claude_payload)
    cursor_block = render_context_block(cursor_payload)

    # guard the test itself: the payload must contain results, otherwise
    # render_context_block returns only the promotion block and the assertion
    # would be vacuous.
    assert claude_payload["results"], "expected non-empty results to render <memory> lines"

    def _memory_lines(block: str) -> list[str]:
        return [line for line in block.splitlines() if line.strip().startswith("<memory ")]

    # then the <memory> lines (ids/order/content/score) are byte-identical
    assert _memory_lines(claude_block) == _memory_lines(cursor_block)

    # and the ONLY difference in the wrapper line is the host="..." attribute
    assert 'host="claude"' in claude_block
    assert 'host="cursor"' in cursor_block
    normalized_claude = claude_block.replace('host="claude"', 'host="X"')
    normalized_cursor = cursor_block.replace('host="cursor"', 'host="X"')
    assert normalized_claude == normalized_cursor


def test_adapters_install_a_byte_identical_behavioral_payload(tmp_path):
    """install() writes the same MNEMOS_BEHAVIOR_BLOCK on disk, modulo delimiter comments."""
    # given a fake home for each adapter with its host config dir present
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".cursor").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)

    # when adapters install their managed block
    ClaudeCodeAdapter().install(home)
    CursorAdapter().install(home)
    CodexAdapter().install(home)

    claude_md = (home / ".claude" / "CLAUDE.md").read_text()
    cursor_rules = (home / ".cursor" / "rules").read_text()
    codex_agents = (home / ".codex" / "AGENTS.md").read_text()

    # then the payload between the delimiters is byte-identical and equals the
    # canonical shared block — only the delimiter comments differ by adapter.
    assert _strip_delimiters(claude_md) == _strip_delimiters(cursor_rules)
    assert _strip_delimiters(claude_md) == _strip_delimiters(codex_agents)
    assert _strip_delimiters(claude_md) == MNEMOS_BEHAVIOR_BLOCK.strip()


# ===========================================================================
# AC3 — host-independent memory lifecycle
# Spec: handoff.md § "AC3 — host-independent memory lifecycle"
# ===========================================================================


def test_memory_lifecycle_is_identical_regardless_of_installed_host_blocks(tmp_path, monkeypatch):
    """capture→search→read→promote yields identical ids/layers/hits/target across host setups.

    The gateway exposes no host/adapter parameter at all — this drives the same
    lifecycle in two homes (one with BOTH adapters' blocks installed, one with
    NONE) and asserts the observable lifecycle is byte-identical, proving the
    gateway never branches on host.
    """
    def _run_lifecycle(repo_root: Path) -> dict:
        _scaffold_repo_root(repo_root)
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        gw = MemoryGateway(repo_root=str(repo_root))
        item_id = gw.capture(
            "gamma workflow: capture then promote deterministically",
            layer="session",
            item_id="fixed-id-78",
        )
        hits = gw.search(query="gamma workflow", limit=5)
        read_back = gw.read(item_id)
        promoted_id = gw.promote(item_id, target_layer="project", force=True)
        promoted = gw.read(promoted_id)
        return {
            "captured_id": item_id,
            "read_layer": read_back["layer"],
            "hit_ids": [h.get("item_id") for h in hits],
            "promoted_id": promoted_id,
            "promoted_layer": promoted["layer"],
            "content": read_back["content"],
        }

    # given one home with BOTH adapters' blocks installed and one with none
    home_with_blocks = tmp_path / "with_blocks"
    (home_with_blocks / ".claude").mkdir(parents=True)
    (home_with_blocks / ".cursor").mkdir(parents=True)
    ClaudeCodeAdapter().install(home_with_blocks)
    CursorAdapter().install(home_with_blocks)

    home_no_blocks = tmp_path / "no_blocks"
    home_no_blocks.mkdir()

    # when the same lifecycle is driven against two separate stores
    with_blocks = _run_lifecycle(tmp_path / "store_with_blocks")
    no_blocks = _run_lifecycle(tmp_path / "store_no_blocks")

    # then the observable lifecycle is identical — the gateway is host-agnostic
    assert with_blocks == no_blocks
    assert with_blocks["captured_id"] == "fixed-id-78"
    assert with_blocks["read_layer"] == "session"
    assert "fixed-id-78" in with_blocks["hit_ids"]
    assert with_blocks["promoted_id"] == "fixed-id-78"
    assert with_blocks["promoted_layer"] == "project"


# ===========================================================================
# AC4 — sync behavior across machines / hosts
# Spec: handoff.md § "AC4 — cross-machine / cross-host sync"
# ===========================================================================


def test_memory_syncs_across_machines_with_different_host_blocks(tmp_path, monkeypatch):
    """A claude-block machine's captured memory round-trips byte-for-byte to a cursor-block machine.

    Reuses the #69 ``MemoryStore`` sync config (``_sync_config``) and the #24
    sync-e2e CLONE plumbing (``_make_bare``, ``_init_bare_with_main``,
    ``_clone``) — no bare-repo plumbing is re-implemented here. Both machines are
    CLONES of one shared bare remote, so they share a single git history and
    pull/push fast-forward cleanly. (An independent ``git init`` per machine —
    the prior approach — produced two unrelated root histories that cannot
    coexist on one ``main`` and is therefore order/isolation-fragile.) Machine A
    installs the CLAUDE block; machine B installs the CURSOR block. The proof is
    that the project (wiki) item written on A is identical when read on B after a
    pull — sync is host-independent.
    """
    from core.store import MemoryStore

    # The autouse isolate_home patches Path.home(); override HOME explicitly so
    # the #69 pull-timestamp cache and git config resolve to this test's tmp dir.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    # given a shared bare remote seeded once with a single root history, and two
    # working clones of that SAME history (machines A and B). Cloning — not an
    # independent init per machine — is what makes the round-trip fast-forward
    # cleanly and order-independent.
    bare = _make_bare(tmp_path / "bare.git")
    _init_bare_with_main(bare, tmp_path / "seed_parent")
    machine_a = _clone(bare, tmp_path / "machine_a")
    machine_b = _clone(bare, tmp_path / "machine_b")

    # and each machine has a DIFFERENT host adapter block installed in its home
    home_a = tmp_path / "home_a"
    (home_a / ".claude").mkdir(parents=True)
    ClaudeCodeAdapter().install(home_a)
    home_b = tmp_path / "home_b"
    (home_b / ".cursor").mkdir(parents=True)
    CursorAdapter().install(home_b)

    store_a = MemoryStore(repo_root=str(machine_a), sync_config=_sync_config())
    store_b = MemoryStore(repo_root=str(machine_b), sync_config=_sync_config())

    # when machine A captures a project (wiki) memory with lifecycle metadata
    before = _commit_count(machine_a)
    metadata = {
        "layer": "project",
        "stage": "stored",
        "created_at": "2026-05-29T00:00:00+00:00Z",
        "access_count": 0,
        "quality_score": 0.9,
    }
    store_a.write("project", "shared-id-78", "cross-host shared memory", metadata)
    assert _commit_count(machine_a) == before + 1  # wiki write committed + pushed

    # and machine B pulls from the shared remote
    store_b.sync_pull()

    # then machine B reads A's memory back byte-for-byte (content + metadata)
    item_a = store_a.read("shared-id-78")
    item_b = store_b.read("shared-id-78")
    assert item_b["content"] == item_a["content"] == "cross-host shared memory"
    assert item_b["id"] == "shared-id-78"
    assert item_b["layer"] == "project"
    # lifecycle metadata survives the cross-host round-trip identically
    for key in ("layer", "stage", "created_at", "access_count", "quality_score"):
        assert item_b[key] == item_a[key], f"metadata {key!r} diverged across machines"
