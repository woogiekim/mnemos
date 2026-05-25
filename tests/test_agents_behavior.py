"""Behavioral tests for standalone agent modules."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter
import pytest

from agents.contradiction import ContradictionAgent
from agents.ingest import IngestAgent, _sha256
from agents.linker import LinkerAgent
from agents.lint import LintAgent
from agents.query import QueryAgent
from agents.scanner import ClaudeMdScanner
from agents.writer import WriterAgent


class FakeStore:
    def __init__(self, items: list[dict[str, Any]] | None = None, *, fail_list: bool = False) -> None:
        self.items = items or []
        self.fail_list = fail_list
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def list_layer(self, layer: str, run_id: str | None = None) -> list[Path]:
        if self.fail_list:
            raise RuntimeError("list failed")
        return [Path(str(item["_path"])) for item in self.items if item.get("layer") == layer]

    def read(self, item_path: str) -> dict[str, Any]:
        for item in self.items:
            if str(item.get("_path")) == item_path or str(item.get("id")) == item_path:
                if item.get("raise_read"):
                    raise RuntimeError("read failed")
                return dict(item)
        raise FileNotFoundError(item_path)

    def update(self, item_path: str, metadata_updates: dict[str, Any]) -> Path:
        self.updated.append((item_path, metadata_updates))
        for item in self.items:
            if str(item.get("_path")) == item_path:
                item.update(metadata_updates)
                return Path(item_path)
        return Path(item_path)


class FakeGateway:
    def __init__(
        self,
        root: Path,
        *,
        search_results: list[dict[str, Any]] | None = None,
        search_sequence: list[list[dict[str, Any]]] | None = None,
        store: FakeStore | None = None,
    ) -> None:
        self._root = str(root)
        self._store = store or FakeStore()
        self.search_results = search_results or []
        self.search_sequence = search_sequence or []
        self.captured: list[dict[str, Any]] = []
        self.updated: list[tuple[str, str]] = []
        self.used: dict[str, dict[str, Any]] = {}
        self.fail_capture = False
        self.fail_update = False

    def capture(self, **kwargs: Any) -> str:
        if self.fail_capture:
            raise RuntimeError("capture failed")
        item_id = f"item-{len(self.captured) + 1}"
        self.captured.append(dict(kwargs, item_id=item_id))
        return item_id

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.search_sequence:
            return self.search_sequence.pop(0)
        return list(self.search_results)

    def use(self, *, item_id: str) -> dict[str, Any]:
        item = self.used.get(item_id)
        if item is None:
            raise RuntimeError("use failed")
        return dict(item)

    def update(self, *, item_id: str, content: str) -> None:
        if self.fail_update:
            raise RuntimeError("update failed")
        self.updated.append((item_id, content))


def _write_md(path: Path, content: str, **metadata: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(content, **metadata)), encoding="utf-8")


def test_contradiction_agent_reports_empty_and_conflicting_claims(tmp_path: Path) -> None:
    gateway = FakeGateway(tmp_path)
    empty = ContradictionAgent(gateway).run()
    assert empty["contradictions"] == []
    assert Path(empty["report_path"]).read_text(encoding="utf-8").count("No contradictions")

    _write_md(tmp_path / "wiki" / "claims" / "a.md", "The API is stable.", id="a")
    _write_md(tmp_path / "wiki" / "claims" / "b.md", "The API is not stable.", id="b")
    (tmp_path / "wiki" / "claims" / "bad.md").write_text("---\nid: [bad\n---\n", encoding="utf-8")

    report = ContradictionAgent(gateway).run()

    assert report["total_claims"] == 2
    assert {report["contradictions"][0]["claim_a"], report["contradictions"][0]["claim_b"]} == {"a", "b"}
    assert "Found 1 potential" in Path(report["report_path"]).read_text(encoding="utf-8")


def test_linker_agent_detects_backlinks_and_handles_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = FakeGateway(tmp_path)
    _write_md(tmp_path / "wiki" / "projects" / "source.md", "See [[target]] and [[target]].", id="source")
    _write_md(tmp_path / "wiki" / "projects" / "target.md", "Target body\n\n## Backlinks\n- [[old]]", id="target")
    (tmp_path / "wiki" / "index.md").write_text("# index", encoding="utf-8")

    backlinks = LinkerAgent(gateway).run()

    assert backlinks["target"] == ["source"]
    assert "- [[source]]" in (tmp_path / "wiki" / "projects" / "target.md").read_text(encoding="utf-8")

    original_read_text = Path.read_text

    def flaky_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == "source.md":
            raise OSError("cannot read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert LinkerAgent(gateway).run()["source"] == ["target"]

    monkeypatch.setattr(Path, "read_text", original_read_text)

    def flaky_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name == "target.md":
            raise OSError("cannot write")
        return Path.open(path, *args, **kwargs)

    agent = LinkerAgent(gateway)
    monkeypatch.setattr(Path, "open", flaky_open)
    agent._write_backlinks(tmp_path / "wiki" / "projects" / "target.md", ["source"])


def test_lint_agent_reports_errors_warnings_and_clean_sections(tmp_path: Path) -> None:
    gateway = FakeGateway(tmp_path)
    (tmp_path / "wiki" / "log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "log.md").write_text("# log", encoding="utf-8")
    (tmp_path / "wiki" / "index.md").write_text("# index", encoding="utf-8")
    _write_md(
        tmp_path / "wiki" / "projects" / "valid.md",
        "Links to [[missing]].",
        id="valid",
        layer="project",
        stage="stored",
        created_at="2026-01-01T00:00:00Z",
    )
    _write_md(tmp_path / "wiki" / "projects" / "incomplete.md", "No metadata.", id="incomplete")
    (tmp_path / "wiki" / "projects" / "broken.md").write_text("---\nid: [bad\n---\n", encoding="utf-8")

    result = LintAgent(gateway).run()
    text = Path(result["report_path"]).read_text(encoding="utf-8")

    assert result["pages_checked"] == 3
    assert any("missing required" in error for error in result["errors"])
    assert any("invalid YAML" in error for error in result["errors"])
    assert any("broken wikilink" in warning for warning in result["warnings"])
    assert "## Errors" in text and "## Warnings" in text

    clean_root = tmp_path / "clean"
    _write_md(
        clean_root / "wiki" / "projects" / "target.md",
        "[[source]]",
        id="target",
        layer="project",
        stage="stored",
        created_at="2026-01-01T00:00:00Z",
    )
    _write_md(
        clean_root / "wiki" / "projects" / "source.md",
        "[[target]]",
        id="source",
        layer="project",
        stage="stored",
        created_at="2026-01-01T00:00:00Z",
    )
    clean = LintAgent(FakeGateway(clean_root)).run()
    assert clean["errors"] == []
    assert clean["warnings"] == []
    assert "None." in Path(clean["report_path"]).read_text(encoding="utf-8")


def test_query_agent_handles_empty_success_and_unreadable_sources(tmp_path: Path) -> None:
    empty = QueryAgent(FakeGateway(tmp_path)).run("missing")
    assert empty["item_ids"] == []
    assert "No memories found" in empty["answer"]

    gateway = FakeGateway(
        tmp_path,
        search_results=[
            {"item_id": "good", "content": "fallback good"},
            {"item_id": "bad", "content": "fallback bad"},
        ],
    )
    gateway.used["good"] = {"content": "answer content", "layer": "project"}

    result = QueryAgent(gateway).run("question")

    assert result["item_ids"] == ["good", "bad"]
    assert result["answer"] == "answer content"
    assert result["sources"][1]["snippet"] == "fallback bad"


def test_writer_agent_creates_and_updates_entries(tmp_path: Path) -> None:
    gateway = FakeGateway(
        tmp_path,
        search_sequence=[
            [{"item_id": "memory-1", "content": "remember this"}],
            [],
        ],
    )

    created = WriterAgent(gateway).run("Topic")

    assert created == "item-1"
    assert "remember this" in gateway.captured[0]["content"]

    gateway.search_sequence = [
        [{"item_id": "memory-1", "content": "remember this"}],
        [{"item_id": "existing", "content": "old"}],
    ]
    updated = WriterAgent(gateway).run("Topic")

    assert updated == "existing"
    assert gateway.updated[0][0] == "existing"

    gateway.search_sequence = [[], []]
    empty_created = WriterAgent(gateway).run("Empty")

    assert empty_created == "item-2"
    assert "No memories found" in gateway.captured[1]["content"]


def test_ingest_agent_runs_directory_and_file_modes(tmp_path: Path) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "doc.txt").write_text("body", encoding="utf-8")
    (source_dir / "subdir").mkdir()
    gateway = FakeGateway(tmp_path)
    agent = IngestAgent(gateway)

    assert agent.run(str(tmp_path / "missing")) == []
    assert agent.run(str(source_dir)) == ["item-1"]
    assert gateway.captured[0]["tags"] == ["ingest", "txt"]

    explicit = source_dir / "explicit.md"
    explicit.write_text("explicit", encoding="utf-8")
    assert agent.run(str(source_dir), files=[explicit, source_dir / "missing.md"]) == ["item-2"]

    gateway.fail_capture = True
    assert agent.run(str(source_dir)) == []
    assert agent.run(str(source_dir), files=[explicit]) == []


def test_ingest_agent_scanner_results_and_dedup_modes(tmp_path: Path) -> None:
    file_path = tmp_path / "CLAUDE.md"
    file_path.write_text("memory", encoding="utf-8")
    gateway = FakeGateway(tmp_path)
    agent = IngestAgent(gateway)

    assert agent.run_scanner_results([(file_path, "project", "project")]) == ["item-1"]
    assert agent.run_scanner_results([(tmp_path / "missing.md", "project", "project")]) == []
    gateway.fail_capture = True
    assert agent.run_scanner_results([(file_path, "project", "project")]) == []
    gateway.fail_capture = False

    assert agent.run_scanner_results_dedup(
        [(file_path, "project", "project")],
        dry_run=True,
    ) == {"created": [str(file_path)], "updated": [], "skipped": []}

    created = agent.run_scanner_results_dedup([(file_path, "project", "project")])
    assert created["created"] == ["item-2"]

    existing = {
        "id": "existing",
        "_path": str(tmp_path / "existing.md"),
        "layer": "project",
        "source_file": str(file_path),
        "content_hash": _sha256("memory"),
    }
    store = FakeStore([existing])
    agent = IngestAgent(FakeGateway(tmp_path, store=store))

    assert agent.run_scanner_results_dedup([(file_path, "project", "project")])["skipped"] == ["existing"]
    assert agent.run_scanner_results_dedup([(file_path, "project", "project")], dry_run=True)["skipped"] == [
        str(file_path)
    ]

    file_path.write_text("changed", encoding="utf-8")
    gateway = FakeGateway(tmp_path, store=store)
    agent = IngestAgent(gateway)
    assert agent.run_scanner_results_dedup([(file_path, "project", "project")], dry_run=True)["updated"] == [
        str(file_path)
    ]
    assert agent.run_scanner_results_dedup([(file_path, "project", "project")])["updated"] == ["existing"]
    assert gateway.updated[0][0] == "existing"
    assert store.updated[0][1]["content_hash"] == _sha256("changed")


def test_ingest_agent_dedup_handles_read_update_capture_and_scan_failures(tmp_path: Path) -> None:
    file_path = tmp_path / "memory.md"
    file_path.write_text("memory", encoding="utf-8")
    unreadable = tmp_path / "unreadable.md"
    unreadable.write_text("bad", encoding="utf-8")
    existing = {
        "id": "existing",
        "_path": str(tmp_path / "existing.md"),
        "layer": "project",
        "source_file": str(file_path),
        "content_hash": "old",
    }

    gateway = FakeGateway(tmp_path, store=FakeStore([existing]))
    gateway.fail_update = True
    assert gateway._store.read(str(tmp_path / "existing.md"))["id"] == "existing"
    assert IngestAgent(gateway).run_scanner_results_dedup([(file_path, "project", "project")]) == {
        "created": [],
        "updated": [],
        "skipped": [],
    }

    gateway = FakeGateway(tmp_path, store=FakeStore())
    gateway.fail_capture = True
    assert IngestAgent(gateway).run_scanner_results_dedup([(file_path, "project", "project")]) == {
        "created": [],
        "updated": [],
        "skipped": [],
    }

    gateway = FakeGateway(tmp_path, store=FakeStore(fail_list=True))
    assert IngestAgent(gateway)._find_by_source_file("project", str(file_path)) is None

    broken_store = FakeStore([dict(existing, raise_read=True)])
    assert IngestAgent(FakeGateway(tmp_path, store=broken_store))._find_by_source_file(
        "project",
        str(file_path),
    ) is None
    assert IngestAgent(FakeGateway(tmp_path)).run_scanner_results_dedup(
        [(tmp_path / "missing.md", "project", "project")]
    ) == {"created": [], "updated": [], "skipped": []}

    original_read_text = Path.read_text

    def flaky_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == unreadable:
            raise OSError("cannot read")
        return original_read_text(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Path, "read_text", flaky_read)
        assert IngestAgent(FakeGateway(tmp_path)).run_scanner_results_dedup(
            [(unreadable, "project", "project")]
        ) == {"created": [], "updated": [], "skipped": []}


def test_scanner_skips_non_file_memory_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projects_root = tmp_path / "claude-projects"
    memory_dir = projects_root / "proj" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "folder.md").mkdir()
    (memory_dir / "actual.md").write_text("actual", encoding="utf-8")

    monkeypatch.setattr("agents.scanner._CLAUDE_PROJECTS_ROOT", projects_root)

    results = ClaudeMdScanner(project_root=tmp_path).discover_memory_files()

    assert results == [((memory_dir / "actual.md").resolve(), "global", "claude_memory")]
