#!/usr/bin/env python3
"""Local Markdown visualizer for md-view.

Usage:
  md-view <markdown-file> [<markdown-file> ...] [--port PORT] [--browser firefox|none]

This server binds to 127.0.0.1 only. It renders Markdown server-side, serves all
browser assets from the repo-local assets directory by default, and uses
Server-Sent Events to update the page whenever the selected source file's mtime
or size changes.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

import markdown as markdown_lib
import xml.etree.ElementTree as etree
from latex2mathml.converter import convert as latex_to_mathml
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.preprocessors import Preprocessor
from pygments.formatters import HtmlFormatter


APP_NAME = "md-view"
DEFAULT_PORT = 8765
POLL_INTERVAL_SECONDS = 0.7
HEARTBEAT_SECONDS = 15.0
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
REPO_ROOT = Path(__file__).resolve().parents[2]


HELP_TEXT = f"""\
{APP_NAME} - local offline Markdown visualizer

Usage:
  md-view <markdown-file> [<markdown-file> ...] [--port PORT] [--browser firefox|none]

Examples:
  md-view README.md
  md-view README.md CHANGELOG.md docs/design.md
  md-view "notes with spaces.md" --port 9000 --browser firefox
  md-view design.md --browser none

Features:
  - Markdown: headings, lists, tables, links, blockquotes, fenced code
  - Syntax highlighting: server-side Pygments for fenced code blocks
  - Mermaid: fenced ```mermaid blocks rendered by local/offline JavaScript
  - Math: $inline$, $$block$$, \\(...\\), and \\[...\\] rendered to MathML
  - Live reload: local SSE stream watches selected file mtime/size changes on disk

No CDN URLs are used at runtime. Static browser assets are served locally from:
  <repo>/assets/ by default, or MD_VIEW_HOME/assets when MD_VIEW_HOME is set.
"""


BASE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f7f7f8;
  --fg: #1f2328;
  --muted: #667085;
  --panel: #ffffff;
  --border: #d0d7de;
  --accent: #0969da;
  --code-bg: #f6f8fa;
  --shadow: 0 12px 28px rgba(27, 31, 36, 0.10);
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --muted: #8b949e;
    --panel: #161b22;
    --border: #30363d;
    --accent: #58a6ff;
    --code-bg: #0d1117;
    --shadow: 0 12px 28px rgba(0, 0, 0, 0.35);
  }
}

* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  line-height: 1.6;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  gap: 1rem;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(10px);
}

.title-group {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.08rem;
}

.title {
  min-width: 0;
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.current-path {
  min-width: 0;
  color: var(--muted);
  font-size: 0.78rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status {
  flex: 0 0 auto;
  color: var(--muted);
  font-size: 0.875rem;
}

.doc-list {
  position: sticky;
  top: 3.8rem;
  z-index: 4;
  display: flex;
  gap: 0.5rem;
  padding: 0.6rem 1rem;
  overflow-x: auto;
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(10px);
}

.doc-button {
  flex: 0 0 auto;
  max-width: 18rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.32rem 0.72rem;
  background: var(--panel);
  color: var(--fg);
  cursor: pointer;
  font: inherit;
  font-size: 0.88rem;
}

.doc-button:hover,
.doc-button.active {
  border-color: var(--accent);
  color: var(--accent);
}

.doc-button.active {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}

main {
  width: min(1040px, calc(100vw - 2rem));
  margin: 1.25rem auto 3rem;
  padding: 2rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
}

@media (max-width: 700px) {
  main { width: 100%; margin: 0; border-left: 0; border-right: 0; border-radius: 0; padding: 1rem; }
}

a { color: var(--accent); }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.4em 0 0.55em; }
h1:first-child, h2:first-child, h3:first-child { margin-top: 0; }
h1 .headerlink, h2 .headerlink, h3 .headerlink,
h4 .headerlink, h5 .headerlink, h6 .headerlink {
  margin-left: 0.35em;
  opacity: 0;
  text-decoration: none;
  font-size: 0.82em;
}
h1:hover .headerlink, h2:hover .headerlink, h3:hover .headerlink,
h4:hover .headerlink, h5:hover .headerlink, h6:hover .headerlink,
.headerlink:focus { opacity: 0.75; }
.toc {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
  background: color-mix(in srgb, var(--code-bg) 55%, transparent);
}
p, ul, ol, blockquote, table, pre { margin-top: 0; margin-bottom: 1rem; }
hr { border: 0; border-top: 1px solid var(--border); margin: 2rem 0; }
blockquote { border-left: 4px solid var(--border); color: var(--muted); padding-left: 1rem; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--border); padding: 0.35rem 0.6rem; }
th { background: color-mix(in srgb, var(--code-bg) 85%, transparent); }

code, pre, kbd, samp {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas,
    "Liberation Mono", "Courier New", monospace;
}

:not(pre) > code {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 0.3rem;
  padding: 0.08rem 0.28rem;
}

pre, .codehilite {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: auto;
}

pre { padding: 1rem; }
.codehilite pre { border: 0; margin: 0; }

.math { overflow-x: auto; }
.math-block { margin: 1rem 0; text-align: center; }
.math-inline math { display: inline; }
.math-error {
  color: #b42318;
  background: #fee4e2;
  border: 1px solid #fda29b;
  border-radius: 0.3rem;
  padding: 0.08rem 0.28rem;
}

.mermaid {
  margin: 1.25rem 0;
  padding: 1rem;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow-x: auto;
  text-align: center;
}

.mermaid svg { max-width: 100%; height: auto; }
.mermaid-error { color: #b42318; white-space: pre-wrap; text-align: left; }

.empty, .error-panel {
  border: 1px dashed var(--border);
  border-radius: 12px;
  padding: 1rem;
  color: var(--muted);
}
"""


def _is_fence_start(line: str) -> re.Match[str] | None:
    return re.match(r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<rest>.*)$", line)


def _is_matching_fence(line: str, fence: str) -> bool:
    marker = re.escape(fence[0]) + "{" + str(len(fence)) + ",}"
    return re.match(r"^[ \t]{0,3}" + marker + r"[ \t]*$", line) is not None


class MermaidPreprocessor(Preprocessor):
    """Stash ```mermaid fenced blocks as raw HTML before fenced_code runs."""

    def run(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            match = _is_fence_start(line)
            if not match:
                out.append(line)
                i += 1
                continue

            info = match.group("rest").strip().lower()
            info_parts = info.strip("{}").lstrip(".").split(maxsplit=1)
            normalized = info_parts[0] if info_parts else ""
            if normalized != "mermaid":
                out.append(line)
                i += 1
                continue

            fence = match.group("fence")
            body: list[str] = []
            i += 1
            while i < len(lines) and not _is_matching_fence(lines[i], fence):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1

            source = "\n".join(body).strip("\n")
            raw_html = (
                '<div class="mermaid" data-md-view-mermaid="1">'
                + html.escape(source)
                + "</div>"
            )
            out.append("")
            out.append(self.md.htmlStash.store(raw_html))
            out.append("")
        return out


class MathPreprocessor(Preprocessor):
    """Render TeX math outside fenced code blocks to MathML."""

    def run(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        pending: list[str] = []
        i = 0

        def flush_pending() -> None:
            nonlocal pending
            if pending:
                out.extend(convert_math_text("\n".join(pending)).split("\n"))
                pending = []

        while i < len(lines):
            line = lines[i]
            match = _is_fence_start(line)
            if not match:
                pending.append(line)
                i += 1
                continue

            flush_pending()
            fence = match.group("fence")
            out.append(line)
            i += 1
            while i < len(lines):
                out.append(lines[i])
                if _is_matching_fence(lines[i], fence):
                    i += 1
                    break
                i += 1

        flush_pending()
        return out


# Match bare http/https URLs in text nodes.
# - Requires http:// or https:// prefix.
# - Allows parentheses in URL paths (common in Wikipedia links) as balanced pairs.
# - Strips trailing punctuation that is unlikely to be part of the URL.
_BARE_URL_RE = (
    r"(https?://"
    r"(?:[^\s<>\[\]()\"\']|\((?:[^\s<>\[\]()\"\'])*\))+"
    r"(?<![.,;:!?'\"]))"
)


class UrlAutolinkProcessor(InlineProcessor):
    """Convert bare http/https URLs in Markdown text to clickable <a> elements."""

    def handleMatch(  # noqa: N802 - markdown API name
        self, m: re.Match[str], data: str
    ) -> tuple[etree.Element, int, int]:
        url = m.group(1)
        el = etree.Element("a")
        el.set("href", url)
        el.set("target", "_blank")
        el.set("rel", "noopener noreferrer")
        el.text = url
        return el, m.start(0), m.end(0)


class MdViewExtension(Extension):
    def extendMarkdown(self, md: markdown_lib.Markdown) -> None:  # noqa: N802 - markdown API name
        # Run after Markdown's normalize_whitespace preprocessor (priority 30)
        # so htmlStash control markers survive, but before fenced_code handles
        # the remaining non-Mermaid code fences.
        md.preprocessors.register(MermaidPreprocessor(md), "md_view_mermaid", 29)
        md.preprocessors.register(MathPreprocessor(md), "md_view_math", 28)
        # Autolink bare http/https URLs in plain text.  Priority 170 runs before
        # the default backtick (170) and emphasis (60) processors, ensuring bare
        # URLs are linked before other inline rules consume the text.  The
        # InlineProcessor only operates on text nodes, so URLs already inside
        # <a href="..."> are never double-linked.
        md.inlinePatterns.register(
            UrlAutolinkProcessor(_BARE_URL_RE, md), "md_view_url_autolink", 170
        )


def mathml_for(tex: str, *, display: bool) -> str:
    tex = tex.strip()
    if not tex:
        return ""
    mode = "block" if display else "inline"
    try:
        try:
            mathml = latex_to_mathml(tex, display=mode)
        except TypeError:
            mathml = latex_to_mathml(tex)
    except Exception as exc:  # latex2mathml reports parse errors as exceptions.
        return (
            '<code class="math-error" title="'
            + html.escape(str(exc), quote=True)
            + '">'
            + html.escape(tex)
            + "</code>"
        )
    tag = "div" if display else "span"
    klass = "math math-block" if display else "math math-inline"
    return f'<{tag} class="{klass}">{mathml}</{tag}>'


def convert_math_text(text: str) -> str:
    def block_dollar(match: re.Match[str]) -> str:
        return "\n\n" + mathml_for(match.group(1), display=True) + "\n\n"

    def block_bracket(match: re.Match[str]) -> str:
        return "\n\n" + mathml_for(match.group(1), display=True) + "\n\n"

    def inline_paren(match: re.Match[str]) -> str:
        return mathml_for(match.group(1), display=False)

    def inline_dollar(match: re.Match[str]) -> str:
        return mathml_for(match.group(1), display=False)

    text = re.sub(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", block_dollar, text, flags=re.S)
    text = re.sub(r"(?<!\\)\\\[(.+?)(?<!\\)\\\]", block_bracket, text, flags=re.S)
    text = re.sub(r"(?<!\\)\\\((.+?)(?<!\\)\\\)", inline_paren, text, flags=re.S)
    text = re.sub(r"(?<!\\)\$(?![\s$])([^\n$]+?)(?<!\\)\$", inline_dollar, text)
    return text


def md_view_slugify(value: str, separator: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).strip().lower()
    value = re.sub(r"\s+", separator, value, flags=re.UNICODE)
    value = re.sub(rf"[^\w{re.escape(separator)}]", "", value, flags=re.UNICODE)
    return value.strip(separator)


def render_markdown(markdown_text: str) -> str:
    md = markdown_lib.Markdown(
        extensions=[MdViewExtension(), "extra", "sane_lists", "codehilite", "toc"],
        extension_configs={
            "codehilite": {
                "guess_lang": False,
                "use_pygments": True,
                "noclasses": False,
                "pygments_style": "default",
            },
            "toc": {
                "permalink": "¶",
                "toc_depth": "1-6",
                "slugify": md_view_slugify,
            },
        },
        output_format="html5",
    )
    body = md.convert(markdown_text)
    if body.strip():
        return body
    return '<div class="empty">The Markdown file is empty.</div>'


def read_markdown(path: Path) -> tuple[str, str | None]:
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except FileNotFoundError:
        return "", f"File not found: {path}"
    except OSError as exc:
        return "", f"Could not read {path}: {exc}"


def file_signature(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "mtime_ns": 0, "size": 0}
    except OSError as exc:
        return {"exists": False, "mtime_ns": 0, "size": 0, "error": str(exc)}
    return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def script_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def style_css() -> bytes:
    formatter = HtmlFormatter(style="default")
    pygments_css = formatter.get_style_defs(".codehilite")
    return (BASE_CSS + "\n\n" + pygments_css + "\n").encode("utf-8")


def is_markdown_ref(path_ref: str | PurePosixPath | Path) -> bool:
    return PurePosixPath(str(path_ref)).suffix.lower() in MARKDOWN_EXTENSIONS


def nearest_git_ancestor(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def safe_root_for_markdown_paths(paths: list[Path]) -> Path:
    if not paths:
        raise ValueError("at least one Markdown file is required")

    per_file_git_roots = [nearest_git_ancestor(path.parent) for path in paths]
    unique_git_roots = {
        git_root for git_root in per_file_git_roots if git_root is not None
    }
    if None not in per_file_git_roots and len(unique_git_roots) == 1:
        return next(iter(unique_git_roots)).resolve()

    parents = [path.parent.resolve() for path in paths]
    common_parent = Path(
        os.path.commonpath([str(parent) for parent in parents])
    ).resolve()
    common_git_root = nearest_git_ancestor(common_parent)
    if common_git_root is not None:
        return common_git_root.resolve()
    return common_parent


def doc_id_for_path(root: Path, path: Path) -> str:
    root = root.resolve()
    candidate = path.resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"document path escapes safe root: {path}") from exc


def path_is_within_root(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(candidate)]) == str(root)
    except ValueError:
        return False


class AnchorHrefRewriter(HTMLParser):
    def __init__(self, rewrite_href: Any) -> None:
        super().__init__(convert_charrefs=False)
        self.rewrite_href = rewrite_href
        self.parts: list[str] = []

    def rewrite(self, html_text: str) -> str:
        self.parts = []
        self.feed(html_text)
        self.close()
        return "".join(self.parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(f"<{tag}{self._attrs(tag, attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        output: list[str] = []
        for name, value in attrs:
            if tag.lower() == "a" and name.lower() == "href" and value is not None:
                value = self.rewrite_href(value) or value
            if value is None:
                output.append(" " + name)
            else:
                output.append(f' {name}="{html.escape(value, quote=True)}"')
        return "".join(output)


@dataclass(frozen=True)
class MarkdownDoc:
    id: str
    path: Path

    @property
    def display_name(self) -> str:
        return self.path.name


@dataclass
class RenderCache:
    signature: dict[str, Any] | None = None
    html: str = ""
    error: str | None = None


@dataclass
class AppState:
    docs: list[MarkdownDoc]
    assets_dir: Path
    safe_root: Path
    initial_aliases: dict[str, str] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    doc_lock: threading.RLock = field(default_factory=threading.RLock)
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    render_cache: dict[str, RenderCache] = field(default_factory=dict)
    doc_lookup: dict[str, MarkdownDoc] = field(init=False, default_factory=dict)
    doc_aliases: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.docs:
            raise ValueError("at least one Markdown file is required")
        self.safe_root = self.safe_root.resolve()
        self.doc_lookup = {doc.id: doc for doc in self.docs}
        if len(self.doc_lookup) != len(self.docs):
            raise ValueError("document IDs must be unique")
        self.doc_aliases = dict(self.initial_aliases)
        for alias, target_id in self.doc_aliases.items():
            if target_id not in self.doc_lookup:
                raise ValueError(f"document alias {alias!r} targets unknown document")

    @property
    def default_doc(self) -> MarkdownDoc:
        return self.docs[0]

    @property
    def default_doc_id(self) -> str:
        return self.default_doc.id

    @property
    def display_name(self) -> str:
        return self.default_doc.display_name

    def doc_by_id(self, doc_id: str | None) -> MarkdownDoc | None:
        if doc_id in (None, ""):
            return self.default_doc
        with self.doc_lock:
            doc = self._lookup_doc_locked(doc_id)
        if doc is not None:
            return doc
        return self.add_dynamic_doc(doc_id)

    def _lookup_doc_locked(self, doc_id: str) -> MarkdownDoc | None:
        doc = self.doc_lookup.get(doc_id)
        if doc is not None:
            return doc
        alias_target = self.doc_aliases.get(doc_id)
        if alias_target is None:
            return None
        return self.doc_lookup.get(alias_target)

    def add_dynamic_doc(self, doc_id: str) -> MarkdownDoc | None:
        resolved = self.resolve_dynamic_doc_id(doc_id)
        if resolved is None:
            return None
        path, canonical_id = resolved
        with self.doc_lock:
            doc = self.doc_lookup.get(canonical_id)
            if doc is not None:
                return doc
            doc = MarkdownDoc(id=canonical_id, path=path)
            self.docs.append(doc)
            self.doc_lookup[canonical_id] = doc
            return doc

    def resolve_dynamic_doc_id(self, doc_id: str) -> tuple[Path, str] | None:
        if not doc_id or "\x00" in doc_id or "\\" in doc_id:
            return None
        rel = PurePosixPath(doc_id)
        if rel.is_absolute():
            return None
        parts = rel.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        if not is_markdown_ref(parts[-1]):
            return None

        candidate = (self.safe_root / Path(*parts)).resolve()
        if not path_is_within_root(self.safe_root, candidate):
            return None
        try:
            canonical_id = candidate.relative_to(self.safe_root).as_posix()
        except ValueError:
            return None
        return candidate, canonical_id

    def viewer_url_for_markdown_href(
        self, current_doc: MarkdownDoc, href: str
    ) -> str | None:
        try:
            parsed = urllib.parse.urlsplit(href)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc or parsed.query:
            return None

        raw_path = parsed.path
        if not raw_path:
            return None
        if raw_path.startswith(("/", "\\")) or "\\" in raw_path:
            return None

        decoded_path = urllib.parse.unquote(raw_path)
        if (
            not decoded_path
            or "\x00" in decoded_path
            or "\\" in decoded_path
            or decoded_path.startswith(("/", "\\"))
        ):
            return None
        if not is_markdown_ref(decoded_path):
            return None

        candidate = (current_doc.path.parent / decoded_path).resolve()
        if not path_is_within_root(self.safe_root, candidate):
            return None
        try:
            target_id = candidate.relative_to(self.safe_root).as_posix()
        except ValueError:
            return None

        url = "/?doc=" + urllib.parse.quote(target_id, safe="")
        if parsed.fragment:
            url += "#" + parsed.fragment
        return url

    def rewrite_markdown_links(self, current_doc: MarkdownDoc, html_body: str) -> str:
        return AnchorHrefRewriter(
            lambda href: self.viewer_url_for_markdown_href(current_doc, href)
        ).rewrite(html_body)

    def docs_metadata(self) -> list[dict[str, str]]:
        with self.doc_lock:
            docs = list(self.docs)
        return [
            {
                "id": doc.id,
                "title": doc.display_name,
                "path": str(doc.path),
                "root_path": doc.id,
            }
            for doc in docs
        ]

    def render_doc(self, doc: MarkdownDoc) -> dict[str, Any]:
        sig = file_signature(doc.path)
        with self.cache_lock:
            cached = self.render_cache.get(doc.id)
            if cached is not None and sig == cached.signature:
                html_body = cached.html
                error = cached.error
            else:
                text, error = read_markdown(doc.path)
                if error:
                    html_body = (
                        '<div class="error-panel">' + html.escape(error) + "</div>"
                    )
                else:
                    html_body = self.rewrite_markdown_links(doc, render_markdown(text))
                self.render_cache[doc.id] = RenderCache(sig, html_body, error)
        return {
            "doc_id": doc.id,
            "title": doc.display_name,
            "path": str(doc.path),
            "signature": sig,
            "html": html_body,
            "error": error,
            "docs": self.docs_metadata(),
        }

    def mermaid_asset(self) -> str | None:
        official = self.assets_dir / "vendor" / "mermaid.min.js"
        lite = self.assets_dir / "vendor" / "mermaid-lite.js"
        if official.exists():
            return "/assets/vendor/mermaid.min.js"
        if lite.exists():
            return "/assets/vendor/mermaid-lite.js"
        return None


class MdViewHandler(BaseHTTPRequestHandler):
    server_version = "md-view/1.0"
    state: AppState

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("md-view: " + (fmt % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API name
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        doc_id = query.get("doc", [None])[0]
        if path == "/":
            doc = self.state.doc_by_id(doc_id)
            if doc is None:
                self.send_error(404, "Unknown document")
                return
            self.send_html(index_html(self.state, doc))
        elif path == "/docs":
            self.send_json(
                {
                    "docs": self.state.docs_metadata(),
                    "default_doc_id": self.state.default_doc_id,
                }
            )
        elif path == "/render":
            self.send_render(doc_id)
        elif path == "/state":
            self.send_state(doc_id)
        elif path == "/events":
            doc = self.state.doc_by_id(doc_id)
            if doc is None:
                self.send_error(404, "Unknown document")
                return
            self.send_events(doc)
        elif path == "/assets/style.css":
            self.send_bytes(style_css(), "text/css; charset=utf-8")
        elif path.startswith("/assets/"):
            self.send_asset(path.removeprefix("/assets/"))
        else:
            self.send_error(404, "Not found")

    def send_render(self, doc_id: str | None) -> None:
        doc = self.state.doc_by_id(doc_id)
        if doc is None:
            self.send_error(404, "Unknown document")
            return
        self.send_json(self.state.render_doc(doc))

    def send_state(self, doc_id: str | None) -> None:
        doc = self.state.doc_by_id(doc_id)
        if doc is None:
            self.send_error(404, "Unknown document")
            return
        self.send_json({"doc_id": doc.id, "signature": file_signature(doc.path)})

    def send_html(self, text: str) -> None:
        self.send_bytes(text.encode("utf-8"), "text/html; charset=utf-8")

    def send_json(self, value: Any) -> None:
        self.send_bytes(
            json_bytes(value), "application/json; charset=utf-8", cache=False
        )

    def send_bytes(self, data: bytes, content_type: str, *, cache: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_asset(self, rel_url_path: str) -> None:
        try:
            rel = PurePosixPath(rel_url_path)
            if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                raise ValueError("invalid asset path")
            candidate = (self.state.assets_dir / Path(*rel.parts)).resolve()
            root = self.state.assets_dir.resolve()
            if os.path.commonpath([str(root), str(candidate)]) != str(root):
                raise ValueError("asset path escapes asset root")
            data = candidate.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "Asset not found")
            return
        except (OSError, ValueError) as exc:
            self.send_error(403, str(exc))
            return

        content_type, _ = mimetypes.guess_type(str(candidate))
        self.send_bytes(data, content_type or "application/octet-stream")

    def send_events(self, doc: MarkdownDoc) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_signature = file_signature(doc.path)
        last_heartbeat = 0.0
        self._write_sse_comment("connected")

        while not self.state.stop_event.is_set():
            now = time.monotonic()
            sig = file_signature(doc.path)
            if sig != last_signature:
                last_signature = sig
                if not self._write_sse_event(
                    "reload", {"doc_id": doc.id, "signature": sig}
                ):
                    return
            elif now - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = now
                if not self._write_sse_comment("heartbeat"):
                    return
            time.sleep(POLL_INTERVAL_SECONDS)

    def _write_sse_comment(self, comment: str) -> bool:
        try:
            self.wfile.write((": " + comment + "\n\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _write_sse_event(self, event: str, data: Any) -> bool:
        payload = "event: " + event + "\n" + "data: " + json.dumps(data) + "\n\n"
        try:
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


def index_html(state: AppState, selected_doc: MarkdownDoc | None = None) -> str:
    selected_doc = selected_doc or state.default_doc
    mermaid_src = state.mermaid_asset()
    mermaid_tag = (
        f'<script src="{html.escape(mermaid_src)}"></script>' if mermaid_src else ""
    )
    title = html.escape(selected_doc.display_name)
    full_path = html.escape(str(selected_doc.path))
    docs_metadata = state.docs_metadata()
    docs_json = script_json(docs_metadata)
    default_doc_id_json = script_json(state.default_doc_id)
    selected_doc_id_json = script_json(selected_doc.id)
    doc_list_hidden = " hidden" if len(docs_metadata) <= 1 else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} - md-view</title>
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <div class="topbar">
    <div class="title-group">
      <div id="title" class="title" title="{full_path}">{title}</div>
      <div id="currentPath" class="current-path" title="{full_path}">{full_path}</div>
    </div>
    <div id="status" class="status">starting…</div>
  </div>
  <nav id="docList" class="doc-list" aria-label="Markdown files"{doc_list_hidden}></nav>
  <main id="content" aria-live="polite"><div class="empty">Loading…</div></main>
  {mermaid_tag}
  <script>
  const docs = {docs_json};
  const defaultDocId = {default_doc_id_json};
  let currentDocId = {selected_doc_id_json};
  const titleEl = document.getElementById('title');
  const currentPathEl = document.getElementById('currentPath');
  const statusEl = document.getElementById('status');
  const contentEl = document.getElementById('content');
  const docListEl = document.getElementById('docList');
  const markdownExtensions = ['.md', '.markdown', '.mdown'];
  let renderInFlight = false;
  let pendingRender = false;
  let pendingScrollHash = window.location.hash || null;
  let eventSource = null;
  let pollTimer = null;
  let lastSignature = null;
  let selectionRequestId = 0;

  function setStatus(text) {{ statusEl.textContent = text; }}
  function sameSignature(a, b) {{ return JSON.stringify(a) === JSON.stringify(b); }}
  function selectedDoc() {{ return docs.find((doc) => doc.id === currentDocId) || null; }}
  function docUrl(endpoint, docId) {{
    return endpoint + '?doc=' + encodeURIComponent(docId || defaultDocId) + '&ts=' + Date.now();
  }}

  function basename(docId) {{
    const value = String(docId || '');
    const parts = value.split('/');
    return parts[parts.length - 1] || value || 'Markdown';
  }}

  function metadataForDocId(docId) {{
    return {{ id: docId, title: basename(docId), path: docId, root_path: docId }};
  }}

  function normalizeHash(hash) {{
    if (!hash) return '';
    return hash.startsWith('#') ? hash : '#' + hash;
  }}

  function decodeHash(hash) {{
    const raw = normalizeHash(hash).slice(1);
    try {{ return decodeURIComponent(raw); }} catch (_) {{ return raw; }}
  }}

  function scrollToTop() {{
    window.scrollTo({{ top: 0, behavior: 'smooth' }});
  }}

  function scrollToHash(hash) {{
    const normalized = normalizeHash(hash);
    const id = decodeHash(normalized);
    if (!id) {{ scrollToTop(); return; }}
    const target = document.getElementById(id) || document.getElementsByName(id)[0];
    if (target) target.scrollIntoView({{ block: 'start' }});
  }}

  function makeDocPageUrl(docId, hash) {{
    const url = new URL(window.location.href);
    url.pathname = '/';
    url.search = 'doc=' + encodeURIComponent(docId || defaultDocId);
    url.hash = normalizeHash(hash);
    return url;
  }}

  function updateHistory(docId, hash, replace) {{
    const state = {{ docId: docId || defaultDocId, hash: normalizeHash(hash) }};
    const url = makeDocPageUrl(state.docId, state.hash);
    if (replace) history.replaceState(state, '', url);
    else history.pushState(state, '', url);
  }}

  function setActiveDocButton() {{
    for (const button of docListEl.querySelectorAll('.doc-button')) {{
      button.classList.toggle('active', button.dataset.docId === currentDocId);
    }}
  }}

  function syncDocListVisibility() {{
    if (docListEl) docListEl.hidden = docs.length <= 1;
  }}

  function ensureDocButton(doc) {{
    if (!docListEl || !doc || !doc.id) return;
    let button = null;
    for (const existing of docListEl.querySelectorAll('.doc-button')) {{
      if (existing.dataset.docId === doc.id) {{ button = existing; break; }}
    }}
    if (!button) {{
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'doc-button';
      button.dataset.docId = doc.id;
      button.addEventListener('click', () => selectDoc(doc.id, {{ pushHistory: true, reason: 'selected' }}));
      docListEl.appendChild(button);
    }}
    button.textContent = doc.title || basename(doc.id);
    button.title = doc.path || doc.root_path || doc.id;
  }}

  function upsertDoc(doc) {{
    if (!doc) return;
    const normalized = {{
      id: doc.id || doc.doc_id,
      title: doc.title,
      path: doc.path,
      root_path: doc.root_path,
    }};
    if (!normalized.id) return;
    let existing = docs.find((item) => item.id === normalized.id);
    if (!existing) {{
      existing = {{ id: normalized.id }};
      docs.push(existing);
    }}
    existing.title = normalized.title || existing.title || basename(normalized.id);
    existing.path = normalized.path || existing.path || normalized.root_path || normalized.id;
    existing.root_path = normalized.root_path || existing.root_path || normalized.id;
    ensureDocButton(existing);
    syncDocListVisibility();
    setActiveDocButton();
  }}

  function updateChrome() {{
    const doc = selectedDoc() || metadataForDocId(currentDocId);
    if (!doc) return;
    titleEl.textContent = doc.title;
    titleEl.title = doc.path;
    currentPathEl.textContent = doc.path;
    currentPathEl.title = doc.path;
    document.title = doc.title + ' - md-view';
    setActiveDocButton();
  }}

  function buildDocList() {{
    if (!docListEl) return;
    for (const doc of docs) {{
      ensureDocButton(doc);
    }}
    syncDocListVisibility();
    setActiveDocButton();
  }}

  async function runMermaid() {{
    if (!window.mermaid) return;
    try {{
      if (window.mermaid.initialize) {{
        window.mermaid.initialize({{ startOnLoad: false, securityLevel: 'strict', theme: 'default' }});
      }}
      if (window.mermaid.run) {{
        await window.mermaid.run({{ querySelector: '.mermaid' }});
      }} else if (window.mermaid.init) {{
        window.mermaid.init(undefined, document.querySelectorAll('.mermaid'));
      }}
    }} catch (err) {{
      console.error('Mermaid render failed', err);
      for (const node of document.querySelectorAll('.mermaid')) {{
        if (!node.querySelector('.mermaid-error')) {{
          const pre = document.createElement('pre');
          pre.className = 'mermaid-error';
          pre.textContent = 'Mermaid render failed: ' + (err && err.message ? err.message : err);
          node.appendChild(pre);
        }}
      }}
    }}
  }}

  async function fetchRenderData(docId) {{
      const res = await fetch(docUrl('/render', docId), {{ cache: 'no-store' }});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
  }}

  async function applyRenderData(data, reason, hashToScroll) {{
    lastSignature = data.signature;
    if (Array.isArray(data.docs)) {{
      for (const doc of data.docs) upsertDoc(doc);
    }}
    upsertDoc({{ id: data.doc_id, title: data.title, path: data.path, root_path: data.doc_id }});
    contentEl.innerHTML = data.html;
    document.title = data.title + ' - md-view';
    updateChrome();
    await runMermaid();
    if (hashToScroll !== null && hashToScroll !== undefined) {{
      if (hashToScroll) scrollToHash(hashToScroll);
      else scrollToTop();
    }}
    const stamp = new Date().toLocaleTimeString();
    setStatus((data.error ? 'error' : 'loaded') + ' ' + stamp + (reason ? ' · ' + reason : ''));
  }}

  async function renderNow(reason) {{
    if (renderInFlight) {{ pendingRender = true; return; }}
    const docId = currentDocId;
    renderInFlight = true;
    pendingRender = false;
    try {{
      const data = await fetchRenderData(docId);
      if (docId !== currentDocId) return;
      const hashToScroll = pendingScrollHash;
      pendingScrollHash = null;
      await applyRenderData(data, reason, hashToScroll);
    }} catch (err) {{
      console.error(err);
      setStatus('reload failed: ' + (err && err.message ? err.message : err));
    }} finally {{
      renderInFlight = false;
      if (pendingRender) renderNow('queued');
    }}
  }}

  function startPollingFallback() {{
    if (pollTimer) return;
    pollTimer = setInterval(async () => {{
      const docId = currentDocId;
      try {{
        const res = await fetch(docUrl('/state', docId), {{ cache: 'no-store' }});
        if (!res.ok) return;
        const data = await res.json();
        if (docId !== currentDocId) return;
        if (!sameSignature(data.signature, lastSignature)) await renderNow('poll');
      }} catch (_) {{}}
    }}, 1500);
  }}

  function connectEvents() {{
    if (!window.EventSource) {{
      setStatus('SSE unavailable; polling');
      startPollingFallback();
      return;
    }}
    if (eventSource) eventSource.close();
    const docId = currentDocId;
    eventSource = new EventSource('/events?doc=' + encodeURIComponent(docId));
    const source = eventSource;
    source.addEventListener('open', () => {{
      if (source === eventSource) setStatus('watching for changes');
    }});
    source.addEventListener('reload', () => {{
      if (docId === currentDocId) renderNow('file changed');
    }});
    source.onerror = () => {{
      if (source !== eventSource) return;
      setStatus('watch disconnected; retrying');
      source.close();
      eventSource = null;
      setTimeout(() => {{ if (docId === currentDocId) connectEvents(); }}, 1500);
      startPollingFallback();
    }};
  }}

  async function selectDoc(docId, options = {{}}) {{
    if (!docId) return;
    const hash = normalizeHash(options.hash || '');
    if (docId === currentDocId) {{
      if (hash) {{
        if (options.pushHistory) updateHistory(currentDocId, hash, false);
        scrollToHash(hash);
      }}
      return;
    }}
    const requestId = ++selectionRequestId;
    setStatus('loading ' + docId + '…');
    try {{
      const data = await fetchRenderData(docId);
      if (requestId !== selectionRequestId) return;
      currentDocId = data.doc_id || docId;
      lastSignature = null;
      pendingRender = false;
      pendingScrollHash = null;
      if (options.pushHistory) updateHistory(currentDocId, hash, false);
      if (eventSource) {{ eventSource.close(); eventSource = null; }}
      await applyRenderData(data, options.reason || 'selected', hash);
      connectEvents();
    }} catch (err) {{
      if (requestId !== selectionRequestId) return;
      console.error(err);
      setStatus('load failed: ' + (err && err.message ? err.message : err));
    }}
  }}

  function hasKnownScheme(value) {{
    return /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value);
  }}

  function decodePath(value) {{
    try {{ return decodeURIComponent(value); }} catch (_) {{ return value; }}
  }}

  function isMarkdownPath(value) {{
    const lower = value.toLowerCase();
    return markdownExtensions.some((suffix) => lower.endsWith(suffix));
  }}

  function splitHash(value) {{
    const index = value.indexOf('#');
    if (index === -1) return {{ path: value, hash: '' }};
    return {{ path: value.slice(0, index), hash: value.slice(index) }};
  }}

  function normalizeRelativeDocPath(path) {{
    if (!path || path.startsWith('/') || path.startsWith('\\\\') || path.startsWith('//')) return null;
    if (path.includes('?') || hasKnownScheme(path)) return null;
    const decoded = decodePath(path);
    if (!decoded || decoded.includes('\\0') || decoded.includes('\\\\')) return null;
    if (decoded.startsWith('/') || decoded.startsWith('\\\\')) return null;

    const parts = String(currentDocId || defaultDocId).split('/');
    parts.pop();
    const normalizedParts = parts.filter((part) => part);
    for (const segment of decoded.split('/')) {{
      if (!segment || segment === '.') continue;
      if (segment === '..') {{
        if (!normalizedParts.length) return null;
        normalizedParts.pop();
      }} else {{
        normalizedParts.push(segment);
      }}
    }}
    const normalized = normalizedParts.join('/');
    if (!normalized || !isMarkdownPath(normalized)) return null;
    return normalized;
  }}

  function internalMarkdownLink(anchor) {{
    const raw = anchor.getAttribute('href');
    if (!raw) return null;
    try {{
      const url = new URL(raw, window.location.href);
      if (url.origin === window.location.origin && url.pathname === '/' && url.searchParams.has('doc')) {{
        return {{ docId: url.searchParams.get('doc') || defaultDocId, hash: normalizeHash(url.hash) }};
      }}
    }} catch (_) {{}}
    if (raw.startsWith('#')) return {{ docId: currentDocId, hash: normalizeHash(raw) }};
    if (raw.startsWith('/') || raw.startsWith('\\\\') || raw.startsWith('//') || hasKnownScheme(raw)) return null;

    const parts = splitHash(raw);
    const targetDocId = normalizeRelativeDocPath(parts.path);
    if (!targetDocId) return null;
    return {{ docId: targetDocId, hash: normalizeHash(parts.hash) }};
  }}

  contentEl.addEventListener('click', (event) => {{
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    const anchor = target ? target.closest('a[href]') : null;
    if (!anchor || !contentEl.contains(anchor)) return;
    if (anchor.target && anchor.target !== '_self') return;
    const link = internalMarkdownLink(anchor);
    if (!link) return;
    event.preventDefault();
    if (link.docId === currentDocId) {{
      if (link.hash) {{
        updateHistory(currentDocId, link.hash, false);
        scrollToHash(link.hash);
      }}
      return;
    }}
    selectDoc(link.docId, {{ hash: link.hash, pushHistory: true, reason: 'selected' }});
  }});

  window.addEventListener('popstate', () => {{
    const params = new URLSearchParams(window.location.search);
    const docId = params.get('doc') || defaultDocId;
    const hash = window.location.hash || '';
    if (docId === currentDocId) {{
      if (hash) scrollToHash(hash);
      else scrollToTop();
      return;
    }}
    selectDoc(docId, {{ hash, pushHistory: false, reason: 'history' }});
  }});

  buildDocList();
  updateChrome();
  updateHistory(currentDocId, window.location.hash || '', true);
  renderNow('initial');
  connectEvents();
  </script>
</body>
</html>
"""


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 65535") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 65535")
    return port


def create_server(
    preferred: int, *, exact: bool, handler_class: type[BaseHTTPRequestHandler]
) -> ThreadingHTTPServer:
    candidates = [preferred] if exact else range(preferred, min(preferred + 50, 65536))
    for port in candidates:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
        except OSError:
            continue
        server.daemon_threads = True
        return server
    if exact:
        raise SystemExit(f"Port {preferred} is already in use")
    raise SystemExit(f"No free localhost ports found near {preferred}")


def open_browser(browser: str, url: str) -> None:
    if browser == "none":
        return
    executable = shutil.which(browser)
    if executable is None:
        print(
            f"md-view: browser '{browser}' not found; open manually: {url}",
            file=sys.stderr,
        )
        return
    try:
        subprocess.Popen(
            [executable, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        print(
            f"md-view: could not launch {browser}: {exc}; open manually: {url}",
            file=sys.stderr,
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="md-view",
        description="Local offline Markdown visualizer with live reload.",
        epilog=HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("markdown_files", nargs="+", help="Markdown file(s) to display")
    parser.add_argument(
        "--port",
        type=parse_port,
        default=DEFAULT_PORT,
        help=f"localhost port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--browser",
        choices=("firefox", "none"),
        default="firefox",
        help="browser to open (default: firefox)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    markdown_paths: list[Path] = []
    for markdown_file in args.markdown_files:
        markdown_path = Path(markdown_file).expanduser().resolve()
        if not markdown_path.exists():
            print(f"md-view: file does not exist: {markdown_path}", file=sys.stderr)
            return 2
        if not markdown_path.is_file():
            print(f"md-view: not a regular file: {markdown_path}", file=sys.stderr)
            return 2
        markdown_paths.append(markdown_path)

    app_home = Path(os.environ.get("MD_VIEW_HOME", str(REPO_ROOT))).expanduser()
    assets_dir = (app_home / "assets").resolve()
    safe_root = safe_root_for_markdown_paths(markdown_paths)
    docs: list[MarkdownDoc] = []
    seen_doc_ids: set[str] = set()
    initial_aliases: dict[str, str] = {}
    for index, markdown_path in enumerate(markdown_paths):
        doc_id = doc_id_for_path(safe_root, markdown_path)
        initial_aliases[str(index)] = doc_id
        if doc_id in seen_doc_ids:
            continue
        docs.append(MarkdownDoc(id=doc_id, path=markdown_path))
        seen_doc_ids.add(doc_id)
    state = AppState(
        docs=docs,
        assets_dir=assets_dir,
        safe_root=safe_root,
        initial_aliases=initial_aliases,
    )

    raw_argv = sys.argv[1:] if argv is None else argv
    requested_exact_port = any(
        arg == "--port" or arg.startswith("--port=") for arg in raw_argv
    )

    class Handler(MdViewHandler):
        pass

    Handler.state = state
    server = create_server(args.port, exact=requested_exact_port, handler_class=Handler)
    port = server.server_address[1]

    url = f"http://127.0.0.1:{port}/"
    if len(markdown_paths) == 1:
        print(f"md-view: serving {markdown_paths[0]}")
    else:
        print(f"md-view: serving {len(markdown_paths)} files")
    print(f"md-view: {url}")
    if state.mermaid_asset() is None:
        print(
            "md-view: warning: no local Mermaid asset found; "
            "place mermaid.min.js or mermaid-lite.js in assets/vendor/",
            file=sys.stderr,
        )
    open_browser(args.browser, url)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nmd-view: stopping", file=sys.stderr)
    finally:
        state.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
