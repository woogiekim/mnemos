# AGENTS.md — mnemos Agent Manifest

This file describes all agents registered in the mnemos memory operating system.

## Agent Registry

| Agent | Module | Role |
|---|---|---|
| Ingest | mnemos.agents.ingest | Reads raw/sources/, runs capture for each document |
| Writer | mnemos.agents.writer | Generates/rewrites wiki entries from captured memories |
| Linker | mnemos.agents.linker | Detects cross-references between wiki pages, writes backlinks |
| Contradiction | mnemos.agents.contradiction | Detects conflicting claims in wiki/claims/ |
| Lint | mnemos.agents.lint | Validates YAML front-matter and Markdown formatting |
| Query | mnemos.agents.query | Answers questions using search + memory-read |

## Agent Interfaces

All agents expose a `run()` method and may accept a `run_id` or `session_id`
parameter to scope ephemeral/working memory.

### Ingest Agent

```python
from mnemos.agents.ingest import IngestAgent
agent = IngestAgent(gateway)
agent.run(source_dir="repo/raw/sources", run_id="run-001")
```

### Writer Agent

```python
from mnemos.agents.writer import WriterAgent
agent = WriterAgent(gateway)
agent.run(topic="memory-lifecycle", run_id="run-001")
```

### Linker Agent

```python
from mnemos.agents.linker import LinkerAgent
agent = LinkerAgent(gateway)
agent.run()
```

### Contradiction Agent

```python
from mnemos.agents.contradiction import ContradictionAgent
agent = ContradictionAgent(gateway)
agent.run()
```

### Lint Agent

```python
from mnemos.agents.lint import LintAgent
agent = LintAgent(gateway)
agent.run()
```

### Query Agent

```python
from mnemos.agents.query import QueryAgent
agent = QueryAgent(gateway)
result = agent.run(question="What is the memory lifecycle?", run_id="run-001")
```

## Memory Layer Ownership

| Layer | Path | Managed By |
|---|---|---|
| Ephemeral | .agent/runs/{runId}/scratch/ | Ingest, Query |
| Working | .agent/runs/{runId}/working/ | Writer, Query |
| Session | .agent/sessions/{sessionId}/ | All agents |
| Project | wiki/projects/ | Writer, Linker |
| Global | wiki/global/ | Writer, Linker |
