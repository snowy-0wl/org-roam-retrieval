"""Inert prose extraction for HTML, EPUB, LaTeX and bibliography sources."""
from __future__ import annotations
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile
import xml.etree.ElementTree as ET

MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096


class ProseHTML(HTMLParser):
    """Read visible prose without loading, evaluating, or following resources."""
    OMIT = {"script", "style", "nav", "header", "footer", "template", "noscript", "svg", "form"}
    BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.title = []
        self.first_heading = []
        self.fragment = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        omitted = (tag in self.OMIT or "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true"
                   or attrs.get("role", "").lower() in {"navigation", "banner", "contentinfo"})
        style = re.sub(r"\s+", "", attrs.get("style", "").lower())
        omitted = omitted or "display:none" in style or "visibility:hidden" in style
        if tag not in {"br", "hr", "meta", "link", "img", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            self.stack.append((tag, omitted))
        if tag in self.BLOCK and not any(value for _, value in self.stack):
            self.text.append("\n")
        if tag in {"h1", "h2", "h3"} and self.fragment is None and attrs.get("id"):
            self.fragment = attrs["id"]
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in self.BLOCK and not any(value for _, value in self.stack):
            self.text.append("\n")
    def handle_data(self, data):
        if any(value for _, value in self.stack):
            return
        tags = {tag for tag, _ in self.stack}
        if "title" in tags:
            self.title.append(data)
        elif "head" not in tags:
            self.text.append(data)
    def result(self):
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.text))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text, " ".join("".join(self.title).split())


def html_prose(raw: str) -> tuple[str, str, str | None]:
    parser = ProseHTML()
    parser.feed(raw)
    parser.close()
    text, title = parser.result()
    return text, title, parser.fragment


def html_document(path: str) -> dict:
    file = Path(path)
    text, title, _ = html_prose(file.read_text(encoding="utf-8", errors="replace"))
    return {"path": path, "title": title or file.stem,
            "segments": [{"title": title or file.stem, "text": text, "locator": {"kind": "file"}}],
            "coverage": "text", "details": {"extractor": "html-prose-1", "scripts_executed": False}}


def safe_member(base: str, href: str) -> tuple[str, str | None]:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or "\\" in href:
        raise ValueError("EPUB member reference must be a local archive path")
    decoded = unquote(parsed.path)
    if decoded.startswith("/"):
        raise ValueError("EPUB absolute archive path is forbidden")
    member = posixpath.normpath(posixpath.join(base, decoded))
    if member in {".", ".."} or member.startswith("../"):
        raise ValueError("EPUB member reference escapes the archive root")
    return member, unquote(parsed.fragment) or None


def epub_document(path: str) -> dict:
    file = Path(path)
    with zipfile.ZipFile(file) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("EPUB exceeds archive entry/expansion budget")
        if any(entry.file_size > MAX_MEMBER_BYTES or entry.file_size > max(1, entry.compress_size) * 500 for entry in entries):
            raise ValueError("EPUB member exceeds expansion/ratio budget")
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("EPUB duplicate archive member names are ambiguous")
        consumed = set()
        def read(member):
            if member not in archive.namelist():
                raise ValueError(f"EPUB declared member is absent: {member}")
            consumed.add(member)
            return archive.read(member)
        def xml_document(raw):
            declarations = raw.replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
                raise ValueError("EPUB package XML declarations/entities are unsupported")
            return ET.fromstring(raw)
        container = xml_document(read("META-INF/container.xml"))
        rootfiles = [element for element in container.iter() if element.tag.rsplit("}", 1)[-1] == "rootfile"]
        if not rootfiles:
            raise ValueError("EPUB container declares no package document")
        package_member, _ = safe_member("", rootfiles[0].attrib.get("full-path", ""))
        package = xml_document(read(package_member))
        base = posixpath.dirname(package_member)
        titles = ["".join(element.itertext()).strip() for element in package.iter() if element.tag.rsplit("}", 1)[-1] == "title"]
        title = next((value for value in titles if value), file.stem)
        manifest = {element.attrib.get("id"): element.attrib for element in package.iter() if element.tag.rsplit("}", 1)[-1] == "item"}
        spine = [element for element in package.iter() if element.tag.rsplit("}", 1)[-1] == "itemref"]
        segments = []
        empty_items = []
        for index, itemref in enumerate(spine):
            item = manifest.get(itemref.attrib.get("idref"))
            if not item:
                raise ValueError("EPUB spine references a missing manifest item")
            if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                raise ValueError("EPUB spine contains an unsupported non-HTML reading-order document")
            member, fragment = safe_member(base, item.get("href", ""))
            raw = read(member).decode("utf-8", errors="replace")
            text, chapter_title, heading_fragment = html_prose(raw)
            if text:
                locator = {"kind": "epub-section", "href": member, "spine_index": index}
                if fragment or heading_fragment:
                    locator["fragment"] = fragment or heading_fragment
                segments.append({"text": text, "title": f"{title} / {chapter_title}" if chapter_title else title, "locator": locator})
            else:
                empty_items.append(member)
        if not spine:
            raise ValueError("EPUB package declares no reading-order spine")
        coverage = "partial-text" if empty_items else "text"
        if not segments:
            coverage = "filename-only"
            segments = [{"text": title, "title": title, "locator": {"kind": "file"}}]
        return {"path": path, "title": title, "segments": segments,
                "coverage": coverage, "details": {"extractor": "epub-spine-1", "spine_items": len(spine),
                "text_items": len(spine) - len(empty_items), "empty_spine_items": empty_items}}


def latex_document(path: str) -> dict:
    file = Path(path)
    raw = file.read_text(encoding="utf-8", errors="replace")
    # Treat commands as inert source, never expand macros, include files or run TeX.
    without_comments = re.sub(r"(?m)(?<!\\)%[^\n]*", "", raw)
    begin = re.search(r"\\begin\s*\{document\}", without_comments)
    start = begin.end() if begin else 0
    end = re.search(r"\\end\s*\{document\}", without_comments[start:])
    body = without_comments[start:start + end.start()] if end else without_comments[start:]
    title_match = re.search(r"\\title\s*\{([^}]*)\}", without_comments)
    title = title_match.group(1) if title_match else file.stem
    lines = body.splitlines()
    first_line = without_comments[:start].count("\n") + 1
    prose = []
    for line in lines:
        if re.match(r"\s*\\(?:documentclass|usepackage|include|input|write18|openout|write|bibliography|bibliographystyle)\b", line):
            prose.append("")
        else:
            prose.append(line)
    return {"path": path, "title": title, "segments": [{"title": title, "text": "\n".join(prose),
            "locator": {"kind": "text-range", "line": first_line}}], "coverage": "text",
            "details": {"extractor": "latex-source-1", "commands_executed": False, "math_source_retained": True}}


def bibtex_document(path: str) -> dict:
    file = Path(path)
    text = file.read_text(encoding="utf-8", errors="replace")
    segments = []
    for match in re.finditer(r"(?m)^\s*@([A-Za-z]+)\s*([\{(])\s*([^,\s]+)\s*,", text):
        kind, opening, key = match.group(1).lower(), match.group(2), match.group(3)
        if kind in {"comment", "preamble", "string"}:
            continue
        closing = "}" if opening == "{" else ")"
        depth, quoted, escaped = 1, False, False
        end = match.end()
        for end in range(match.end(), len(text)):
            character = text[end]
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                quoted = not quoted
            if not quoted:
                if character == opening:
                    depth += 1
                elif character == closing:
                    depth -= 1
                    if not depth:
                        break
        if depth:
            raise ValueError(f"Unclosed BibTeX entry: {key}")
        entry = text[match.end():end]
        entry_title = re.search(r"(?is)\btitle\s*=\s*(?:\{([^\n]+)\}|\"([^\"]+)\")", entry)
        title = (entry_title.group(1) or entry_title.group(2)).strip().rstrip(",") if entry_title else key
        segments.append({"title": f"{key} / {title}", "text": f"{key}\n{entry}",
                         "locator": {"kind": "text-range", "line": text[:match.start() + match.group(0).index("@")].count("\n") + 1},
                         "citation_key": key})
    return {"path": path, "title": file.stem, "segments": segments, "coverage": "text",
            "details": {"extractor": "bibtex-source-1", "entries": len(segments)}}
