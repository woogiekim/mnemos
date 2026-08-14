# Optional QMD Retrieval

Mnemos remains the canonical Markdown store, lifecycle policy layer, stable-ID
owner, and recall filter. QMD is an optional local derived index behind the
existing FTS -> vector -> grep retrieval pipeline. A QMD failure cannot roll
back a canonical memory mutation.

QMD is not installed by default. Mnemos can install and prepare it only when the
operator explicitly requests `--with-qmd`; its current upstream requirements,
commands, config schema, and model behavior are documented in the
[official QMD README](https://github.com/tobi/qmd/blob/main/README.md). In
particular, current QMD requires Node.js 22+ or Bun, and model-backed commands
can download GGUF files from Hugging Face on first use.

## Default: model-free QMD search

Install QMD only after explicitly approving the network and global-package
changes. Mnemos can run the model-free bootstrap for you:

```bash
mnemos install --with-qmd .
# or, after an existing install:
mnemos update --with-qmd
```

The bootstrap installs `@tobilu/qmd` with `npm` or `bun` when `qmd` is missing,
enables `retrieval.qmd` in `mnemos.yml`, and runs `mnemos qmd-prepare --json`.
It does not embed models.

Manual installation is still supported:

```bash
npm install -g @tobilu/qmd
# or
bun install -g @tobilu/qmd
```

Enable the optional backend in `mnemos.yml`:

```yaml
retrieval:
  qmd:
    enabled: true
    executable: qmd
    index_name: mnemos
    mode: search
    timeout_seconds: 15
    update_timeout_seconds: 120
    embed_on_update: false
    model_ready: false
```

`mode: search` is QMD's BM25-only mode and is the Mnemos bootstrap default
because it does not require model work. Prepare the repo-local QMD config
without invoking QMD:

```bash
mnemos qmd-prepare --json
```

The generated config and index cache are isolated under:

```text
.agent/state/qmd/config/{index_name}.yml
.agent/state/qmd/cache/
```

Mnemos owns those directory boundaries; the filenames QMD creates inside its
cache remain an upstream implementation detail.

The generated QMD collection excludes `**/domain-*.md` by default. Those files
are generated domain-distillation summaries and can explode into very large
embedding batches; canonical search can still reach them through FTS/grep.

Canonical capture, update, classification, promotion, demotion, archive, and
deletion enqueue content-free jobs under `.agent/state/qmd-refresh/`. A
detached one-shot worker coalesces each claimed batch into one `qmd update`.
Foreground capture does not wait for QMD.

## Semantic and hybrid modes

For Korean/CJK embeddings, Mnemos accepts an explicit Qwen3 model URI:

```yaml
retrieval:
  qmd:
    enabled: true
    mode: vsearch
    embed_model: hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf
    embed_on_update: false
    model_ready: false
```

Keep `model_ready: false` while preparing models. Mnemos will return
`model_not_ready` and preserve FTS/grep results without launching a
model-backed QMD process.

After `mnemos qmd-prepare`, explicitly perform the approved model operation in
the same repo-local QMD directories:

```bash
export QMD_CONFIG_DIR="$MNEMOS_REPO_ROOT/.agent/state/qmd/config"
export XDG_CACHE_HOME="$MNEMOS_REPO_ROOT/.agent/state/qmd/cache"

qmd --index mnemos update
qmd --index mnemos embed
```

Those QMD commands may download models. Set `model_ready: true` only after they
complete. `mode: vsearch` then enables semantic retrieval. Mnemos invokes this
as a typed `qmd query` vector query with `--no-rerank` so recall does not
implicitly download QMD's generation or reranking models. `mode: query` uses
QMD's full query expansion and reranking path; pre-run an explicit `qmd query`
under the same environment before attesting readiness because it can require
additional models. Set `embed_on_update: true` only when automatic background
embedding after each coalesced index refresh is desired.

Mnemos currently invokes QMD through its CLI adapter. The UserPrompt hook does
not wait for that process: it returns a fresh context cache when available and
starts the next context prefetch in a detached background process. A slow first
model load can therefore delay cache freshness for a later prompt, but it does
not extend the foreground hook. If a `qmd embed` process is already active for
the same index, model-backed recall reports `busy` and uses canonical
FTS/grep fallbacks instead of waiting on a competing QMD model process. QMD
also documents a shared HTTP MCP transport that keeps models loaded across
requests; that transport is not used or installed by this integration.

Environment overrides are available for automation:

```text
MNEMOS_QMD_ENABLED
MNEMOS_QMD_EXECUTABLE
MNEMOS_QMD_INDEX
MNEMOS_QMD_MODE
MNEMOS_QMD_TIMEOUT_SECONDS
MNEMOS_QMD_UPDATE_TIMEOUT_SECONDS
MNEMOS_QMD_EMBED_MODEL
MNEMOS_QMD_EMBED_ON_UPDATE
MNEMOS_QMD_MODEL_READY
```

## Diagnostics and recovery

Inspect queue state without consuming it:

```bash
mnemos qmd-index-worker --status --json
```

The response reports `pending`, `processing`, `failed`, `done`, oldest pending
age, and worker state. Search diagnostics report `missing`, `busy`, `timeout`,
`invalid_output`, `model_not_ready`, or `stale` while retaining canonical
fallback results. Diagnostics contain counts and error classes/codes, not full
memory content or queries.

Run an explicit foreground drain when debugging:

```bash
mnemos qmd-index-worker --json
```

After correcting QMD config, executable, or model readiness, requeue terminal
failures and signal a detached worker:

```bash
mnemos qmd-index-worker --retry-failed --json
```

Deleting `.agent/state/qmd/` only discards derived QMD state; it does not delete
canonical Mnemos Markdown. Re-run `mnemos qmd-prepare`, then enqueue or manually
run the worker to rebuild. Do not delete `.agent/state/qmd-refresh/` while jobs
are pending unless intentionally abandoning index-consistency work.

## Offline evaluation

The tracked 30-case Korean paraphrase fixture is deterministic and does not
invoke QMD or download a model:

```bash
mnemos qmd-evaluate \
  --fixture benchmarks/qmd-korean-paraphrases.json \
  --json
```

It measures labelled ranking `Recall@5`, `MRR`, and nearest-rank p95 latency
inputs for a synthetic adapter contract. The tracked report is
[`benchmarks/qmd-korean-paraphrases-report.json`](../benchmarks/qmd-korean-paraphrases-report.json).
These numbers prove metric plumbing and low-token-overlap handling only. They
are not a live QMD, Qwen3, hardware, or production-latency claim. A live model
benchmark remains a separate, explicitly approved run on the target machine.
