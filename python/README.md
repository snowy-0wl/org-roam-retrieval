# Org-roam managed retrieval worker

This directory is the external process owned by the Org-roam extension. Its
SQLite database is derived state, separate from `org-roam.db`. It never changes
source files or the graph database.

## Setup

The Emacs setup command provisions the environment using the committed lock:

```sh
UV_PROJECT_ENVIRONMENT=/path/to/runtime/venv uv sync --project /path/to/org-roam/python --frozen --extra embedding --python 3.12
```

Ordinary operation is offline. The pinned EmbeddingGemma 2 snapshot must be in
the local Hugging Face cache. Check it without downloading:

```sh
/path/to/runtime/venv/bin/python -m org_roam_retrieval.model_setup
```

Explicitly provision it when needed:

```sh
/path/to/runtime/venv/bin/python -m org_roam_retrieval.model_setup --download
```

Cache presence is not proof of inference readiness. Actual loading and numerical
checks happen in the model executor. An absent or incompatible model leaves
lexical search available with an explicit degraded status.

## Process and protocol

Run `python -m org_roam_retrieval`. Input and output are UTF-8 newline-delimited
JSON-RPC 2.0. Logs go to stderr. The input limit is 4 MiB per message. The Emacs
client performs one `initialize` handshake with database, roots, embedding
profile, Emacs executable, and Org extraction helper paths.

Methods are `initialize`, `search`, `reconcile`, `status`, `repair`, `cancel`, and
`shutdown`. `reconcile` returns a job ID; progress arrives as notifications.
Search returns lexical results promptly. When query embedding is needed it
returns `semantic_pending: true` and later emits `search-update` with the
original `request_id` and full `result`. Closing/cancelling the query suppresses
late semantic updates.

One scheduler owns SQLite writes and native vector-cache generations. A separate
single-model executor owns lazy model loading and inference. A bounded
single-extractor executor owns Org subprocesses, PDF/XLSX/office extraction,
and OCR; completions are revision-checked on the state scheduler. Priority query
requests precede the next background embedding batch. Neither model loading nor
inference blocks status or lexical search. In-flight inference may finish after
cancellation, but cancelled query results are not emitted.

The worker reconciles roots on initialization and every idle minute. It coalesces
in-flight source revisions, and periodic scans do not multiply unfinished
extraction queues. Embeddings begin after incremental commits, without waiting
for an entire image/PDF library to finish. Emacs sends
coalesced saved-file invalidations between reconciliations. Directory/file
symlinks and hidden entries are excluded. Missing roots are reported and retain
stale-tagged cached results. Removing a root from configuration removes its
sources from the derived index on reconciliation.

## Retrieval and coverage

Org prose and exact heading identities come from the clean Emacs Org parser.
Text and Markdown are chunked without a whole-file cap. DOCX/ODT paragraph text
is searchable with a truthful file locator (not a fabricated page number). PDFs retain page
locations and report empty/unextracted pages. XLSX retains sheet/cell ranges.
Images and scanned PDF pages use optional local Tesseract OCR; PDF rasterization
uses `pdftoppm`. Both run in the extraction executor with timeouts and private
spool files. Available English/Hebrew/Greek language packs are detected and
reported. Missing tools, absent language packs, failed pages, and images with no
recognized text remain explicitly filename-only or partial. OCR text is
searchable but is not visual semantic embedding; photos without text currently
have filename-only coverage. Unsupported formats are explicitly filename-only.

Lexical candidates use FTS5 BM25. Semantic candidates use a persistent normalized
float32 matrix and native NumPy dot products, never Elisp vector objects. Root
filters and best-passage grouping apply before the candidate cap. Hybrid search
uses reciprocal rank fusion, keeping BM25 and cosine scales separate. A source
cannot occupy the whole list through duplicate overlapping chunks.

Related-note discovery currently uses the source as a search query. This is
explicitly not a symmetric similarity metric. Corpus vectors use titled-document
formatting; query vectors use the model's `SearchQuery` prompt. Long source
snapshots use every bounded window and a normalized query centroid. No input is
silently truncated to fit the model. Finite values, exact dimensions, nonzero
norms, and unit normalization are checked before storage/scoring.

Embedding cache identity includes an immutable model revision, task policy,
dimension, precision, and normalization. Changing the profile does not reuse
incompatible vectors. Changed chunks become lexical immediately and receive
new vectors in the background; deleted chunks cannot be resurrected by a late
embedding completion. `repair` offers a dry-run plan and rebuilds derived
sources/chunks while preserving embedding cache, source files, and the graph.

## Verification

```sh
python -m unittest discover -s tests -v
python evaluate.py --device cpu --runs 5 --output evaluation-cpu.json
```

The unit/integration suite uses deterministic embeddings and the actual Org
extraction helper. It covers native scoring, malformed vectors/profiles,
Unicode, query syntax, self-exclusion, root filtering, grouping, incremental
updates/deletion, PDF/XLSX locations, cancellation, repair/restart, and status
responsiveness while inference is deliberately blocked.

The evaluator uses real local model inference on synthetic English/Hebrew/Greek
and cross-language examples, including same-vocabulary negatives. It compares
lexical and semantic retrieval and checks dimensions, finiteness, normalization,
and expected ranks. These fixtures are engineering gates; they are not a
substitute for user-judged corpus relevance. Cached timings are labeled and are
not reported as uncached model latency or as whole-editor benchmarks.

PDFs that open with an empty user password are readable, as in ordinary PDF
viewers. A PDF that requires a password is reported separately; the indexer
does not guess passwords or modify source files.

## Extraction coverage policy

The additive schema-2 migration records per-format extraction provenance and a
separate exclusion ledger. Reconciliation upgrades newly supported formats and
removes generated artifacts from active results without deleting authored files,
resetting the library, or discarding content-addressed vector cache entries.
Unchanged passage inputs reuse their existing vectors.

Org archives use the same clean Org parser. HTML excludes executable/navigation
noise. EPUB follows reading-order spine members and returns real `epub-section`
archive locations. EPUB extraction never unpacks or runs content and rejects
remote/traversing references, entities, oversized/over-compressed archives, and
ambiguous duplicate members. LaTeX is inert searchable prose/math source; no TeX,
macro expansion, includes, or shell commands run. BibTeX preserves citation keys,
field text, and actual source lines. The pypdf crypto extra supports AES PDFs that
open with an empty password; protected PDFs remain explicit errors.

Generated `org-ltximg_<hash>` previews and recognized editor/TeX build artifacts
are intentionally excluded, including previously OCR'd previews. Small authored
images are retained. Original Org inline/display LaTeX remains searchable under
`org-parser-2`. Unsupported binaries remain filename-only with an unsupported
reason, separate from failures and exclusions.

Progress uses `embedded_chunks / eligible_chunks`, restricted to active-profile,
current-extraction-policy content. `chunks` remains the count of all active chunks.
`filename_only_sources`, `filename_only_chunks`, `unsupported_sources`,
`excluded_sources`, `excluded_by_reason`, `error_sources`, and
`pending_extraction_sources` describe the remainder honestly. Pending extraction
and embedding are distinct stages; filename-only or excluded files cannot create
an impossible embedding completion denominator.

See `docs/coverage-design.md` and `tests/test_coverage.py` for the design and
source-safe migration/format fixtures. Activating this upgrade requires syncing
the frozen environment, restarting the managed worker, and reconciling normally;
a destructive repair is not needed.

## Integration contracts

The [contract reference](../doc/org-roam-retrieval-contracts.org) covers the
Emacs/worker boundary, cancellation, readiness, source safety, locations, and
cache lifecycle. The [design explanation](../doc/org-roam-retrieval-design.org)
explains why the editor and managed worker own different responsibilities.
