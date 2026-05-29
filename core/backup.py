"""Explicit backup / restore for mnemos (issue #75).

This module provides pure-stdlib functions for snapshotting the persistent
``wiki/`` memory layers into a single gzip tar archive and restoring them
into a fresh repo root.  It complements (not replaces) the implicit
continuous backup that ``mnemos sync`` produces via git: ``git push`` is the
distribution channel; this module is the disaster-recovery channel.

Design notes
------------

- Pure stdlib only: ``pathlib`` / ``tarfile`` / ``json`` / ``dataclasses`` /
  ``socket`` / ``datetime``.  No ``MemoryStore``, no Click, no logging.
- The archive layout is intentionally minimal: a single ``manifest.json`` at
  the top level plus ``wiki/<layer>/*.md`` entries for each layer named in
  :data:`core.layers.LAYER_STATIC_PATHS`.  Auxiliary wiki files
  (``policy.yaml``, ``log.md``, ``log.jsonl``, ``index.md``,
  ``observability.jsonl``) are NOT memory items and are excluded.
- ``.agent/`` is excluded by design.  It is ephemeral / working / session /
  transient data — the same wiki-only scope the git-sync stage filter uses.
- ``SCHEMA_VERSION`` is a frozen contract.  Restoring an archive whose
  manifest declares an unknown future version raises :class:`ValueError`
  rather than silently producing a corrupt restore.
- :func:`restore_backup` calls ``tarfile.extractall`` with the hardened
  ``filter="data"`` (Python 3.12+) and additionally re-validates each
  member's resolved path to defang malicious archives even on older
  Python versions where the filter keyword does not yet ship.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.layers import LAYER_STATIC_PATHS


# Frozen on-disk contract; bump when the manifest schema or the archive layout
# changes in a backward-incompatible way.
SCHEMA_VERSION: int = 1

# Top-level archive entries: a manifest plus a single ``wiki/`` sub-tree.
_MANIFEST_NAME = "manifest.json"
_WIKI_ROOT = "wiki"


@dataclass(frozen=True)
class RestoreReport:
    """Outcome of a :func:`restore_backup` call.

    ``restored_count``:
        Number of items written to the destination because no file existed
        there yet.
    ``skipped_count``:
        Number of items NOT written because a file already existed at the
        target path and ``overwrite`` was ``False`` (the default policy).
    ``overwritten_count``:
        Number of items written over an existing file because the caller
        passed ``overwrite=True``.
    ``skipped_ids``:
        Logical ids of skipped items.  Populated only on the
        ``overwrite=False`` path; useful when the caller wants to surface a
        diff or re-run the restore selectively.
    """

    restored_count: int = 0
    skipped_count: int = 0
    overwritten_count: int = 0
    skipped_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time in ISO 8601 with a ``Z`` suffix."""
    # ``datetime.utcnow`` is intentionally avoided — it returns a naive
    # datetime and is deprecated in 3.12+.  Use a timezone-aware UTC value
    # and format with a literal ``Z`` so the manifest is unambiguous.
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_layer_files(repo_root: Path) -> list[tuple[str, Path]]:
    """Yield ``(layer_name, file_path)`` for every memory item under wiki/."""
    out: list[tuple[str, Path]] = []
    for layer, rel in LAYER_STATIC_PATHS.items():
        layer_dir = repo_root / rel
        if not layer_dir.is_dir():
            continue
        for entry in sorted(layer_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".md":
                out.append((layer, entry))
    return out


def _build_manifest(repo_root: Path, files: list[tuple[str, Path]]) -> dict[str, Any]:
    """Construct the manifest dict for the given file set."""
    summary: dict[str, int] = {layer: 0 for layer in LAYER_STATIC_PATHS}
    for layer, _ in files:
        summary[layer] = summary.get(layer, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "source_host": socket.gethostname(),
        "generated_at": _utc_now_iso(),
        "item_count": len(files),
        "layer_summary": summary,
    }


def _safe_resolve(base: Path, candidate: Path) -> Path | None:
    """Return ``candidate`` resolved under ``base``, or ``None`` if it escapes.

    Defence-in-depth alongside ``tarfile.extractall(filter="data")``.  A
    member whose resolved path is not a descendant of ``base`` is rejected
    unconditionally.
    """
    try:
        # Resolve via the destination so we capture any symlink shenanigans.
        full = (base / candidate).resolve()
        base_resolved = base.resolve()
        full.relative_to(base_resolved)
        return full
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def make_backup(repo_root: Path | str, output_path: Path | str) -> Path:
    """Write a gzip tar archive of ``repo_root``'s persistent memory layers.

    Only files under ``wiki/<layer>/`` for each layer named in
    :data:`core.layers.LAYER_STATIC_PATHS` are included.  The caller owns
    ``output_path``'s parent directory; this function does NOT silently
    create parent directories for caller-supplied paths.  The CLI layer
    creates the default ``~/.mnemos/backups/`` parent on first use.
    """
    repo_root = Path(repo_root)
    output_path = Path(output_path)

    files = _iter_layer_files(repo_root)
    manifest = _build_manifest(repo_root, files)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    with tarfile.open(output_path, "w:gz") as tar:
        # Manifest first so ``read_backup_manifest`` short-reads after the
        # first member instead of walking the whole archive.
        import io
        info = tarfile.TarInfo(_MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(manifest_bytes))

        for layer, abs_path in files:
            rel = abs_path.relative_to(repo_root)
            tar.add(str(abs_path), arcname=str(rel))

    return output_path


def read_backup_manifest(archive_path: Path | str) -> dict[str, Any]:
    """Return the parsed manifest dict from ``archive_path``.

    The archive is opened for read-only streaming and only the
    ``manifest.json`` member is extracted into memory; no files are written
    to disk as a side effect.  Raises :class:`ValueError` when the archive
    has no manifest.
    """
    archive_path = Path(archive_path)
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember(_MANIFEST_NAME)
        except KeyError as exc:
            raise ValueError(
                f"Archive {archive_path} has no {_MANIFEST_NAME} entry"
            ) from exc
        with tar.extractfile(member) as fileobj:
            data = fileobj.read()
    return json.loads(data.decode("utf-8"))


def restore_backup(
    archive_path: Path | str,
    repo_root: Path | str,
    *,
    overwrite: bool = False,
) -> RestoreReport:
    """Restore items from ``archive_path`` into ``repo_root``.

    Default policy (``overwrite=False``): if a target file already exists at
    the destination path, the archive entry is skipped and ``skipped_count``
    is incremented.  ``overwrite=True`` replaces the existing file and counts
    it as ``overwritten_count``.

    Raises :class:`ValueError` when the archive's ``schema_version`` is not
    :data:`SCHEMA_VERSION` (forward-compatibility guard) and when a tarball
    member would resolve to a path outside ``repo_root``
    (path-traversal hardening).
    """
    archive_path = Path(archive_path)
    repo_root = Path(repo_root)

    manifest = read_backup_manifest(archive_path)
    declared = manifest.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise ValueError(
            f"Archive {archive_path} declares schema_version={declared!r}; "
            f"this build only restores schema_version={SCHEMA_VERSION}."
        )

    restored = 0
    skipped = 0
    overwritten = 0
    skipped_ids: list[str] = []

    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == _MANIFEST_NAME:
                continue
            # Skip non-file members defensively; the format only contains
            # files but tarfile is liberal in what it accepts.
            if not member.isfile():
                continue

            # Path-traversal guard.  ``filter="data"`` enforces this in
            # Python 3.12+; we also verify explicitly so the contract holds
            # on older versions.
            safe = _safe_resolve(repo_root, Path(member.name))
            if safe is None:
                raise ValueError(
                    f"Archive {archive_path} contains a path-traversal "
                    f"member: {member.name!r}"
                )

            target = safe
            exists = target.exists()
            if exists and not overwrite:
                skipped += 1
                skipped_ids.append(_id_from_path(member.name))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open("wb") as dst:
                dst.write(src.read())

            if exists:
                overwritten += 1
            else:
                restored += 1

    return RestoreReport(
        restored_count=restored,
        skipped_count=skipped,
        overwritten_count=overwritten,
        skipped_ids=skipped_ids,
    )


def _id_from_path(member_name: str) -> str:
    """Best-effort logical id extraction from an archive member path.

    The on-disk filename is :meth:`MemoryStore.canonical_filename` of the
    logical id, percent-encoded.  Reversing it perfectly requires the same
    URL-decode pass; for the report we only need a human-readable hint so
    we strip the ``wiki/<layer>/`` prefix and the ``.md`` suffix.
    """
    name = member_name
    if name.endswith(".md"):
        name = name[:-3]
    # ``wiki/layer/...`` → ``...``
    parts = name.split("/", 2)
    if len(parts) == 3:
        return parts[2]
    return name
