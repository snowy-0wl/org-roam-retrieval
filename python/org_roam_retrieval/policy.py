"""Versioned extraction and generated-source classification policies."""
from pathlib import Path
import re

SCOPE_POLICY = "generated-artifacts-2"
ORG_EXTENSIONS = {".org", ".org_archive"}


def exclusion_reason(path: str) -> str | None:
    """Classify structural generated artifacts, never image dimensions/content."""
    file = Path(path)
    name = file.name.lower()
    if re.fullmatch(r"org-ltximg[_-][a-f0-9]{8,64}(?:[_-][0-9]+)?\.(?:png|svg|jpe?g|gif|webp)", name):
        return "org-latex-preview"
    if ("ltximg" in {part.lower() for part in file.parts[:-1]} and
            re.fullmatch(r"ltximg[_-][a-f0-9]{8,64}\.(?:png|svg)", name)):
        return "org-latex-preview"
    if any(part in {"node_modules", "__pycache__"} or part.startswith("_minted-") for part in file.parts[:-1]):
        return "generated-build-directory"
    if name.endswith("~") or (name.startswith("#") and name.endswith("#")):
        return "editor-backup-or-autosave"
    if file.suffix.lower() in {".pyc", ".elc", ".o", ".class", ".swp", ".swo", ".tmp"}:
        return "compiled-or-temporary"
    tex_output = {".aux", ".bbl", ".blg", ".toc", ".out", ".fls", ".bcf", ".nav", ".snm", ".vrb", ".log"}
    if name.endswith((".fdb_latexmk", ".synctex.gz", ".run.xml")):
        return "latex-build-output"
    if file.suffix.lower() in tex_output and file.with_suffix(".tex").is_file():
        return "latex-build-output"
    return None


_POLICY_FORMATS = {
    "org-parser-2": ORG_EXTENSIONS,
    "plain-text-1": {".txt", ".text", ".rst", ".csv", ".tsv"},
    "markdown-1": {".md", ".markdown"},
    "html-prose-1": {".html", ".htm", ".xhtml"},
    "epub-spine-1": {".epub"},
    "latex-source-1": {".tex", ".latex"},
    "bibtex-source-1": {".bib"},
    "pdf-text-ocr-1": {".pdf"},
    "office-xml-1": {".docx", ".odt"},
    "image-ocr-1": {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"},
    "xlsx-1": {".xlsx", ".xlsm"},
}
POLICIES = {suffix: policy for policy, suffixes in _POLICY_FORMATS.items() for suffix in suffixes}
CURRENT_CONTENT_POLICIES = tuple(sorted(_POLICY_FORMATS))


def extraction_policy(path: str) -> str:
    return POLICIES.get(Path(path).suffix.lower(), "filename-1")


def legacy_extraction_policy(path: str) -> str:
    """Infer v1's supported formats without re-extracting unchanged content."""
    if Path(path).suffix.lower() == ".org":
        return "org-parser-1"
    if Path(path).suffix.lower() in {".org_archive", ".html", ".htm", ".xhtml", ".epub", ".tex", ".latex", ".bib"}:
        return "filename-1"
    return extraction_policy(path)
