"""Tests for explicit backup/restore (issue #75).

TDD spec for ``core.backup``: a pure-stdlib module that produces gzip tar
snapshots of the persistent ``wiki/`` layers (plus a top-level
``manifest.json``) and restores them into a fresh repo root with byte-equal
fidelity for every documented memory-item metadata field.  Two additive Click
subcommands (``mnemos backup`` and ``mnemos restore``) drive the same pure
functions from the CLI surface.

Every test here is fully self-contained: tests use ``tmp_path`` /
``tmp_path_factory`` for repo roots, write memory items via ``MemoryStore``,
exercise the backup/restore round-trip, and assert metadata preservation by
parsing the restored file's front-matter directly.  No test asserts on raw byte
contents of the tarball — the contract is "round-trip preserves the documented
metadata fields and the wiki layout", not "two compressions are byte-identical
on every platform".
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

from core.layers import LAYER_STATIC_PATHS
from core.store import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap_repo(root: Path) -> Path:
    """Create the minimum ``wiki/`` layout that ``MemoryStore`` expects."""
    wiki = root / "wiki"
    for rel in LAYER_STATIC_PATHS.values():
        (root / rel).mkdir(parents=True, exist_ok=True)
    # Sentinel files that other parts of the project create on bootstrap.
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")
    return root


def _write_item(
    root: Path,
    *,
    layer: str,
    item_id: str,
    content: str,
    metadata: dict,
) -> Path:
    """Write a memory item via ``MemoryStore`` and return its file path."""
    store = MemoryStore(repo_root=str(root))
    return store.write(layer=layer, item_id=item_id, content=content, metadata=metadata)


def _read_item(path: Path) -> dict:
    """Parse a memory item file's front-matter + content into a flat dict."""
    post = frontmatter.load(str(path))
    out = dict(post.metadata)
    out["content"] = post.content
    return out


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_single_item_round_trip(self, tmp_path):
        """Capture one memory, backup, restore into a fresh root, assert every
        documented metadata field survives byte-equal."""
        from core.backup import make_backup, restore_backup

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")

        original_metadata = {
            "tag": ["round-trip", "issue-75"],
            "layer": "project",
            "trust_level": "verified",
            "quality_score": 0.91,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:00:00Z",
        }
        original_content = "single-item round trip body"
        original_path = _write_item(
            src,
            layer="project",
            item_id="rt-single-1",
            content=original_content,
            metadata=original_metadata,
        )

        archive = tmp_path / "snap.tar.gz"
        result_path = make_backup(src, archive)
        assert result_path == archive
        assert archive.exists()

        report = restore_backup(archive, dst)
        assert report.restored_count == 1
        assert report.skipped_count == 0
        assert report.overwritten_count == 0

        restored_path = dst / original_path.relative_to(src)
        assert restored_path.exists()

        original = _read_item(original_path)
        restored = _read_item(restored_path)
        for field in ("id", "tag", "layer", "trust_level", "quality_score",
                      "lifecycle_action", "created_at", "content"):
            assert restored[field] == original[field], f"{field} drifted"

    def test_multi_item_round_trip(self, tmp_path):
        """Many items across many layers; every item is restored under its
        original relative path and metadata survives."""
        from core.backup import make_backup, restore_backup

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")

        layers = ["project", "global", "entities", "claims", "topics"]
        written: list[tuple[str, Path]] = []
        for idx, layer in enumerate(layers):
            metadata = {
                "tag": [f"multi-{idx}"],
                "layer": layer,
                "trust_level": "validated",
                "quality_score": 0.5 + 0.05 * idx,
                "lifecycle_action": "stored",
                "created_at": f"2026-05-29T07:{idx:02d}:00Z",
            }
            path = _write_item(
                src,
                layer=layer,
                item_id=f"multi-{layer}-{idx}",
                content=f"multi body {layer}/{idx}",
                metadata=metadata,
            )
            written.append((layer, path))

        archive = tmp_path / "multi.tar.gz"
        make_backup(src, archive)
        report = restore_backup(archive, dst)
        assert report.restored_count == len(written)
        for _, original_path in written:
            restored_path = dst / original_path.relative_to(src)
            assert restored_path.exists()
            original = _read_item(original_path)
            restored = _read_item(restored_path)
            assert restored == original

    def test_multi_host_round_trip(self, tmp_path_factory, monkeypatch):
        """Host A captures, backs up, then host B (different
        ``MNEMOS_REPO_ROOT``) restores. Byte-equal modulo absolute paths."""
        from core.backup import make_backup, restore_backup

        host_a = _bootstrap_repo(tmp_path_factory.mktemp("host_a"))
        host_b = _bootstrap_repo(tmp_path_factory.mktemp("host_b"))

        # Host A — capture
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(host_a))
        meta = {
            "tag": ["multi-host"],
            "layer": "entities",
            "trust_level": "verified",
            "quality_score": 0.77,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:30:00Z",
        }
        original_path = _write_item(
            host_a,
            layer="entities",
            item_id="multi-host-1",
            content="multi-host body",
            metadata=meta,
        )

        archive = tmp_path_factory.mktemp("archives") / "host-a.tar.gz"
        make_backup(host_a, archive)

        # Host B — restore
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(host_b))
        report = restore_backup(archive, host_b)
        assert report.restored_count == 1

        restored_path = host_b / original_path.relative_to(host_a)
        assert restored_path.exists()
        # Byte-equality of the file content (front-matter + body).
        original_bytes = original_path.read_bytes()
        restored_bytes = restored_path.read_bytes()
        assert restored_bytes == original_bytes


# ---------------------------------------------------------------------------
# Conflict / overwrite semantics
# ---------------------------------------------------------------------------


class TestConflictPolicy:
    def test_default_skip_on_conflict(self, tmp_path):
        """Restoring a backup whose ids already exist defaults to SKIP."""
        from core.backup import make_backup, restore_backup

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")
        meta = {
            "tag": ["conflict"],
            "layer": "global",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:31:00Z",
        }
        _write_item(src, layer="global", item_id="dup-1",
                    content="original body", metadata=meta)
        # Pre-populate dst with the same id and DIFFERENT content.
        _write_item(dst, layer="global", item_id="dup-1",
                    content="existing body", metadata=meta)

        archive = tmp_path / "dup.tar.gz"
        make_backup(src, archive)
        report = restore_backup(archive, dst)

        assert report.restored_count == 0
        assert report.skipped_count == 1
        assert report.overwritten_count == 0
        assert "dup-1" in report.skipped_ids

        # The pre-existing file is untouched (still says "existing body").
        existing = (dst / "wiki" / "global" / "dup-1.md").read_text(encoding="utf-8")
        assert "existing body" in existing
        assert "original body" not in existing

    def test_overwrite_flag(self, tmp_path):
        """``overwrite=True`` replaces conflicting ids and counts as
        overwritten."""
        from core.backup import make_backup, restore_backup

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")
        meta = {
            "tag": ["overwrite"],
            "layer": "claims",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:32:00Z",
        }
        _write_item(src, layer="claims", item_id="ow-1",
                    content="new body", metadata=meta)
        _write_item(dst, layer="claims", item_id="ow-1",
                    content="old body", metadata=meta)

        archive = tmp_path / "ow.tar.gz"
        make_backup(src, archive)
        report = restore_backup(archive, dst, overwrite=True)

        assert report.restored_count == 0
        assert report.skipped_count == 0
        assert report.overwritten_count == 1
        existing = (dst / "wiki" / "claims" / "ow-1.md").read_text(encoding="utf-8")
        assert "new body" in existing
        assert "old body" not in existing


# ---------------------------------------------------------------------------
# Schema-version guard + manifest read
# ---------------------------------------------------------------------------


class TestManifest:
    def test_schema_version_guard(self, tmp_path):
        """Restoring an archive with an unknown future ``schema_version``
        raises ``ValueError`` carrying the offending version number."""
        from core.backup import restore_backup, SCHEMA_VERSION

        future = tmp_path / "future.tar.gz"
        manifest = {
            "schema_version": SCHEMA_VERSION + 99,
            "source_host": "test-host",
            "generated_at": "2026-05-29T07:33:00Z",
            "item_count": 0,
            "layer_summary": {},
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        with tarfile.open(future, "w:gz") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))

        dst = _bootstrap_repo(tmp_path / "dst")
        with pytest.raises(ValueError) as exc:
            restore_backup(future, dst)
        assert str(SCHEMA_VERSION + 99) in str(exc.value)

    def test_read_manifest_without_extract(self, tmp_path):
        """``read_backup_manifest`` returns the parsed manifest dict without
        unpacking the archive contents to disk."""
        from core.backup import make_backup, read_backup_manifest, SCHEMA_VERSION

        src = _bootstrap_repo(tmp_path / "src")
        meta = {
            "tag": ["m"],
            "layer": "topics",
            "trust_level": "verified",
            "quality_score": 0.4,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:34:00Z",
        }
        _write_item(src, layer="topics", item_id="m-1",
                    content="m body", metadata=meta)

        archive = tmp_path / "m.tar.gz"
        make_backup(src, archive)

        manifest = read_backup_manifest(archive)
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["item_count"] == 1
        assert manifest["layer_summary"].get("topics") == 1
        assert "source_host" in manifest
        assert manifest["generated_at"].endswith("Z")

        # No side effects on disk: a sibling "extract-target" dir does not get
        # populated by the read call.
        peek = tmp_path / "peek"
        peek.mkdir()
        manifest2 = read_backup_manifest(archive)
        assert manifest == manifest2
        assert list(peek.iterdir()) == []


# ---------------------------------------------------------------------------
# Exclusion + path-traversal hardening
# ---------------------------------------------------------------------------


class TestExclusionsAndHardening:
    def test_agent_directory_excluded(self, tmp_path):
        """Items under ``.agent/`` (ephemeral / transient) are excluded from
        the backup by default; the resulting archive has no ``.agent/``
        entries."""
        from core.backup import make_backup

        src = _bootstrap_repo(tmp_path / "src")
        meta = {
            "tag": ["exclusion"],
            "layer": "transient",
            "trust_level": "verified",
            "quality_score": 0.1,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:35:00Z",
        }
        # Write a transient item that lands under .agent/transient.
        _write_item(src, layer="transient", item_id="trans-1",
                    content="ephemeral body", metadata=meta)
        # Sanity: file actually exists under .agent/.
        assert (src / ".agent" / "transient" / "trans-1.md").exists()

        archive = tmp_path / "exclude.tar.gz"
        make_backup(src, archive)

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert all(not n.startswith(".agent/") for n in names), names
        assert all(".agent/" not in n for n in names), names

    def test_path_traversal_rejection(self, tmp_path):
        """A malicious tarball entry whose resolved path escapes the
        destination repo root must be rejected by ``restore_backup``."""
        from core.backup import restore_backup, SCHEMA_VERSION

        malicious = tmp_path / "malicious.tar.gz"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_host": "evil-host",
            "generated_at": "2026-05-29T07:36:00Z",
            "item_count": 1,
            "layer_summary": {"project": 1},
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        evil_body = b"---\nid: evil\n---\nshould-not-land"
        with tarfile.open(malicious, "w:gz") as tar:
            mi = tarfile.TarInfo("manifest.json")
            mi.size = len(manifest_bytes)
            tar.addfile(mi, io.BytesIO(manifest_bytes))

            # Escape attempt: relative path that climbs out of repo_root.
            ti = tarfile.TarInfo("../escape.md")
            ti.size = len(evil_body)
            tar.addfile(ti, io.BytesIO(evil_body))

        dst = _bootstrap_repo(tmp_path / "dst")
        with pytest.raises((tarfile.TarError, ValueError)):
            restore_backup(malicious, dst)

        # The escape file MUST NOT exist on disk anywhere outside dst.
        assert not (dst.parent / "escape.md").exists()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_backup_default_output(self, tmp_path, monkeypatch):
        """``mnemos backup`` with no --output writes a tarball under
        ``~/.mnemos/backups/<UTC ts>.tar.gz``."""
        from core.cli import cli

        src = _bootstrap_repo(tmp_path / "src")
        meta = {
            "tag": ["cli"],
            "layer": "global",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:37:00Z",
        }
        _write_item(src, layer="global", item_id="cli-1",
                    content="cli body", metadata=meta)

        # Isolate HOME so the CLI default destination does not pollute the
        # developer's real ~/.mnemos.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(src))

        runner = CliRunner()
        result = runner.invoke(cli, ["backup"])
        assert result.exit_code == 0, result.output

        backups_dir = fake_home / ".mnemos" / "backups"
        assert backups_dir.is_dir()
        archives = list(backups_dir.glob("*.tar.gz"))
        assert len(archives) == 1
        # The printed path is the absolute archive path.
        assert str(archives[0]) in result.output

    def test_cli_backup_custom_output(self, tmp_path, monkeypatch):
        """``mnemos backup --output PATH`` writes to PATH exactly."""
        from core.cli import cli

        src = _bootstrap_repo(tmp_path / "src")
        meta = {
            "tag": ["cli-custom"],
            "layer": "topics",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:38:00Z",
        }
        _write_item(src, layer="topics", item_id="cli-custom-1",
                    content="cli custom body", metadata=meta)

        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(src))
        out = tmp_path / "custom.tar.gz"
        runner = CliRunner()
        result = runner.invoke(cli, ["backup", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert str(out) in result.output

    def test_cli_restore_default_skip(self, tmp_path, monkeypatch):
        """``mnemos restore --input PATH`` defaults to skip on conflict."""
        from core.backup import make_backup
        from core.cli import cli

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")
        meta = {
            "tag": ["cli-skip"],
            "layer": "claims",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:39:00Z",
        }
        _write_item(src, layer="claims", item_id="cli-skip-1",
                    content="src body", metadata=meta)
        _write_item(dst, layer="claims", item_id="cli-skip-1",
                    content="dst body", metadata=meta)
        archive = tmp_path / "skip.tar.gz"
        make_backup(src, archive)

        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(dst))
        runner = CliRunner()
        result = runner.invoke(cli, ["restore", "--input", str(archive)])
        assert result.exit_code == 0, result.output
        # The summary line reports the per-bucket counts.
        assert "restored: 0" in result.output
        assert "skipped: 1" in result.output
        assert "overwritten: 0" in result.output

        # The pre-existing file is unchanged.
        existing = (dst / "wiki" / "claims" / "cli-skip-1.md").read_text(encoding="utf-8")
        assert "dst body" in existing
        assert "src body" not in existing

    def test_cli_restore_overwrite(self, tmp_path, monkeypatch):
        """``mnemos restore --input PATH --overwrite`` forces overwrite and
        the bucket counts reflect that."""
        from core.backup import make_backup
        from core.cli import cli

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")
        meta = {
            "tag": ["cli-ow"],
            "layer": "entities",
            "trust_level": "verified",
            "quality_score": 0.5,
            "lifecycle_action": "stored",
            "created_at": "2026-05-29T07:40:00Z",
        }
        _write_item(src, layer="entities", item_id="cli-ow-1",
                    content="new src body", metadata=meta)
        _write_item(dst, layer="entities", item_id="cli-ow-1",
                    content="old dst body", metadata=meta)
        archive = tmp_path / "ow.tar.gz"
        make_backup(src, archive)

        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(dst))
        runner = CliRunner()
        result = runner.invoke(
            cli, ["restore", "--input", str(archive), "--overwrite"]
        )
        assert result.exit_code == 0, result.output
        assert "overwritten: 1" in result.output

        replaced = (dst / "wiki" / "entities" / "cli-ow-1.md").read_text(encoding="utf-8")
        assert "new src body" in replaced
        assert "old dst body" not in replaced


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------


class TestDefensivePaths:
    """Coverage for the small defensive branches in core.backup + core.cli."""

    def test_iter_layer_files_skips_missing_layer_dir(self, tmp_path):
        """``make_backup`` tolerates a repo with no layer dirs at all — the
        ``layer_dir.is_dir()`` skip branch is exercised."""
        from core.backup import _iter_layer_files, make_backup

        # Bare repo root with NO ``wiki/`` sub-tree at all.
        bare = tmp_path / "bare"
        bare.mkdir()
        assert _iter_layer_files(bare) == []

        # And the public API works end-to-end on a bare root.
        archive = tmp_path / "bare.tar.gz"
        make_backup(bare, archive)
        assert archive.exists()

    def test_read_manifest_missing_raises_value_error(self, tmp_path):
        """An archive without ``manifest.json`` raises ``ValueError`` from
        :func:`read_backup_manifest`."""
        from core.backup import read_backup_manifest

        no_manifest = tmp_path / "no-manifest.tar.gz"
        body = b"hello"
        with tarfile.open(no_manifest, "w:gz") as tar:
            info = tarfile.TarInfo("only-file.md")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))

        with pytest.raises(ValueError) as exc:
            read_backup_manifest(no_manifest)
        assert "manifest.json" in str(exc.value)

    def test_restore_ignores_non_file_members(self, tmp_path):
        """Restore tolerates directory and symlink entries in a tarball —
        the ``isfile()`` skip branch is exercised."""
        from core.backup import restore_backup, SCHEMA_VERSION

        mixed = tmp_path / "mixed.tar.gz"
        manifest_bytes = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "source_host": "test",
            "generated_at": "2026-05-29T07:41:00Z",
            "item_count": 0,
            "layer_summary": {},
        }).encode("utf-8")
        with tarfile.open(mixed, "w:gz") as tar:
            mi = tarfile.TarInfo("manifest.json")
            mi.size = len(manifest_bytes)
            tar.addfile(mi, io.BytesIO(manifest_bytes))

            # Directory entry — not a file.
            di = tarfile.TarInfo("wiki/projects")
            di.type = tarfile.DIRTYPE
            di.mode = 0o755
            tar.addfile(di)

        dst = _bootstrap_repo(tmp_path / "dst")
        report = restore_backup(mixed, dst)
        assert report.restored_count == 0
        assert report.skipped_count == 0
        assert report.overwritten_count == 0

    def test_id_from_path_handles_short_names(self):
        """``_id_from_path`` falls back to the raw name when the path does
        not have the canonical ``wiki/<layer>/<id>.md`` three-part shape."""
        from core.backup import _id_from_path

        assert _id_from_path("bare.md") == "bare"
        assert _id_from_path("just-a-name") == "just-a-name"
        assert _id_from_path("wiki/projects/canonical.md") == "canonical"

    def test_cli_backup_propagates_make_backup_error(self, tmp_path, monkeypatch):
        """``mnemos backup`` reports a clear error (and exits non-zero) when
        the underlying ``make_backup`` call raises."""
        from core.cli import cli

        # Pointing --output at a path inside a non-existent parent dir
        # surfaces an OSError from tarfile.open through the CLI's error
        # branch.
        bad = tmp_path / "nope" / "child" / "out.tar.gz"
        runner = CliRunner()
        result = runner.invoke(cli, ["backup", "--output", str(bad)])
        assert result.exit_code != 0
        assert "error:" in result.output

    def test_cli_restore_propagates_schema_error(self, tmp_path, monkeypatch):
        """``mnemos restore`` exits 1 and prints the message when the
        archive declares an unknown schema_version."""
        from core.backup import SCHEMA_VERSION
        from core.cli import cli

        bad = tmp_path / "bad.tar.gz"
        manifest_bytes = json.dumps({
            "schema_version": SCHEMA_VERSION + 1,
            "source_host": "test",
            "generated_at": "2026-05-29T07:42:00Z",
            "item_count": 0,
            "layer_summary": {},
        }).encode("utf-8")
        with tarfile.open(bad, "w:gz") as tar:
            mi = tarfile.TarInfo("manifest.json")
            mi.size = len(manifest_bytes)
            tar.addfile(mi, io.BytesIO(manifest_bytes))

        dst = _bootstrap_repo(tmp_path / "dst")
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(dst))
        runner = CliRunner()
        result = runner.invoke(cli, ["restore", "--input", str(bad)])
        assert result.exit_code == 1
        assert "schema_version" in result.output

    def test_cli_restore_propagates_generic_error(self, tmp_path, monkeypatch):
        """``mnemos restore`` also handles non-ValueError exceptions from
        the underlying API (e.g. tarfile corruption)."""
        from core.cli import cli

        # A file that exists on disk but is not a valid tar.gz archive.
        bogus = tmp_path / "bogus.tar.gz"
        bogus.write_bytes(b"this is definitely not a gzip stream")
        dst = _bootstrap_repo(tmp_path / "dst")
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(dst))
        runner = CliRunner()
        result = runner.invoke(cli, ["restore", "--input", str(bogus)])
        assert result.exit_code == 1
        assert "error:" in result.output


class TestEmptyStore:
    def test_empty_store_backup(self, tmp_path):
        """Backing up an empty store produces a valid archive whose manifest
        item_count is 0 and restoring it into a fresh root is a no-op."""
        from core.backup import (
            make_backup,
            read_backup_manifest,
            restore_backup,
        )

        src = _bootstrap_repo(tmp_path / "src")
        dst = _bootstrap_repo(tmp_path / "dst")

        archive = tmp_path / "empty.tar.gz"
        make_backup(src, archive)
        assert archive.exists()

        manifest = read_backup_manifest(archive)
        assert manifest["item_count"] == 0
        # layer_summary may be empty dict or zeros — either is acceptable.
        for layer in LAYER_STATIC_PATHS:
            assert manifest["layer_summary"].get(layer, 0) == 0

        report = restore_backup(archive, dst)
        assert report.restored_count == 0
        assert report.skipped_count == 0
        assert report.overwritten_count == 0
