"""Builds .zim (OpenZIM/Kiwix) archives from a wiki's pages.

Shared by both entry points that produce a downloadable wiki export:
  * AI-generated wikis -- ``/export/wiki`` with ``format=="zim"`` (api.api).
  * Imported MediaWiki XML wikis -- ``fanwiki_library.export_zim``.

Any offline reader that already speaks the ZIM format (Kiwix, or this app's
own HackDeepWikiReader, which already reads .zim files for imported content)
can then browse either kind of wiki with no internet connection and this app
not even running -- which means the archive has to be genuinely
self-contained: Mermaid diagrams rendered for real (not left as unreadable
fenced code) and every image embedded locally (not left as a remote URL that
goes dead the moment there's no network), not just the page text.

``libzim.writer.Creator`` is already a runtime dependency (used elsewhere in
this app to *read* .zim imports) and has full write support, so no new
third-party dependency is needed beyond ``markdown-it-py`` (already present
transitively via another package) for turning each page's Markdown body into
browsable HTML. Mermaid rendering reuses the exact same mermaid.js build the
web app ships (vendored into api/vendor/mermaid.min.js, MIT licensed) rather
than a second copy fetched at export time -- Kiwix/ZIM readers execute page
JS same as a normal browser tab, so client-side rendering works unmodified.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import mimetypes
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from libzim.writer import Creator, FileProvider, Hint, Item, StringProvider
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable(["table", "strikethrough"])
)

_MERMAID_JS_PATH = Path(__file__).resolve().parent / "vendor" / "mermaid.min.js"
_MERMAID_ASSET_PATH = "assets/mermaid.min.js"

# markdown-it renders a ```mermaid fence as <pre><code class="language-mermaid">
# ...escaped source...</code></pre> -- mermaid.js's documented embedding
# convention instead reads diagram source from <pre class="mermaid">, so this
# swaps the wrapper (keeping the HTML-escaped content as-is: the browser
# decodes entities for us via .textContent, same as any other HTML text).
_MERMAID_FENCE_RE = re.compile(
    r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL
)

# Remote images referenced from Markdown -- ![alt](https://...) -- go dead
# the moment the reader has no network, which defeats the point of an
# offline archive. Only http(s) URLs are rewritten; local/relative paths are
# left untouched (fanwiki_library.export_zim already resolves those to its
# own bundled _images/ assets before calling build_zim).
_MD_REMOTE_IMAGE_RE = re.compile(r'(!\[[^\]]*\]\()(https?://[^\s)]+)(\))')

_EXT_MIMETYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
}


def guess_mimetype(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _EXT_MIMETYPES.get(ext, "application/octet-stream")


# Minimal, self-contained styling (no external fonts/CDNs -- Kiwix/ZIM
# readers are offline by definition) that reads fine in both a browser-style
# light background and inside Kiwix's own reader chrome.
_PAGE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width: 860px; margin: 0 auto; padding: 24px 20px 60px; color: #1a1a1a; background: #ffffff; line-height: 1.6; }
h1, h2, h3 { line-height: 1.25; }
h1 { border-bottom: 1px solid #e0e0e0; padding-bottom: 8px; }
pre { background: #f5f5f5; padding: 12px; overflow-x: auto; border-radius: 4px; }
code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 0.92em; }
pre code { background: none; padding: 0; }
pre.mermaid { background: #ffffff; border: 1px solid #e0e0e0; text-align: center; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
img { max-width: 100%; }
a { color: #2563eb; }
.hdw-index ul { list-style: none; padding-left: 0; }
.hdw-index li { padding: 4px 0; }
.hdw-meta { color: #666; font-size: 0.9em; margin-bottom: 1.5em; }
"""

_MERMAID_INIT_SCRIPT = """
<script src="../%s"></script>
<script>
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: false, theme: "neutral", securityLevel: "loose" });
    mermaid.run({ querySelector: "pre.mermaid" });
  }
</script>
""" % _MERMAID_ASSET_PATH


def _page_shell(title: str, body_html: str, *, include_mermaid: bool = False) -> str:
    scripts = _MERMAID_INIT_SCRIPT if include_mermaid else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html_lib.escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f"<body><article><h1>{html_lib.escape(title)}</h1>{body_html}</article>{scripts}</body></html>"
    )


def _download_remote_images(markdown_text: str, url_cache: Dict[str, str], assets: List["ZimAssetSpec"]) -> str:
    """Replace every remote (http/https) Markdown image URL with a locally
    embedded copy, downloading each URL at most once across the whole
    archive (url_cache) and appending new ZimAssetSpec entries to `assets`.
    A failed download (network error, timeout, 404, ...) is non-fatal --
    the original remote URL is left in place so the export never breaks
    over one bad image, it just won't work offline for that one image."""

    def replace(match: re.Match) -> str:
        prefix, url, suffix = match.group(1), match.group(2), match.group(3)
        cached = url_cache.get(url)
        if cached:
            return f"{prefix}../{cached}{suffix}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HackDeepWiki offline export)"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        except Exception as exc:  # noqa: BLE001 - one bad image must never break the export
            logger.warning(f"Skipping offline embed for image (download failed): {url} ({exc})")
            return match.group(0)

        ext = mimetypes.guess_extension(content_type) if content_type else None
        if not ext:
            ext = os.path.splitext(url.split("?")[0].split("#")[0])[1] or ".bin"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        archive_path = f"assets/img_{digest}{ext}"
        url_cache[url] = archive_path
        assets.append(ZimAssetSpec(archive_path=archive_path, data=data, mimetype=content_type or guess_mimetype(archive_path)))
        return f"{prefix}../{archive_path}{suffix}"

    return _MD_REMOTE_IMAGE_RE.sub(replace, markdown_text)


def _render_page_body(markdown_text: str) -> Tuple[str, bool]:
    """Markdown -> (HTML body, whether it contains a Mermaid diagram)."""
    html_out = _md.render(markdown_text or "")
    html_out, n = _MERMAID_FENCE_RE.subn(r'<pre class="mermaid">\1</pre>', html_out)
    return html_out, n > 0


@dataclass
class ZimPageSpec:
    """One wiki page to add to the archive."""

    page_id: str  # used verbatim as the archive path stem: pages/<page_id>.html
    title: str
    markdown: str = ""
    html: Optional[str] = None  # pre-rendered HTML body (skips markdown_body rendering) if set


@dataclass
class ZimAssetSpec:
    """One binary asset (image, etc.) to add to the archive."""

    archive_path: str  # path inside the archive, e.g. "assets/foo.png"
    filepath: Optional[str] = None  # read from disk (streamed, not loaded up front)
    data: Optional[bytes] = None  # or pass raw bytes directly
    mimetype: str = "application/octet-stream"


class _TextItem(Item):
    def __init__(self, path: str, title: str, content, mimetype: str, front_article: bool = True):
        super().__init__()
        self._path = path
        self._title = title
        self._content = content
        self._mimetype = mimetype
        self._front_article = front_article

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._title

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self):
        return StringProvider(self._content)

    def get_hints(self) -> Dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._front_article}


class _FileItem(Item):
    def __init__(self, path: str, filepath: str, mimetype: str):
        super().__init__()
        self._path = path
        self._filepath = filepath
        self._mimetype = mimetype

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return ""

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self):
        return FileProvider(self._filepath)

    def get_hints(self) -> Dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: False}


def build_zim(
    output_path: str,
    *,
    title: str,
    description: str,
    language: str,
    creator: str,
    publisher: str,
    zim_name: str,
    pages: List[ZimPageSpec],
    assets: Optional[Iterable[ZimAssetSpec]] = None,
    index_intro_html: str = "",
) -> Dict[str, int]:
    """Write a complete .zim archive to ``output_path``.

    Adds one HTML article per page (``pages/<page_id>.html``), an
    auto-generated ``index.html`` table of contents (set as the ZIM main
    entry) listing every page, and any binary assets. Full-text indexing is
    enabled so Kiwix's built-in search works over the exported content.

    Pages built from ``markdown`` (as opposed to pre-rendered ``html``) get
    two additional passes so the archive is genuinely self-contained
    offline: Mermaid fences are rendered for real (mermaid.js is bundled in
    and only added once, if at least one page actually uses it), and any
    remote (http/https) image referenced in the Markdown is downloaded and
    embedded locally, rewriting the link in place.

    Returns ``{"page_count": N, "asset_count": M}``.
    """
    # ZIM's Language metadata wants ISO-639-3; most of this app's language
    # codes are already ISO-639-1 (en, es, fr...) which libzim/Kiwix also
    # tolerate in practice for indexing purposes, so passed through as-is
    # rather than maintaining a full ISO-639-1 -> ISO-639-3 mapping table.
    lang_code = (language or "en").split("-")[0] or "en"

    asset_list = list(assets or [])
    image_url_cache: Dict[str, str] = {}
    any_mermaid = False

    with Creator(output_path).config_indexing(True, lang_code) as zim_creator:
        zim_creator.set_mainpath("index.html")

        index_items = []
        for page in pages:
            path = f"pages/{page.page_id}.html"
            if page.html is not None:
                body_html, has_mermaid = page.html, False
            else:
                markdown_text = _download_remote_images(page.markdown or "", image_url_cache, asset_list)
                body_html, has_mermaid = _render_page_body(markdown_text)
            any_mermaid = any_mermaid or has_mermaid
            zim_creator.add_item(_TextItem(
                path=path, title=page.title,
                content=_page_shell(page.title, body_html, include_mermaid=has_mermaid),
                mimetype="text/html",
            ))
            index_items.append((path, page.title))

        index_list_html = "".join(
            f'<li><a href="{html_lib.escape(path)}">{html_lib.escape(item_title)}</a></li>'
            for path, item_title in index_items
        )
        index_html = _page_shell(
            title,
            f'<div class="hdw-meta">{html_lib.escape(description)}</div>'
            f"{index_intro_html}"
            f'<div class="hdw-index"><h2>Pages</h2><ul>{index_list_html}</ul></div>',
        )
        zim_creator.add_item(_TextItem(
            path="index.html", title=title, content=index_html,
            mimetype="text/html", front_article=True,
        ))

        if any_mermaid and _MERMAID_JS_PATH.is_file():
            zim_creator.add_item(_FileItem(_MERMAID_ASSET_PATH, str(_MERMAID_JS_PATH), "application/javascript"))
        elif any_mermaid:
            logger.warning(f"Mermaid diagram(s) found but vendored mermaid.min.js is missing at {_MERMAID_JS_PATH}; diagrams will show as plain text.")

        for asset in asset_list:
            if asset.filepath is not None:
                zim_creator.add_item(_FileItem(asset.archive_path, asset.filepath, asset.mimetype))
            elif asset.data is not None:
                zim_creator.add_item(_TextItem(
                    path=asset.archive_path, title="", content=asset.data,
                    mimetype=asset.mimetype, front_article=False,
                ))

        zim_creator.add_metadata("Title", title[:30] or "Wiki")
        zim_creator.add_metadata("Description", (description or title)[:80])
        zim_creator.add_metadata("Language", lang_code)
        zim_creator.add_metadata("Creator", creator or "HackDeepWiki")
        zim_creator.add_metadata("Publisher", publisher or "HackDeepWiki")
        zim_creator.add_metadata("Name", zim_name)

    return {"page_count": len(pages), "asset_count": len(asset_list) + (1 if any_mermaid and _MERMAID_JS_PATH.is_file() else 0)}
