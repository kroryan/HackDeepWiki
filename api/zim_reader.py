"""
Thin wrapper over libzim (openzim.org's Python bindings) for reading .zim
archives: offline wiki dumps (Wikipedia, DevDocs, StackExchange, etc.).

A .zim file can hold anywhere from dozens to millions of entries, so this
module never loads "everything" -- it opens the archive lazily, resolves
individual entries by path, and relies on libzim's built-in full-text search
index (Xapian under the hood) rather than building our own index.
"""
import logging
import re
import threading
from typing import Optional, TypedDict

from libzim.reader import Archive
from libzim.search import Query, Searcher

logger = logging.getLogger(__name__)

# Archive objects are cheap to keep open (lazy reads), so cache them by path
# for the lifetime of the process instead of reopening on every request.
_archive_cache: dict[str, Archive] = {}
_archive_cache_lock = threading.Lock()

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class SearchHit(TypedDict):
    path: str
    title: str


def open_archive(path: str) -> Archive:
    """Open (or reuse a cached handle for) the .zim file at `path`.

    Raises whatever libzim raises (RuntimeError) if `path` is not a valid
    .zim file -- callers should catch that and turn it into an HTTP 400.
    """
    with _archive_cache_lock:
        cached = _archive_cache.get(path)
        if cached is not None:
            return cached
        archive = Archive(path)
        _archive_cache[path] = archive
        return archive


def close_archive(path: str) -> None:
    """Drop a cached Archive handle (called when a .zim is unregistered)."""
    with _archive_cache_lock:
        _archive_cache.pop(path, None)


def _resolve_main_entry_path(archive: Archive) -> Optional[str]:
    """archive.main_entry is itself a redirect pseudo-entry: its own `.path`
    (e.g. "mainPage") is NOT a real, independently-resolvable path in the
    archive namespace -- calling get_entry_by_path() on it raises "Cannot
    find entry". The real, browsable path is only reachable by following the
    redirect once via get_redirect_entry()."""
    if not archive.has_main_entry:
        return None
    main_entry = archive.main_entry
    if main_entry.is_redirect:
        return main_entry.get_redirect_entry().path
    return main_entry.path


def get_metadata(archive: Archive) -> dict:
    def _meta(key: str) -> Optional[str]:
        if key not in archive.metadata_keys:
            return None
        try:
            return archive.get_metadata(key).decode("utf-8", errors="replace")
        except Exception:
            return None

    return {
        "title": _meta("Title") or "Untitled ZIM",
        "description": _meta("Description") or "",
        "language": _meta("Language") or "",
        "creator": _meta("Creator") or "",
        "articleCount": archive.article_count,
        "hasFulltextIndex": archive.has_fulltext_index,
        "mainEntryPath": _resolve_main_entry_path(archive),
    }


def get_entry_content(archive: Archive, path: str) -> tuple[bytes, str]:
    """Return (content_bytes, mimetype) for the entry at `path`.

    Raises KeyError (via libzim) if the path does not exist in the archive.
    """
    entry = archive.get_entry_by_path(path)
    if entry.is_redirect:
        entry = entry.get_redirect_entry()
    item = entry.get_item()
    return bytes(item.content), item.mimetype


def get_entry_title(archive: Archive, path: str) -> str:
    entry = archive.get_entry_by_path(path)
    return entry.title


def search_entries(archive: Archive, query: str, limit: int = 5) -> list[SearchHit]:
    """Full-text search using libzim's built-in Xapian index.

    Falls back to an empty list (never raises) if the archive has no
    full-text index or the query fails -- callers treat "no results" the
    same as "search unavailable" for a single .zim.
    """
    if not query or not query.strip():
        return []
    try:
        searcher = Searcher(archive)
        search = searcher.search(Query().set_query(query))
        hits: list[SearchHit] = []
        for path in search.getResults(0, limit):
            try:
                title = archive.get_entry_by_path(path).title
            except Exception:
                title = path
            hits.append({"path": path, "title": title})
        return hits
    except Exception as e:
        logger.warning(f"ZIM search failed for query {query!r}: {e}")
        return []


def extract_plain_text(html: bytes, max_chars: int = 4000) -> str:
    """Strip HTML tags for a cheap plain-text snippet to feed an LLM as
    context. Not a real HTML parser -- good enough for prose extraction,
    not for anything security-sensitive (never rendered as HTML)."""
    text = html.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars]
