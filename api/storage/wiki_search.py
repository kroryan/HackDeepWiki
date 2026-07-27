"""Full-text wiki search over generated wikis (Fase 2).

Backs ``GET /api/wiki/search`` and the MCP ``search_wiki`` tool's heavier
sibling: an FTS5 index of every page in every generated wiki, so a user can
find "where did I read about the ZIM exporter" across all their repos at
once, not just within one repo's cache.

Why FTS5 (not the in-memory scan mcp_tools.search_wiki does): the per-repo
scan is fine for one repo's ~10-50 pages, but a user with dozens of repos
each regenerated in multiple languages needs an indexed lookup, not a
re-scan of every JSON on disk for every keystroke. FTS5 is a SQLite
virtual table -- stdlib, portable, no new dependency -- and gives ranking
+ snippet extraction for free.

Index lives in ``profile.db`` (cross-repo by nature: search spans repos) as
a ``wiki_fts`` virtual table plus a ``wiki_pages`` source table mapping a
row back to its (repo, language, page_id) so a hit can deep-link.
"""

from __future__ import annotations

import logging
from typing import Optional

from api.storage import connect, profile_db_path

logger = logging.getLogger(__name__)

# FTS5 table is created lazily because CREATE VIRTUAL TABLE on every connect
# would be wasteful; we track whether we've initialized it this process.
_FTS_INITIALIZED = False


def _ensure_fts(conn) -> None:
    global _FTS_INITIALIZED
    if _FTS_INITIALIZED:
        return
    # Self-contained FTS5 (no external content): the FTS table holds its own
    # copy of title+content. The external-content variant is faster to update
    # but its 'delete'-then-reinsert sync corrupts easily if the reinserted
    # text doesn't byte-match the indexed text; for a wiki's hundreds-to-low-
    # thousands of pages the self-contained store is simpler and robust, and
    # the duplication is negligible against the wiki caches already on disk.
    # wiki_pages_meta holds the deep-link fields the FTS row can't (FTS rows
    # are opaque); rowid is shared so a hit joins back to its metadata.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wiki_pages_meta (
            rowid        INTEGER PRIMARY KEY,
            repo_key     TEXT NOT NULL,
            owner        TEXT,
            repo         TEXT,
            repo_type    TEXT,
            language     TEXT NOT NULL,
            page_id      TEXT NOT NULL,
            version      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wpm_repo ON wiki_pages_meta(repo_key, language);
        CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
            title, content,
            content_rowid='rowid',
            tokenize='unicode61'
        );
        """
    )
    conn.commit()
    _FTS_INITIALIZED = True


def _db():
    conn = connect(profile_db_path())
    _ensure_fts(conn)
    return conn


def _repo_key(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> str:
    from api.storage import repo_key as _rk
    return _rk(owner, repo, repo_type)


def index_wiki_cache(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                     language: str, pages: list[dict], version: Optional[str] = None) -> int:
    """Index (or re-index) one wiki release's pages. ``pages`` is the list of
    page dicts from WikiCacheData (each with id/title/content). Replaces any
    prior index for the same (repo_key, language, page_id, version) so a
    regeneration updates the index instead of duplicating. Returns the number
    of pages indexed."""
    rk = _repo_key(owner, repo, repo_type)
    n = 0
    with _db() as conn:
        for page in pages:
            pid = str(page.get("id") or page.get("title") or "")
            if not pid:
                continue
            title = str(page.get("title") or pid)
            content = str(page.get("content") or "")
            # Find an existing meta row for this exact page; if present, delete
            # its FTS entry first so the re-insert replaces instead of dupes.
            existing = conn.execute(
                "SELECT rowid FROM wiki_pages_meta WHERE repo_key=? AND language=? "
                "AND page_id=? AND COALESCE(version,'')=COALESCE(?,'')",
                (rk, language, pid, version),
            ).fetchone()
            if existing:
                rid = existing["rowid"]
                conn.execute("DELETE FROM wiki_fts WHERE rowid = ?", (rid,))
                conn.execute("DELETE FROM wiki_pages_meta WHERE rowid = ?", (rid,))
            # Insert the meta row (rowid auto-assigned), then mirror it into FTS
            # with the SAME rowid so a search hit joins back to its metadata.
            cur = conn.execute(
                "INSERT INTO wiki_pages_meta (repo_key, owner, repo, repo_type, language, page_id, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rk, owner, repo, repo_type, language, pid, version),
            )
            rid = cur.lastrowid
            conn.execute("INSERT INTO wiki_fts (rowid, title, content) VALUES (?, ?, ?)",
                         (rid, title, content))
            n += 1
        conn.commit()
    return n


def search(query: str, *, owner: Optional[str] = None, repo: Optional[str] = None,
           language: Optional[str] = None, limit: int = 20) -> list[dict]:
    """FTS5 MATCH search across indexed wiki pages. Optional owner/repo/language
    filters scope the results. Each hit returns title, a snippet, and enough
    to deep-link (owner/repo/repo_type/language/page_id/version)."""
    if not query or not query.strip():
        return []
    # Escape FTS5 special chars so a query like "C++" or "api/main.py" doesn't
    # trip the query parser -- wrap each token in double quotes.
    tokens = [t for t in query.strip().split() if t]
    if not tokens:
        return []
    match_expr = " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)
    sql = (
        "SELECT m.owner, m.repo, m.repo_type, m.language, m.page_id, m.version, "
        "f.title, snippet(wiki_fts, 1, '<<', '>>', '...', 12) AS snip, rank "
        "FROM wiki_fts f JOIN wiki_pages_meta m ON m.rowid = f.rowid "
        "WHERE wiki_fts MATCH ? "
    )
    params: list = [match_expr]
    if owner and repo:
        sql += " AND m.owner = ? AND m.repo = ?"
        params += [owner, repo]
    if language:
        sql += " AND m.language = ?"
        params += [language]
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "owner": r["owner"], "repo": r["repo"], "repo_type": r["repo_type"],
            "language": r["language"], "page_id": r["page_id"], "title": r["title"],
            "version": r["version"], "snippet": r["snip"],
        }
        for r in rows
    ]


def drop_repo(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
              language: Optional[str] = None) -> int:
    """Remove a repo's pages from the FTS index (e.g. when its wiki is
    deleted). Returns rows removed."""
    rk = _repo_key(owner, repo, repo_type)
    with _db() as conn:
        rows = conn.execute(
            "SELECT rowid FROM wiki_pages_meta WHERE repo_key = ?"
            + (" AND language = ?" if language else ""),
            [rk] + ([language] if language else []),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM wiki_fts WHERE rowid = ?", (r["rowid"],))
        cur = conn.execute(
            "DELETE FROM wiki_pages_meta WHERE repo_key = ?"
            + (" AND language = ?" if language else ""),
            [rk] + ([language] if language else []),
        )
        conn.commit()
        return cur.rowcount
