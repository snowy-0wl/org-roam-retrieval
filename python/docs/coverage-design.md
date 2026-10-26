# Extractor coverage and generated-artifact policy

Status: implementation design, 2026-10-26. Scope is extraction fidelity and truthful
coverage, not a new retrieval engine or embedding profile.

## Classification and source safety

Classify each discovered local source before OCR or extraction. Org LaTeX preview
images are generated artifacts: a known `org-ltximg_<hex>`/`org-ltximg-<hex>` basename
is sufficient structural evidence; a generic small PNG, an arbitrary `ltximg`
folder image, or a filename mentioning mathematics is not. Generated preview
sources, including previews previously OCR'd and embedded, leave the active
source/chunk/FTS index. Original Org LaTeX fragments and environments remain in
Org's searchable source prose. No source file is changed or deleted.

Exclude well-known temporary editor/TeX build artifacts and cache/build directory
contents by structure. Do not classify arbitrary binaries as prose. Unsupported
formats remain filename-only with an explicit unsupported reason; extraction
failures remain errors; intentional exclusions have their own derived ledger.

## New content extraction

- `.org_archive` uses the same isolated Emacs Org parser as `.org`.
- HTML uses Python's HTML parser, preserving meaningful text and heading/title
  context while removing script/style/navigation/template noise. It never executes
  JavaScript or loads external resources.
- EPUB follows its container/OPF manifest and reading-order spine. Each extracted
  passage has `epub-section` location with an exact archive-member `href`,
  zero-based `spine_index`, and an authored fragment ID when available. The parser
  rejects remote/traversing member references and enforces member/total expansion
  budgets, entry-count and compression-ratio limits. It does not unpack files.
- LaTeX is source text: comments and preamble commands are filtered, section/prose
  and math source remain searchable, and TeX/include/write/shell commands are never
  executed. Locations use the source file and actual one-based line.
- BibTeX extracts balanced entries into citation metadata plus source field text,
  with a truthful file/line locator and citation key. It does not resolve URLs.
- PDF encryption support installs pypdf's crypto extra. Empty opening passwords are
  tried once as supported by pypdf; genuine password-protected files stay errors.

## Provenance and incremental migration

Add a versioned per-format extraction-policy field to each source and an exclusion
ledger to the derived database. The additive schema migration infers the old
policy for existing formats; previously supported Org/text/PDF/image/office sources
keep their chunks and vectors when their policy did not change. Newly supported
formats previously indexed as filenames have a different policy and are re-extracted
automatically on reconcile. Each extractor has its own policy version. Org moves to `org-parser-2` to retain
LaTeX environments as well as inline fragments; unchanged section embedding inputs
reuse their cached vectors. OCR configuration provenance remains recorded in details;
this change does not force all PDFs/images through OCR again.

Reconciliation applies exclusion rules to every candidate, regardless of previous
coverage. It removes excluded active chunks and FTS entries while retaining orphan
embedding-cache values, which cannot appear as search results without live chunks.
Re-extraction reuses existing content-addressed embeddings for unchanged embedding
inputs. No whole-library repair or destructive cache reset is required.

## Coverage contract

Keep `chunks` as the backward-compatible count of all active chunks. Add:

- `eligible_chunks`: active content chunks eligible for the active embedding profile.
- `embedded_chunks`: eligible chunks with a vector for that exact profile.
- `pending_embedding_chunks`: `eligible_chunks - embedded_chunks`.
- `filename_only_sources` and `filename_only_chunks`: explicitly non-content coverage.
- `unsupported_sources`: filename-only sources whose format has no content extractor.
- `excluded_sources` and `excluded_by_reason`: derived exclusion-ledger counts.
- `error_sources`: failed extraction sources, separate from unsupported/excluded.

A completed embedding progress fraction is `embedded_chunks / eligible_chunks`;
filename-only and excluded artifacts cannot make completion mathematically impossible.
All format coverage and exclusion reasons are inspectable in source metadata.

## Verification

Use isolated fixtures for Unicode HTML/EPUB/LaTeX/BibTeX, archive headings and IDs,
actual EPUB spine references, formula-source retention, malformed/encrypted inputs,
ZIP expansion/path protections, and false-positive exclusions. An additive migration
fixture starts with the old schema, preserves a pre-embedded unchanged text source,
upgrades filename-only newly supported formats, and removes a previously embedded
Org preview without deleting its file. No full-corpus/model job runs in these tests.
