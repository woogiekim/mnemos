"""Source adapters that turn documents and code into memory candidates."""
from __future__ import annotations

import ast
import datetime
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from core.gateway import MemoryGateway


DOC_SUFFIXES = frozenset({
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".adoc",
    ".html",
    ".htm",
})
CODE_SUFFIXES = frozenset({
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".cs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
})
IGNORED_DIR_NAMES = frozenset({
    ".agent",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
})
MAX_TEXT_FILE_BYTES = 512_000
PROJECT_CONTEXT_NAMESPACE = uuid.UUID("0d7c4987-e222-46bb-9e50-52d4fce4d144")


@dataclass(frozen=True)
class SourceCandidate:
    """A source-derived memory candidate."""

    path: Path
    layer: str
    source_scope: str
    source_type: str
    content: str
    tags: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceIngestReport:
    """Result buckets for source-adapter ingestion."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.skipped)


@dataclass(frozen=True)
class ProjectContextSection:
    """One markdown section from a durable project-context file."""

    path: Path
    relative_path: str
    heading: str
    content: str
    kind: str
    project_id: str
    project_root: str
    project_root_hash: str
    tags: list[str]
    source_revision: str | None = None

    @property
    def memory_id(self) -> str:
        key = "|".join([
            self.project_id,
            self.project_root_hash,
            self.relative_path,
            self.heading,
        ])

        return str(uuid.uuid5(PROJECT_CONTEXT_NAMESPACE, key))

    @property
    def content_hash(self) -> str:
        return _sha256(self.content)


@dataclass
class ProjectContextRecallResult:
    """Structured project-context recall record for agent workflows."""

    memory_id: str
    score: float | None
    content: str
    source_path: str
    source_section: str
    tags: list[str]
    updated_at: str | None
    source_revision: str | None = None


@dataclass
class ProjectContextRecallReport:
    """Structured recall payload plus an orchestrator-friendly trace."""

    status: str
    query: str
    results: list[ProjectContextRecallResult] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    degraded_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "count": len(self.results),
            "results": [
                {
                    "memory_id": result.memory_id,
                    "score": result.score,
                    "content": result.content,
                    "source_path": result.source_path,
                    "source_section": result.source_section,
                    "tags": result.tags,
                    "updated_at": result.updated_at,
                    **(
                        {"source_revision": result.source_revision}
                        if result.source_revision is not None
                        else {}
                    ),
                }
                for result in self.results
            ],
            "trace": self.trace,
            "degraded_reasons": self.degraded_reasons,
        }


@dataclass
class ProjectContextAuditReport:
    """Freshness report for indexed markdown project-context sections."""

    status: str
    fresh: list[dict[str, Any]] = field(default_factory=list)
    stale: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    not_indexed: list[dict[str, Any]] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fresh_count": len(self.fresh),
            "stale_count": len(self.stale),
            "missing_count": len(self.missing),
            "not_indexed_count": len(self.not_indexed),
            "fresh": self.fresh,
            "stale": self.stale,
            "missing": self.missing,
            "not_indexed": self.not_indexed,
            "degraded_reasons": self.degraded_reasons,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def project_root_hash(project_root: str | Path) -> str:
    """Return a compact stable hash for project-root identity."""
    root = str(Path(project_root).expanduser().resolve())

    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _kind_from_path(path: Path, default: str) -> str:
    stem = path.stem.lower().replace("_", "-")
    aliases = {
        "architecture": "architecture",
        "decisions": "decision",
        "decision": "decision",
        "workflows": "workflow",
        "workflow": "workflow",
        "open-risks": "risk",
        "risks": "risk",
        "risk": "risk",
        "domain-glossary": "glossary",
        "glossary": "glossary",
        "project-map": "project-map",
    }

    return aliases.get(stem, default)


def _section_tags(
    *,
    project_id: str,
    project_root_hash_value: str,
    kind: str,
    source_path: str,
    source_section: str,
    extra_tags: Iterable[str],
) -> list[str]:
    tags = [
        "source:project-context",
        "project-context",
        f"project:{project_id}",
        f"project_root:{project_root_hash_value}",
        f"kind:{kind}",
        f"source_path:{source_path}",
        f"source_section:{source_section}",
        *extra_tags,
    ]

    return sorted(dict.fromkeys(tag for tag in tags if tag))


def _iter_markdown_sections(path: Path) -> Iterable[tuple[str, str]]:
    raw = _read_text_file(path)
    if raw is None or not raw.strip():
        return

    current_heading: str | None = None
    current_lines: list[str] = []
    preamble: list[str] = []

    for line in raw.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if current_heading is not None:
                yield current_heading, "\n".join(current_lines).strip()
            elif preamble:
                yield "(preamble)", "\n".join(preamble).strip()

            current_heading = match.group(2).strip()
            current_lines = [line]
            continue

        if current_heading is None:
            preamble.append(line)
        else:
            current_lines.append(line)

    if current_heading is not None:
        yield current_heading, "\n".join(current_lines).strip()
    elif preamble:
        yield "(document)", "\n".join(preamble).strip()


def _iter_files(root: Path, *, recursive: bool) -> Iterable[Path]:
    if recursive:
        for child in root.rglob("*"):
            try:
                rel_parts = child.relative_to(root).parts
            except ValueError:
                rel_parts = child.parts
            if any(part in IGNORED_DIR_NAMES for part in rel_parts[:-1]):
                continue
            if child.is_file():
                yield child
        return

    for child in root.iterdir():
        if child.is_file():
            yield child


def _read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _path_tags(root: Path, path: Path) -> list[str]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path.name
    if isinstance(rel, Path):
        parts = list(rel.parts[:-1])
    else:
        parts = []
    return [f"path:{part}" for part in parts[:3] if part and not part.startswith(".")]


class DocumentFolderScanner:
    """Discover local document files as memory candidates."""

    def __init__(
        self,
        root: str | Path,
        *,
        layer: str = "project",
        recursive: bool = True,
        suffixes: Iterable[str] = DOC_SUFFIXES,
    ) -> None:
        self.root = Path(root).resolve()
        self.layer = layer
        self.recursive = recursive
        self.suffixes = {suffix.lower() for suffix in suffixes}

    def discover(self) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        if not self.root.exists() or not self.root.is_dir():
            return candidates

        for path in sorted(_iter_files(self.root, recursive=self.recursive)):
            if path.suffix.lower() not in self.suffixes:
                continue
            content = _read_text_file(path)
            if content is None or not content.strip():
                continue
            rel = path.relative_to(self.root)
            rendered = (
                f"# Document source: {rel}\n\n"
                f"Source file: {rel}\n"
                f"Source adapter: docs_folder\n\n"
                f"{content.strip()}\n"
            )
            tags = [
                "source:docs",
                "docs",
                f"ext:{path.suffix.lower().lstrip('.') or 'txt'}",
                *_path_tags(self.root, path),
            ]
            candidates.append(SourceCandidate(
                path=path.resolve(),
                layer=self.layer,
                source_scope="docs_folder",
                source_type="docs_folder",
                content=rendered,
                tags=tags,
                metadata={"relative_path": str(rel)},
            ))

        return candidates


class CodebaseScanner:
    """Summarize code files into durable memory candidates."""

    def __init__(
        self,
        root: str | Path,
        *,
        layer: str = "project",
        recursive: bool = True,
        suffixes: Iterable[str] = CODE_SUFFIXES,
    ) -> None:
        self.root = Path(root).resolve()
        self.layer = layer
        self.recursive = recursive
        self.suffixes = {suffix.lower() for suffix in suffixes}

    def discover(self) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        if not self.root.exists() or not self.root.is_dir():
            return candidates

        for path in sorted(_iter_files(self.root, recursive=self.recursive)):
            if path.suffix.lower() not in self.suffixes:
                continue
            raw = _read_text_file(path)
            if raw is None or not raw.strip():
                continue

            rel = path.relative_to(self.root)
            language = _language_for(path)
            summary = _summarize_code(rel, raw, language)
            tags = [
                "source:code",
                "code",
                f"lang:{language}",
                f"ext:{path.suffix.lower().lstrip('.') or 'txt'}",
                *_path_tags(self.root, path),
            ]
            candidates.append(SourceCandidate(
                path=path.resolve(),
                layer=self.layer,
                source_scope="codebase",
                source_type="code_scan",
                content=summary,
                tags=tags,
                metadata={
                    "relative_path": str(rel),
                    "code_language": language,
                    "line_count": raw.count("\n") + 1,
                },
            ))

        return candidates


class ProjectContextScanner:
    """Discover section-level markdown project-context memories."""

    def __init__(
        self,
        source: str | Path,
        *,
        project_id: str,
        project_root: str | Path,
        kind: str = "context",
        recursive: bool = True,
        tags: Iterable[str] = (),
        source_revision: str | None = None,
    ) -> None:
        self.source = Path(source).resolve()
        self.project_id = project_id
        self.project_root = str(Path(project_root).expanduser().resolve())
        self.project_root_hash = project_root_hash(self.project_root)
        self.kind = kind
        self.recursive = recursive
        self.tags = list(tags)
        self.source_revision = source_revision

    def discover(self) -> list[ProjectContextSection]:
        paths = self._markdown_paths()
        sections: list[ProjectContextSection] = []

        for path in paths:
            rel = self._relative_path(path)
            kind = _kind_from_path(path, self.kind)
            for heading, content in _iter_markdown_sections(path):
                if not content:
                    continue
                section_tags = _section_tags(
                    project_id=self.project_id,
                    project_root_hash_value=self.project_root_hash,
                    kind=kind,
                    source_path=rel,
                    source_section=heading,
                    extra_tags=self.tags,
                )
                sections.append(ProjectContextSection(
                    path=path,
                    relative_path=rel,
                    heading=heading,
                    content=content,
                    kind=kind,
                    project_id=self.project_id,
                    project_root=self.project_root,
                    project_root_hash=self.project_root_hash,
                    tags=section_tags,
                    source_revision=self.source_revision,
                ))

        return sections

    def _markdown_paths(self) -> list[Path]:
        if self.source.is_file():
            return [self.source] if self.source.suffix.lower() in {".md", ".markdown"} else []
        if not self.source.is_dir():
            return []

        return [
            path
            for path in sorted(_iter_files(self.source, recursive=self.recursive))
            if path.suffix.lower() in {".md", ".markdown"}
        ]

    def _relative_path(self, path: Path) -> str:
        for root in (Path(self.project_root), self.source if self.source.is_dir() else self.source.parent):
            try:
                return str(path.relative_to(root))
            except ValueError:
                continue

        return path.name


class ProjectContextIngestor:
    """Capture or update durable project-context sections with stable IDs."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def ingest(
        self,
        sections: Iterable[ProjectContextSection],
        *,
        layer: str = "project",
        run_id: str = "project-context",
        dry_run: bool = False,
    ) -> SourceIngestReport:
        report = SourceIngestReport()

        for section in sections:
            memory_id = section.memory_id
            metadata = self._metadata(section, layer=layer)
            existing = self._read_existing(memory_id)

            if existing is not None and existing.get("source_content_hash") == section.content_hash:
                report.skipped.append(str(existing.get("id") or memory_id))
                continue

            if existing is not None:
                if dry_run:
                    report.updated.append(memory_id)
                    continue
                self._gw.update(item_id=memory_id, content=section.content)
                refreshed = self._gw._store.read(memory_id)
                self._gw._store.update(refreshed["_path"], metadata_updates=metadata)
                self._reindex(memory_id, section.content, layer, section.tags)
                report.updated.append(memory_id)
                continue

            if dry_run:
                report.created.append(memory_id)
                continue

            captured_id = self._gw.capture(
                layer=layer,
                item_id=memory_id,
                content=section.content,
                tags=section.tags,
                run_id=run_id,
                extra_metadata=metadata,
                no_classify=True,
            )
            if captured_id is not None:
                report.created.append(captured_id)

        return report

    def _metadata(self, section: ProjectContextSection, *, layer: str) -> dict[str, Any]:
        return {
            "sourceType": "project_context_section",
            "source_scope": "project_context",
            "project_id": section.project_id,
            "project_root": section.project_root,
            "project_root_hash": section.project_root_hash,
            "kind": section.kind,
            "source_path": section.relative_path,
            "source_file": str(section.path),
            "source_section": section.heading,
            "source_content_hash": section.content_hash,
            "source_revision": section.source_revision,
            "updated_at": _utc_now(),
            "layer": layer,
            "tags": section.tags,
        }

    def _read_existing(self, memory_id: str) -> dict[str, Any] | None:
        try:
            return self._gw._store.read(memory_id)
        except Exception:
            return None

    def _reindex(self, memory_id: str, content: str, layer: str, tags: list[str]) -> None:
        try:
            self._gw._fts.index_item(
                item_id=memory_id,
                content=content,
                metadata={"layer": layer, "tags": tags},
            )
        except Exception:
            return


class ProjectContextRecaller:
    """Structured project-context recall with project/kind/tag filters."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def recall(
        self,
        query: str,
        *,
        project_id: str | None = None,
        project_root_hash_value: str | None = None,
        kind: str | None = None,
        tags: Iterable[str] = (),
        active_files: Iterable[str] = (),
        agent_role: str | None = None,
        context_tags: Iterable[str] = (),
        layers: list[str] | None = None,
        limit: int = 10,
    ) -> ProjectContextRecallReport:
        filter_tags = ["source:project-context", *tags]
        if project_id:
            filter_tags.append(f"project:{project_id}")
        if project_root_hash_value:
            filter_tags.append(f"project_root:{project_root_hash_value}")
        if kind:
            filter_tags.append(f"kind:{kind}")

        enriched_query = _enrich_query(
            query,
            project_id=project_id,
            kind=kind,
            active_files=active_files,
            agent_role=agent_role,
            context_tags=context_tags,
        )

        try:
            raw_results = self._gw.search(
                query=query,
                layers=layers or ["project", "global"],
                limit=max(limit * 5, limit),
                tags=filter_tags,
            )
        except Exception as exc:
            return ProjectContextRecallReport(
                status="degraded",
                query=query,
                trace=_recall_trace(query, enriched_query, [], filter_tags),
                degraded_reasons=[f"search: {exc}"],
            )

        results: list[ProjectContextRecallResult] = []
        degraded_reasons: list[str] = []
        for index, result in enumerate(raw_results):
            item_id = str(result.get("item_id") or result.get("id") or "")
            if not item_id:
                continue
            try:
                item = self._gw._store.read(item_id)
            except Exception as exc:
                degraded_reasons.append(f"read:{item_id}: {exc}")
                continue
            if not _matches_project_context(item, project_id, project_root_hash_value, kind, tags):
                continue

            score = 1.0 if limit == 1 else max(0.0, 1.0 - (index / max(limit - 1, 1)))
            results.append(ProjectContextRecallResult(
                memory_id=str(item.get("id") or item_id),
                score=round(float(score), 6),
                content=str(item.get("content", "")),
                source_path=str(item.get("source_path") or ""),
                source_section=str(item.get("source_section") or ""),
                tags=list(item.get("tags") or []),
                updated_at=item.get("updated_at") or item.get("created_at"),
                source_revision=item.get("source_revision"),
            ))
            if len(results) >= limit:
                break

        status = "degraded" if degraded_reasons else "ok"

        return ProjectContextRecallReport(
            status=status,
            query=query,
            results=results,
            trace=_recall_trace(query, enriched_query, results, filter_tags),
            degraded_reasons=degraded_reasons,
        )


class ProjectContextAuditor:
    """Compare indexed project-context memories with current markdown sections."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def audit(
        self,
        sections: Iterable[ProjectContextSection],
        *,
        layer: str = "project",
    ) -> ProjectContextAuditReport:
        report = ProjectContextAuditReport(status="ok")
        section_list = list(sections)
        current_by_id = {section.memory_id: section for section in section_list}
        project_ids = {section.project_id for section in section_list}
        root_hashes = {section.project_root_hash for section in section_list}

        for memory_id, section in current_by_id.items():
            try:
                item = self._gw._store.read(memory_id)
            except FileNotFoundError:
                report.not_indexed.append(_audit_row(memory_id, section, "not_indexed"))
                continue
            except Exception as exc:
                report.degraded_reasons.append(f"read:{memory_id}: {exc}")
                continue

            stale_reasons = _stale_reasons(item, section)
            row = _audit_row(memory_id, section, "stale" if stale_reasons else "fresh")
            row["reasons"] = stale_reasons
            if stale_reasons:
                report.stale.append(row)
            else:
                report.fresh.append(row)

        try:
            for item_path in self._gw._store.list_layer(layer=layer, run_id=None):
                try:
                    item = self._gw._store.read(str(item_path))
                except Exception:
                    continue
                item_id = str(item.get("id") or "")
                if item.get("sourceType") != "project_context_section":
                    continue
                if project_ids and item.get("project_id") not in project_ids:
                    continue
                if root_hashes and item.get("project_root_hash") not in root_hashes:
                    continue
                if item_id in current_by_id:
                    continue
                report.missing.append({
                    "memory_id": item_id,
                    "status": "missing",
                    "source_path": item.get("source_path"),
                    "source_section": item.get("source_section"),
                    "project_id": item.get("project_id"),
                    "project_root_hash": item.get("project_root_hash"),
                })
        except Exception as exc:
            report.degraded_reasons.append(f"scan-indexed: {exc}")

        if report.degraded_reasons:
            report.status = "degraded"

        return report


def _enrich_query(
    query: str,
    *,
    project_id: str | None,
    kind: str | None,
    active_files: Iterable[str],
    agent_role: str | None,
    context_tags: Iterable[str],
) -> str:
    hints = [query]
    if project_id:
        hints.append(f"project {project_id}")
    if kind:
        hints.append(f"kind {kind}")
    if agent_role:
        hints.append(f"agent {agent_role}")
    hints.extend(f"file {path}" for path in active_files)
    hints.extend(f"tag {tag}" for tag in context_tags)

    return " ".join(part for part in hints if part).strip()


def _matches_project_context(
    item: dict[str, Any],
    project_id: str | None,
    project_root_hash_value: str | None,
    kind: str | None,
    tags: Iterable[str],
) -> bool:
    if item.get("sourceType") != "project_context_section":
        return False
    if project_id and item.get("project_id") != project_id:
        return False
    if project_root_hash_value and item.get("project_root_hash") != project_root_hash_value:
        return False
    if kind and item.get("kind") != kind:
        return False

    item_tags = set(item.get("tags") or [])

    return all(tag in item_tags for tag in tags)


def _recall_trace(
    query: str,
    enriched_query: str,
    results: Iterable[ProjectContextRecallResult],
    filter_tags: Iterable[str],
) -> dict[str, Any]:
    return {
        "query": query,
        "enriched_query": enriched_query,
        "filters": {"tags": list(filter_tags)},
        "used_memories": [
            {
                "memory_id": result.memory_id,
                "source_path": result.source_path,
                "source_section": result.source_section,
                "score": result.score,
            }
            for result in results
        ],
    }


def _stale_reasons(item: dict[str, Any], section: ProjectContextSection) -> list[str]:
    reasons: list[str] = []
    if item.get("source_content_hash") != section.content_hash:
        reasons.append("content_hash_changed")
    if item.get("source_path") != section.relative_path:
        reasons.append("source_path_changed")
    if item.get("source_section") != section.heading:
        reasons.append("source_section_changed")
    if item.get("source_revision") != section.source_revision:
        reasons.append("source_revision_changed")

    return reasons


def _audit_row(memory_id: str, section: ProjectContextSection, status: str) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "status": status,
        "source_path": section.relative_path,
        "source_section": section.heading,
        "project_id": section.project_id,
        "project_root_hash": section.project_root_hash,
        "kind": section.kind,
        "source_revision": section.source_revision,
    }


class SourceMemoryIngestor:
    """Capture source candidates with source_file based deduplication."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def ingest(
        self,
        candidates: Iterable[SourceCandidate],
        *,
        run_id: str,
        dry_run: bool = False,
    ) -> SourceIngestReport:
        report = SourceIngestReport()

        for candidate in candidates:
            source_file = str(candidate.path)
            content_hash = _sha256(candidate.content)
            existing = self._find_by_source_file(candidate.layer, source_file, run_id)

            if existing is not None and existing.get("content_hash") == content_hash:
                report.skipped.append(source_file if dry_run else str(existing["id"]))
                continue

            if existing is not None:
                if dry_run:
                    report.updated.append(source_file)
                    continue
                metadata = self._metadata(candidate, content_hash)
                self._gw.update(item_id=str(existing["id"]), content=candidate.content)
                refreshed = self._gw._store.read(str(existing["id"]))
                self._gw._store.update(refreshed["_path"], metadata_updates=metadata)
                report.updated.append(str(existing["id"]))
                continue

            if dry_run:
                report.created.append(source_file)
                continue

            metadata = self._metadata(candidate, content_hash)
            item_id = self._gw.capture(
                layer=candidate.layer,
                content=candidate.content,
                tags=candidate.tags,
                run_id=run_id,
                extra_metadata=metadata,
                no_classify=True,
            )
            if item_id is not None:
                report.created.append(item_id)

        return report

    def _metadata(self, candidate: SourceCandidate, content_hash: str) -> dict[str, Any]:
        return {
            "sourceType": candidate.source_type,
            "source_file": str(candidate.path),
            "source_scope": candidate.source_scope,
            "content_hash": content_hash,
            "tags": candidate.tags,
            **candidate.metadata,
        }

    def _find_by_source_file(
        self,
        layer: str,
        source_file: str,
        run_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            for item_path in self._gw._store.list_layer(layer=layer, run_id=run_id):
                try:
                    item = self._gw._store.read(str(item_path))
                except Exception:
                    continue
                if item.get("source_file") == source_file:
                    return item
        except Exception:
            return None
        return None


def _language_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".swift": "swift",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "csharp",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".h": "c-header",
        ".hpp": "cpp-header",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".json": "json",
    }.get(suffix, suffix.lstrip(".") or "text")


def _summarize_code(rel_path: Path, raw: str, language: str) -> str:
    symbols, imports = _extract_python(raw) if language == "python" else _extract_generic(raw)
    lines = raw.count("\n") + 1
    symbol_block = "\n".join(f"- {symbol}" for symbol in symbols[:40]) or "- (none detected)"
    import_block = "\n".join(f"- {name}" for name in imports[:30]) or "- (none detected)"

    return (
        f"# Code memory: {rel_path}\n\n"
        f"Source file: {rel_path}\n"
        f"Source adapter: code_scan\n"
        f"Language: {language}\n"
        f"Lines: {lines}\n\n"
        "Purpose: structural source memory for AI coding agents. This is not a "
        "complete call graph or language-server index.\n\n"
        "## Symbols\n"
        f"{symbol_block}\n\n"
        "## Imports / dependencies\n"
        f"{import_block}\n"
    )


def _extract_python(raw: str) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return _extract_generic(raw)

    symbols: list[str] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(f"{'.' * node.level}{module}")

    return sorted(set(symbols)), sorted(set(imports))


def _extract_generic(raw: str) -> tuple[list[str], list[str]]:
    symbols: list[str] = []
    imports: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        match = re.match(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][\w$]*)", stripped)
        if match:
            symbols.append(f"function {match.group(1)}")
        match = re.match(r"(?:export\s+)?class\s+([A-Za-z_][\w$]*)", stripped)
        if match:
            symbols.append(f"class {match.group(1)}")
        match = re.match(r"(?:import|from)\s+['\"]?([A-Za-z0-9_./@-]+)", stripped)
        if match:
            imports.append(match.group(1))
        match = re.match(r"(?:const|let|var)\s+([A-Za-z_][\w$]*)\s*=", stripped)
        if match:
            symbols.append(f"value {match.group(1)}")

    return sorted(set(symbols)), sorted(set(imports))
