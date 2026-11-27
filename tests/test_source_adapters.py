"""Tests for document and code source adapters."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Minimal mnemos repo structure for gateway/CLI tests."""
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    for dirname in ["runs", "sessions", "state", "reports", "tools"]:
        (tmp_path / ".agent" / dirname).mkdir(parents=True)
    (tmp_path / ".agent" / "workflows" / "hooks").mkdir(parents=True)

    policy = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 9999.0, "access_count": 9999, "quality_score": 1.1},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 9999.0, "access_count": 9999, "quality_score": 1.1},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")

    return tmp_path


def test_document_folder_scanner_discovers_docs_and_skips_ignored_dirs(tmp_path: Path) -> None:
    from agents.source_adapters import DocumentFolderScanner

    docs = tmp_path / "docs"
    (docs / "nested").mkdir(parents=True)
    (docs / ".agent").mkdir()
    (docs / "guide.md").write_text("# Guide\nUse mnemos memory.", encoding="utf-8")
    (docs / "nested" / "notes.txt").write_text("Persistent notes", encoding="utf-8")
    (docs / ".agent" / "secret.md").write_text("ignored", encoding="utf-8")
    (docs / "image.png").write_text("not text docs", encoding="utf-8")

    candidates = DocumentFolderScanner(docs).discover()

    rel_paths = {candidate.metadata["relative_path"] for candidate in candidates}
    assert rel_paths == {"guide.md", "nested/notes.txt"}
    assert all(candidate.source_type == "docs_folder" for candidate in candidates)
    assert all("source:docs" in candidate.tags for candidate in candidates)
    assert candidates[0].content.startswith("# Document source:")


def test_codebase_scanner_summarizes_python_symbols(tmp_path: Path) -> None:
    from agents.source_adapters import CodebaseScanner

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "import os\n\n"
        "class Runner:\n"
        "    def run(self):\n"
        "        return os.getcwd()\n",
        encoding="utf-8",
    )

    candidates = CodebaseScanner(src).discover()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source_type == "code_scan"
    assert "source:code" in candidate.tags
    assert "lang:python" in candidate.tags
    assert "- class Runner" in candidate.content
    assert "- def run" in candidate.content
    assert "- os" in candidate.content


def test_source_ingestor_creates_updates_and_skips_by_source_file(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from agents.source_adapters import DocumentFolderScanner, SourceMemoryIngestor
    from core.gateway import MemoryGateway

    docs = tmp_path / "docs"
    docs.mkdir()
    guide = docs / "guide.md"
    guide.write_text("# Guide\nInitial guidance.", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    ingestor = SourceMemoryIngestor(gateway)

    first_report = ingestor.ingest(
        DocumentFolderScanner(docs).discover(),
        run_id="source-test",
    )
    assert len(first_report.created) == 1

    item_id = first_report.created[0]
    item = gateway._store.read(item_id)
    assert item["sourceType"] == "docs_folder"
    assert item["source_file"] == str(guide.resolve())
    assert item["relative_path"] == "guide.md"

    same_report = ingestor.ingest(
        DocumentFolderScanner(docs).discover(),
        run_id="source-test",
    )
    assert same_report.skipped == [item_id]

    guide.write_text("# Guide\nUpdated guidance.", encoding="utf-8")
    changed_report = ingestor.ingest(
        DocumentFolderScanner(docs).discover(),
        run_id="source-test",
    )
    assert changed_report.updated == [item_id]
    assert "Updated guidance." in gateway._store.read(item_id)["content"]


def test_ingest_docs_cli_dry_run_does_not_write(repo_root: Path, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "overview.md").write_text("# Overview\nSource docs.", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        ["ingest-docs", str(docs), "--dry-run"],
        env={"MNEMOS_REPO_ROOT": str(repo_root)},
    )

    assert result.exit_code == 0, result.output
    assert "[dry-run] would ingest docs:" in result.output
    assert not list((repo_root / "wiki" / "projects").glob("*.md"))


def test_scan_code_cli_creates_code_memory(repo_root: Path, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.py").write_text(
        "from pathlib import Path\n\n"
        "def build_path(name):\n"
        "    return Path(name)\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["scan-code", str(src)],
        env={"MNEMOS_REPO_ROOT": str(repo_root)},
    )

    assert result.exit_code == 0, result.output
    assert "code created:" in result.output

    matches = list((repo_root / "wiki" / "projects").glob("*.md"))
    assert len(matches) == 1
    post = frontmatter.load(str(matches[0]))
    assert post.get("sourceType") == "code_scan"
    assert "source:code" in post.get("tags", [])
    assert "def build_path" in post.content


def test_capabilities_include_source_adapters() -> None:
    result = CliRunner().invoke(cli, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capability_status"]["source_document_ingestion"] == "supported"
    assert payload["capability_status"]["source_code_scan"] == "supported"
    assert payload["capability_status"]["project_context_section_capture"] == "supported"
    assert payload["capability_status"]["project_context_recall"] == "supported"
    assert payload["capability_status"]["project_context_freshness_audit"] == "supported"


@pytest.mark.parametrize("backend", ["default", "obsidian"])
def test_project_context_dry_run_leaves_store_and_caches_unchanged(repo_root: Path, monkeypatch, backend) -> None:
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("MNEMOS_BACKEND", backend)
    cache = repo_root / "hash-cache"
    monkeypatch.setenv("MNEMOS_HASH_INDEX_CACHE_DIR", str(cache))
    vault = repo_root / "vault"
    (repo_root / "mnemos.yml").write_text(f"storage:\n  vault_path: {vault}\n", encoding="utf-8")
    source = repo_root / "checks.md"
    source.write_text("# First verification\n\nKeep this check.\n", encoding="utf-8")
    args = ["project-context", "capture", str(source), "--project-id", "mnemos", "--project-root",
            str(repo_root), "--section", "First verification", "--json"]
    files_before = set(repo_root.rglob("*"))
    home_cache = Path.home() / ".mnemos/.cache"
    home_before = set(home_cache.glob("*")) if home_cache.exists() else None

    preview = CliRunner().invoke(cli, [*args, "--dry-run"])

    assert preview.exit_code == 0, preview.output
    report = json.loads(preview.output)
    assert report["section_count"] == 1 and len(report["created"]) == 1
    assert set(repo_root.rglob("*")) == files_before
    assert (set(home_cache.glob("*")) if home_cache.exists() else None) == home_before


def test_project_context_capture_selects_only_one_exact_heading(repo_root: Path, monkeypatch) -> None:
    from core.gateway import MemoryGateway

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    source = repo_root / "checks.md"
    source.write_text("# First verification\n\nKeep this check.\n\n# Other verification\n\nOther scope.\n",
                      encoding="utf-8")
    runner = CliRunner()
    flags = ["project-context", "capture", str(source), "--project-id", "mnemos", "--project-root",
             str(repo_root), "--section", "First verification", "--json"]

    preview = runner.invoke(cli, [*flags, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert not list((repo_root / "wiki/projects").glob("*.md"))

    capture = runner.invoke(cli, flags)
    assert capture.exit_code == 0, capture.output
    report = json.loads(capture.output)
    assert report["section_count"] == 1 and json.loads(preview.output) == report
    memory_id, = report["created"]
    item = MemoryGateway(repo_root=str(repo_root)).peek(memory_id)
    assert item["source_path"] == "checks.md"
    assert item["source_section"] == "First verification"
    assert "Other scope" not in item["content"]
    assert json.loads(runner.invoke(cli, flags).output)["skipped"] == [memory_id]
    assert len(list((repo_root / "wiki/projects").glob("*.md"))) == 1


@pytest.mark.parametrize(("case", "message"), [
    ("wrong-heading", "expected exactly one section"),
    ("duplicate", "expected exactly one section"),
    ("directory", "one Markdown file"),
    ("outside", "inside --project-root"),
])
def test_project_context_capture_rejects_invalid_section_scope_before_writing(
    repo_root: Path, monkeypatch, case, message,
) -> None:
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    root = repo_root / "project"
    root.mkdir()
    heading = "Requested"
    path = (repo_root if case == "outside" else root) / "checks.md"
    text = "# Other\nSome context." if case == "wrong-heading" else "# Requested\nSome context."
    if case == "duplicate":
        text += "\n\n## Requested\nAnother scope."
    path.write_text(text, encoding="utf-8")
    if case == "directory":
        path = root

    result = CliRunner().invoke(cli, ["project-context", "capture", str(path), "--project-id", "mnemos",
                                     "--project-root", str(root), "--section", heading])

    assert result.exit_code == 2, result.output
    assert message in result.output
    assert not list((repo_root / "wiki/projects").glob("*.md"))
    assert not (repo_root / ".agent/state/fts.db").exists()


def test_project_context_capture_update_stable_id_and_metadata(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextScanner
    from core.gateway import MemoryGateway

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    arch = context_dir / "architecture.md"
    arch.write_text("# API Boundary\nUse provider JSON for agents.", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    sections = ProjectContextScanner(
        context_dir,
        project_id="mnemos",
        project_root=tmp_path,
        source_revision="rev1",
    ).discover()
    report = ProjectContextIngestor(gateway).ingest(sections)

    assert len(report.created) == 1
    memory_id = report.created[0]
    item = gateway._store.read(memory_id)
    assert item["sourceType"] == "project_context_section"
    assert item["project_id"] == "mnemos"
    assert item["kind"] == "architecture"
    assert item["source_path"] == "context/architecture.md"
    assert item["source_section"] == "API Boundary"
    assert item["source_revision"] == "rev1"
    assert "project:mnemos" in item["tags"]
    assert "kind:architecture" in item["tags"]

    arch.write_text("# API Boundary\nUse structured recall for agents.", encoding="utf-8")
    changed_sections = ProjectContextScanner(
        context_dir,
        project_id="mnemos",
        project_root=tmp_path,
        source_revision="rev2",
    ).discover()
    changed_report = ProjectContextIngestor(gateway).ingest(changed_sections)

    assert changed_report.updated == [memory_id]
    updated = gateway._store.read(memory_id)
    assert "structured recall" in updated["content"]
    assert updated["source_revision"] == "rev2"


@pytest.mark.parametrize("reopen_gateway", [False, True])
def test_project_context_capture_keeps_identical_sections_owned_by_each_root(
    repo_root: Path, tmp_path: Path, reopen_gateway: bool,
) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextRecaller, ProjectContextScanner
    from core.gateway import MemoryGateway

    common_text = "Project recall uses section metadata."
    updated_text = "Project recall uses immutable source metadata."

    def discover(root: Path):
        return ProjectContextScanner(root, project_id="mnemos", project_root=root).discover()

    roots = [tmp_path / name / "mnemos" for name in ("original", "checkout")]
    for root in roots:
        root.mkdir(parents=True)
        (root / "decisions.md").write_text(f"# Retrieval\n{common_text}\n", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    ingestor = ProjectContextIngestor(gateway)
    first, second = [discover(root)[0] for root in roots]
    assert ingestor.ingest([first]).created == [first.memory_id]

    if reopen_gateway:
        gateway = MemoryGateway(repo_root=str(repo_root))
        ingestor = ProjectContextIngestor(gateway)
    assert first.memory_id != second.memory_id
    assert ingestor.ingest([second]).created == [second.memory_id]

    (roots[0] / "decisions.md").write_text(f"# Retrieval\n{updated_text}\n", encoding="utf-8")
    assert ingestor.ingest(discover(roots[0])).updated == [first.memory_id]
    assert ingestor.ingest(discover(roots[0])).skipped == [first.memory_id]

    for section, expected in ((first, updated_text), (second, common_text)):
        item = gateway.peek(section.memory_id)
        assert item["content"].strip() == f"# Retrieval\n{expected}"
        assert item["project_root_hash"] == section.project_root_hash
        recalled = ProjectContextRecaller(gateway).recall(
            "metadata", project_id="mnemos", project_root_hash_value=section.project_root_hash,
        )
        assert [record["memory_id"] for record in recalled.to_dict()["results"]] == [section.memory_id]


@pytest.mark.parametrize(
    ("status", "stage", "superseded_by", "expected_status"),
    [
        (None, "stored", None, "active"),
        ("invalidated", "stored", None, "invalidated"),
        ("verified", "stored", None, "verified"),
        (None, "archived", None, None),
        (None, "stored", "replacement", None),
    ],
)
def test_project_context_recapture_preserves_explicit_status_and_retirement(
    repo_root: Path,
    tmp_path: Path,
    status: str | None,
    stage: str,
    superseded_by: str | None,
    expected_status: str | None,
) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextScanner
    from core.gateway import MemoryGateway

    source = tmp_path / "decisions.md"
    source.write_text("# Boundary\nUse scoped recall.\n", encoding="utf-8")
    scanner = ProjectContextScanner(source, project_id="mnemos", project_root=tmp_path)
    gateway = MemoryGateway(repo_root=str(repo_root))
    ingestor = ProjectContextIngestor(gateway)
    sections = scanner.discover()
    memory_id = ingestor.ingest(sections).created[0]
    item = gateway.peek(memory_id)
    gateway._store.update(
        item["_path"],
        metadata_updates={"semantic_status": status, "stage": stage, "superseded_by": superseded_by},
    )

    preview = ingestor.ingest(sections, dry_run=True)
    assert gateway.peek(memory_id).get("semantic_status") == status
    report = ingestor.ingest(sections)

    assert report.updated == preview.updated == ([memory_id] if status != expected_status else [])
    assert gateway.peek(memory_id).get("semantic_status") == expected_status
    assert ingestor.ingest(sections).skipped == [memory_id]

    source.write_text("# Boundary\nUse scoped recall after revisions.\n", encoding="utf-8")
    assert ingestor.ingest(scanner.discover()).updated == [memory_id]
    updated = gateway.peek(memory_id)
    assert updated.get("semantic_status") == expected_status
    assert updated["stage"] == stage
    assert updated.get("superseded_by") == superseded_by


def test_project_context_recapture_restores_edited_body_at_same_source_hash(repo_root: Path, tmp_path: Path) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextScanner, _stale_reasons
    from core.gateway import MemoryGateway

    source = tmp_path / "decisions.md"
    original = "# Boundary\nUse scoped recall."
    source.write_text(original + "\n", encoding="utf-8")
    section, = ProjectContextScanner(source, project_id="mnemos", project_root=tmp_path).discover()
    gateway = MemoryGateway(repo_root=str(repo_root))
    ingestor = ProjectContextIngestor(gateway)
    memory_id, = ingestor.ingest([section]).created
    gateway.update(memory_id, "# Boundary\nDisable scoped recall.")
    edited = gateway.peek(memory_id)
    assert edited["source_content_hash"] == section.content_hash

    assert ingestor.ingest([section]).updated == [memory_id]
    assert _stale_reasons(edited, section) == ["content_body_changed"]
    current = gateway.peek(memory_id)
    assert current["content"].strip() == original
    assert current["source_content_hash"] == section.content_hash
    assert ingestor.ingest([section]).skipped == [memory_id]


def test_project_context_recall_filters_and_trace_json(repo_root: Path, tmp_path: Path) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextRecaller, ProjectContextScanner
    from core.gateway import MemoryGateway

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "architecture.md").write_text(
        "# Retrieval\nProject recall uses section metadata.",
        encoding="utf-8",
    )
    (context_dir / "workflows.md").write_text(
        "# Release\nShip through local verification.",
        encoding="utf-8",
    )

    gateway = MemoryGateway(repo_root=str(repo_root))
    mnemos_sections = ProjectContextScanner(
        context_dir,
        project_id="mnemos",
        project_root=tmp_path,
        tags=["agent-crew"],
    ).discover()
    ProjectContextIngestor(gateway).ingest(mnemos_sections)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "architecture.md").write_text(
        "# Retrieval\nOther project recall.",
        encoding="utf-8",
    )
    other_sections = ProjectContextScanner(
        other_dir,
        project_id="other",
        project_root=other_dir,
    ).discover()
    ProjectContextIngestor(gateway).ingest(other_sections)

    report = ProjectContextRecaller(gateway).recall(
        "section metadata",
        project_id="mnemos",
        kind="architecture",
        tags=["agent-crew"],
        active_files=["core/cli.py"],
        agent_role="backend",
        context_tags=["durable"],
        limit=5,
    )
    payload = report.to_dict()

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    result = payload["results"][0]
    assert set(result) >= {
        "memory_id",
        "score",
        "content",
        "source_path",
        "source_section",
        "tags",
        "updated_at",
    }
    assert result["source_path"] == "context/architecture.md"
    assert result["source_section"] == "Retrieval"
    assert 0.0 <= result["score"] <= 1.0
    assert payload["trace"]["used_memories"][0]["memory_id"] == result["memory_id"]
    assert "file core/cli.py" in payload["trace"]["enriched_query"]


def test_project_context_recall_uses_scoped_read_only_recall_and_preserves_metadata() -> None:
    from agents.source_adapters import ProjectContextRecaller
    from core.contracts import RecallReport

    class ForbiddenStore:
        def read(self, *_args, **_kwargs):
            raise AssertionError("_store.read must not be used by project-context recall")

    class FakeGateway:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self._store = ForbiddenStore()

        def search(self, *_args, **_kwargs):
            raise AssertionError("gw.search must not be used by project-context recall")

        def recall(self, **kwargs):
            self.calls.append(kwargs)
            memory = SimpleNamespace(
                id="pc-1",
                score=0.87321,
                score_components={"semantic": 0.7, "workflow": 0.2},
                content="Project context recall content",
                layer="project",
                tags=("source:project-context", "project:mnemos", "kind:architecture", "agent-crew"),
                project_id="mnemos",
                project_root_hash="root-hash",
                task_shape="debug",
                record_type="project_context_section",
                provenance={"source": "project-context"},
                source_revision="rev1",
                source_path="context/architecture.md",
                source_section="Recall",
                updated_at="2026-07-29T00:00:00Z",
            )
            return RecallReport(
                queries=(kwargs["queries"][0],),
                candidates=(memory,),
                selected=(memory,),
                candidate_limit=kwargs["candidate_limit"],
                selected_limit=kwargs["selected_limit"],
                max_selected_chars=3600,
                used_chars=len(memory.content),
                diagnostics={"attempts": []},
            )

    gateway = FakeGateway()
    report = ProjectContextRecaller(gateway).recall(
        "base query",
        project_id="mnemos",
        project_root_hash_value="root-hash",
        kind="architecture",
        tags=["agent-crew"],
        active_files=["core/cli.py"],
        agent_role="backend",
        context_tags=["durable"],
        limit=3,
    )
    payload = report.to_dict()

    recall_call = gateway.calls[0]
    assert recall_call["queries"] == [
        "base query project mnemos kind architecture agent backend file core/cli.py tag durable",
        "base query",
    ]
    assert recall_call["project_id"] == "mnemos"
    assert recall_call["project_root_hash"] == "root-hash"
    assert "task_shape" not in recall_call
    assert recall_call["layers"] == ["project", "global"]
    assert recall_call["tags_all"] == ["source:project-context", "agent-crew", "kind:architecture"]
    assert recall_call["selected_limit"] == 3
    assert payload["count"] == 1
    result = payload["results"][0]
    assert result["score"] == 0.87321
    assert result["score_components"] == {"semantic": 0.7, "workflow": 0.2}
    assert result["source_revision"] == "rev1"
    assert result["source_path"] == "context/architecture.md"
    assert result["source_section"] == "Recall"
    assert result["project_id"] == "mnemos"
    assert result["project_root_hash"] == "root-hash"
    assert result["layer"] == "project"
    assert result["provenance"] == {"source": "project-context"}
    assert result["task_shape"] == "debug"
    assert result["record_type"] == "project_context_section"
    assert payload["trace"]["retrieved_memories"][0]["memory_id"] == "pc-1"
    assert payload["trace"]["used_memories"] == payload["trace"]["retrieved_memories"]


def test_project_context_recall_is_read_only_for_memory_metadata(repo_root: Path, tmp_path: Path) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextRecaller, ProjectContextScanner
    from core.gateway import MemoryGateway

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "architecture.md").write_text(
        "# Read Only\nRecall should not mutate lifecycle metadata.",
        encoding="utf-8",
    )
    gateway = MemoryGateway(repo_root=str(repo_root))
    sections = ProjectContextScanner(context_dir, project_id="mnemos", project_root=tmp_path).discover()
    created = ProjectContextIngestor(gateway).ingest(sections).created
    assert len(created) == 1
    memory_id = created[0]
    before = gateway.peek(memory_id)

    report = ProjectContextRecaller(gateway).recall(
        "lifecycle metadata",
        project_id="mnemos",
        kind="architecture",
        limit=5,
    )

    after = gateway.peek(memory_id)
    assert report.status == "ok"
    assert after["access_count"] == before["access_count"]
    assert after["stage"] == before["stage"]
    assert after["layer"] == before["layer"]


def test_project_context_audit_reports_stale_and_missing(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    from agents.source_adapters import ProjectContextAuditor, ProjectContextIngestor, ProjectContextScanner
    from core.gateway import MemoryGateway

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    arch = context_dir / "architecture.md"
    risk = context_dir / "open-risks.md"
    arch.write_text("# Boundary\nInitial boundary.", encoding="utf-8")
    risk.write_text("# Risk\nInitial risk.", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    sections = ProjectContextScanner(context_dir, project_id="mnemos", project_root=tmp_path).discover()
    ProjectContextIngestor(gateway).ingest(sections)

    arch.write_text("# Boundary\nChanged boundary.", encoding="utf-8")
    risk.unlink()
    current_sections = ProjectContextScanner(context_dir, project_id="mnemos", project_root=tmp_path).discover()
    report = ProjectContextAuditor(gateway).audit(current_sections).to_dict()

    assert report["stale_count"] == 1
    assert report["missing_count"] == 1
    assert report["stale"][0]["reasons"] == ["content_hash_changed"]
    assert report["missing"][0]["source_path"] == "context/open-risks.md"


def test_project_context_audit_ignores_other_projects(repo_root: Path, tmp_path: Path) -> None:
    from agents.source_adapters import ProjectContextAuditor, ProjectContextIngestor, ProjectContextScanner
    from core.gateway import MemoryGateway

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "architecture.md").write_text("# Boundary\nMnemos boundary.", encoding="utf-8")
    (context_dir / "workflows.md").write_text("# Flow\nMnemos flow.", encoding="utf-8")

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "architecture.md").write_text("# Boundary\nOther boundary.", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    mnemos_sections = ProjectContextScanner(context_dir, project_id="mnemos", project_root=tmp_path).discover()
    other_sections = ProjectContextScanner(other_dir, project_id="other", project_root=other_dir).discover()
    ProjectContextIngestor(gateway).ingest(mnemos_sections)
    ProjectContextIngestor(gateway).ingest(other_sections)

    (context_dir / "architecture.md").unlink()
    current_sections = ProjectContextScanner(context_dir, project_id="mnemos", project_root=tmp_path).discover()
    report = ProjectContextAuditor(gateway).audit(current_sections).to_dict()

    assert report["missing_count"] == 1
    assert report["missing"][0]["project_id"] == "mnemos"


def test_project_context_cli_json_and_fallback_behavior(repo_root: Path, tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / "architecture.md").write_text(
        "# Recall\nDurable markdown context.",
        encoding="utf-8",
    )

    capture = CliRunner().invoke(
        cli,
        [
            "project-context",
            "capture",
            str(context_dir),
            "--project-id",
            "mnemos",
            "--project-root",
            str(tmp_path),
            "--json",
        ],
        env={"MNEMOS_REPO_ROOT": str(repo_root)},
    )
    assert capture.exit_code == 0, capture.output
    captured = json.loads(capture.output)
    assert len(captured["created"]) == 1

    recall = CliRunner().invoke(
        cli,
        [
            "project-context",
            "recall",
            "durable markdown",
            "--project-id",
            "mnemos",
            "--kind",
            "architecture",
            "--trace-json",
        ],
        env={"MNEMOS_REPO_ROOT": str(repo_root)},
    )
    assert recall.exit_code == 0, recall.output
    payload = json.loads(recall.output)
    assert payload["status"] == "ok"
    assert payload["results"][0]["memory_id"] == captured["created"][0]
    assert payload["trace"]["used_memories"][0]["source_section"] == "Recall"

    missing_repo = tmp_path / "missing-repo"
    fallback = CliRunner().invoke(
        cli,
        [
            "project-context",
            "capture",
            str(context_dir),
            "--project-id",
            "mnemos",
            "--project-root",
            str(tmp_path),
            "--json",
        ],
        env={"MNEMOS_REPO_ROOT": str(missing_repo)},
    )
    assert fallback.exit_code == 0, fallback.output
    degraded = json.loads(fallback.output)
    assert degraded["status"] == "degraded"
    assert degraded["degraded_reasons"]
