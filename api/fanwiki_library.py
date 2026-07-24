"""Discovery and lifecycle helpers for imported MediaWiki XML sources.

An XML import exists before any LLM-generated wiki cache does.  Keeping this
small registry derived from the source manifests makes imports durable across
browser reloads without introducing a second database that can drift out of
sync with the Markdown tree on disk.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from api.data_root import get_data_root
from api.web_crawler.site_store import read_site_meta, website_local_dir


def _source_id(start_url: str) -> str:
    digest = hashlib.sha256(start_url.encode("utf-8")).hexdigest()[:16]
    return f"fanwiki-source-{digest}"


def _is_fanwiki_manifest(meta: Dict) -> bool:
    if meta.get("source_type") == "fanwiki":
        return True
    # Compatibility with imports produced by the first fanwiki release,
    # before source_type was persisted. Imported pages always carried a
    # categories field; live crawler manifests do not.
    pages = meta.get("pages")
    return bool(
        isinstance(pages, list)
        and any(isinstance(page, dict) and "categories" in page for page in pages[:50])
    )


def _timestamp_ms(value: object, fallback_path: str) -> int:
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    try:
        return int(os.path.getmtime(fallback_path) * 1000)
    except OSError:
        return 0


def _entry_from_manifest(local_dir: str, meta: Dict) -> Dict:
    start_url = str(meta.get("start_url") or "").strip()
    host = urlparse(start_url).hostname or "import"
    wiki_name = str(meta.get("wiki_name") or "").strip()
    return {
        "id": _source_id(start_url),
        "owner": "fanwiki",
        "repo": host,
        "name": wiki_name or f"Imported fanwiki: {host}",
        "repo_type": "fanwiki",
        "submittedAt": _timestamp_ms(meta.get("crawled_at"), local_dir),
        "language": "",
        "status": "imported",
        "start_url": start_url,
        "page_count": int(meta.get("page_count") or 0),
    }


def _sources() -> List[Tuple[str, Dict]]:
    repos_dir = os.path.join(get_data_root(), "repos")
    if not os.path.isdir(repos_dir):
        return []

    sources: List[Tuple[str, Dict]] = []
    for name in os.listdir(repos_dir):
        local_dir = os.path.join(repos_dir, name)
        if not os.path.isdir(local_dir):
            continue
        meta = read_site_meta(local_dir)
        if not meta or not _is_fanwiki_manifest(meta) or not meta.get("start_url"):
            continue
        sources.append((local_dir, meta))
    return sources


def list_all() -> List[Dict]:
    entries = [_entry_from_manifest(local_dir, meta) for local_dir, meta in _sources()]
    entries.sort(key=lambda entry: entry["submittedAt"], reverse=True)
    return entries


def _find(entry_id: str) -> Optional[Tuple[str, Dict]]:
    for local_dir, meta in _sources():
        if _source_id(str(meta.get("start_url") or "").strip()) == entry_id:
            return local_dir, meta
    return None


def get(entry_id: str) -> Optional[Dict]:
    source = _find(entry_id)
    if source is None:
        return None
    local_dir, meta = source
    entry = _entry_from_manifest(local_dir, meta)
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    main_page = next(
        (
            page for page in pages
            if isinstance(page, dict)
            and str(page.get("relpath") or "").casefold() == "wiki/main_page.md"
        ),
        next((page for page in pages if isinstance(page, dict)), None),
    )
    entry.update({
        "description": f"Imported MediaWiki XML source for {entry['name']}",
        "main_page_path": str(main_page.get("relpath") or "") if main_page else None,
    })
    return entry


def get_by_start_url(start_url: str) -> Optional[Dict]:
    normalized = start_url.strip()
    for local_dir, meta in _sources():
        if str(meta.get("start_url") or "").strip() == normalized:
            return _entry_from_manifest(local_dir, meta)
    return None


def page_index(entry_id: str, offset: int = 0, limit: int = 500) -> Dict:
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    _, meta = source
    pages = [
        {
            "path": str(page.get("relpath") or ""),
            "title": str(page.get("title") or page.get("relpath") or ""),
            "url": str(page.get("url") or ""),
            "categories": list(page.get("categories") or []),
        }
        for page in (meta.get("pages") or [])
        if isinstance(page, dict) and page.get("relpath")
    ]
    total = len(pages)
    return {
        "entries": pages[offset:offset + limit],
        "offset": offset,
        "truncated": offset + limit < total,
        "totalArticles": total,
    }


def search(entry_id: str, query: str, limit: int = 30) -> List[Dict]:
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    _, meta = source
    needle = query.strip().casefold()
    if not needle:
        return []
    matches = []
    for position, page in enumerate(meta.get("pages") or []):
        if not isinstance(page, dict) or not page.get("relpath"):
            continue
        title = str(page.get("title") or page.get("relpath") or "")
        categories = [str(value) for value in (page.get("categories") or [])]
        searchable = " ".join((title, str(page.get("url") or ""), *categories)).casefold()
        if needle not in searchable:
            continue
        folded_title = title.casefold()
        rank = 0 if folded_title == needle else 1 if folded_title.startswith(needle) else 2
        matches.append((
            rank,
            position,
            {
                "path": str(page["relpath"]),
                "title": title,
                "url": str(page.get("url") or ""),
                "categories": categories,
            },
        ))
    matches.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in matches[:limit]]


def _reader_markdown(content: str) -> str:
    """Remove MediaWiki layout scaffolding that has no meaning in Markdown.

    XML dumps often contain homepage-only table/layout syntax and inputbox
    controls. The importer deliberately preserves their useful cell text, but
    showing raw ``{|``, ``|-`` and ``style=...`` tokens makes the direct
    reader look broken. This presentation pass is non-destructive (the source
    Markdown on disk remains untouched for RAG/export).
    """
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    content = re.sub(r"<inputbox\b[^>]*>.*?</inputbox>", "", content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r"</?mainpage[-\w]*\s*/?>", "", content, flags=re.IGNORECASE)
    content = re.sub(
        r"<h([1-6])\b[^>]*>(.*?)</h\1>",
        lambda match: f"\n{'#' * int(match.group(1))} {match.group(2).strip()}\n",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</?(?:center|small|div)\b[^>]*>", "", content, flags=re.IGNORECASE)
    lines: List[str] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("{|") or stripped == "|}" or stripped.startswith("|-"):
            continue
        line = raw_line
        # A MediaWiki table header starts with ``!``, but a valid Markdown
        # image starts with ``![``. Treating both alike silently converted
        # every imported image embed into a normal text link in the reader.
        if stripped.startswith("|") or (
            stripped.startswith("!") and not stripped.startswith("![")
        ):
            cell = stripped[1:].strip()
            if not cell:
                continue
            if "|" in cell:
                attributes, value = cell.split("|", 1)
                if "=" in attributes:
                    cell = value.strip()
            if not cell:
                continue
            line = cell
        # MediaWiki level-one headings are valid in article bodies but the
        # original converter handled only levels 2-6.
        heading = re.match(r"^=\s*(.+?)\s*=$", line.strip())
        if heading:
            line = f"# {heading.group(1)}"
        if line.strip() == "----":
            # Markdown horizontal rules need whitespace around them or the
            # following paragraph can be parsed as literal markup.
            lines.extend(("", "---", ""))
            continue
        if line.lstrip().startswith("*") and not (
            line.strip().endswith("*") and line.strip().count("*") == 2
        ):
            line = re.sub(r"^(\s*)\*(?=\S)", r"\1* ", line)
        line = line.replace("'''", "**")
        if line.strip().startswith("**") and line.count("**") == 1:
            line = line.rstrip() + "**"
        # External MediaWiki links use `[URL label]`, while Markdown expects
        # `[label](URL)`.
        line = re.sub(
            r"\[(https?://[^\s\]]+)\s+([^\]]+)\]",
            lambda match: f"[{match.group(2)}]({match.group(1)})",
            line,
        )
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def read_page(entry_id: str, relpath: str) -> Dict:
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    local_dir, meta = source
    page = next(
        (
            candidate for candidate in (meta.get("pages") or [])
            if isinstance(candidate, dict)
            and str(candidate.get("relpath") or "") == relpath
        ),
        None,
    )
    if page is None:
        raise FileNotFoundError(relpath)
    content = _read_page_body(local_dir, relpath)
    # The source files use the crawler's small YAML header. It is useful to
    # RAG/indexing, but it should not appear as a horizontal-rule block in the
    # reader itself.
    content = _reader_markdown(content)
    return {
        "path": relpath,
        "title": str(page.get("title") or relpath),
        "url": str(page.get("url") or ""),
        "categories": list(page.get("categories") or []),
        "content": content,
    }


def _safe_source_path(local_dir: str, relpath: str) -> str:
    """Resolve a manifest path without allowing archive/path traversal."""
    base = os.path.realpath(local_dir)
    candidate = os.path.realpath(os.path.join(base, relpath.replace("/", os.sep)))
    if os.path.commonpath((base, candidate)) != base or not os.path.isfile(candidate):
        raise FileNotFoundError(relpath)
    return candidate


def _read_page_body(local_dir: str, relpath: str) -> str:
    with open(_safe_source_path(local_dir, relpath), "r", encoding="utf-8") as handle:
        content = handle.read()
    return re.sub(
        r"\A---\r?\n.*?\r?\n---\r?\n+",
        "",
        content,
        count=1,
        flags=re.DOTALL,
    )


def _safe_archive_name(value: str) -> str:
    value = re.sub(r'[*"\\/<>:|?]', "-", value)
    value = re.sub(r"[\x00-\x1f]", "", value)
    value = re.sub(r"-{2,}", "-", value).strip().rstrip(".-").strip()
    return value or "Imported Wiki"


def _manifest_pages(meta: Dict) -> List[Dict]:
    return [
        page
        for page in (meta.get("pages") or [])
        if isinstance(page, dict) and str(page.get("relpath") or "").endswith(".md")
    ]


def _write_shared_assets(
    zf: zipfile.ZipFile,
    local_dir: str,
    archive_prefix: str,
) -> List[str]:
    """Copy only the importer's managed media directory into an export."""
    assets_dir = os.path.join(local_dir, "_images")
    if not os.path.isdir(assets_dir):
        return []
    exported: List[str] = []
    for root, dirs, files in os.walk(assets_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for filename in files:
            source = os.path.join(root, filename)
            if os.path.islink(source) or not os.path.isfile(source):
                continue
            relative = os.path.relpath(source, assets_dir).replace(os.sep, "/")
            archive_path = posixpath.join(archive_prefix, relative)
            zf.write(source, archive_path)
            exported.append(relative)
    return exported


def export_obsidian(entry_id: str, output_path: str) -> Dict:
    """Write the complete imported source as a directly usable Obsidian vault.

    Article paths are preserved, so repaired relative links keep working.
    Managed images are included under ``_images`` with the same relationship
    to article files as in the in-app reader.
    """
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    local_dir, meta = source
    entry = _entry_from_manifest(local_dir, meta)
    pages = _manifest_pages(meta)
    vault_name = _safe_archive_name(entry["name"])

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr(f"{vault_name}/.obsidian/app.json", json.dumps({}, indent=2))
        home = [
            f"# {entry['name']}",
            "",
            "Bóveda exportada desde un XML de MediaWiki con HackDeepWiki.",
            "",
            f"- Fuente: {entry['start_url']}",
            f"- Artículos: {len(pages)}",
            f"- Exportada: {datetime.now(timezone.utc).isoformat()}",
            "",
            "## Artículos",
            "",
        ]
        for page in pages:
            relpath = str(page["relpath"])
            note_target = relpath[:-3].replace("\\", "/")
            title = str(page.get("title") or relpath).replace("|", "–")
            home.append(f"- [[{note_target}|{title}]]")
            body = _reader_markdown(_read_page_body(local_dir, relpath))
            zf.writestr(f"{vault_name}/{relpath}", body)
        zf.writestr(f"{vault_name}/Home.md", "\n".join(home) + "\n")
        assets = _write_shared_assets(zf, local_dir, f"{vault_name}/_images")

    return {
        "format": "obsidian",
        "page_count": len(pages),
        "asset_count": len(assets),
        "title": entry["name"],
    }


_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


def _resolve_markdown_target(source_relpath: str, href: str) -> Tuple[Optional[str], str]:
    href = href.strip()
    if not href or href.startswith("#") or re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", href, re.I):
        return None, ""
    path_and_query, separator, fragment = href.partition("#")
    path_only = path_and_query.split("?", 1)[0]
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_relpath), path_only))
    return resolved, f"{separator}{fragment}" if separator else ""


def export_hdwreader(entry_id: str, output_path: str) -> Dict:
    """Write a format-v1 HackDeepWikiReader bundle for an imported wiki."""
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    local_dir, meta = source
    entry = _entry_from_manifest(local_dir, meta)
    pages = _manifest_pages(meta)
    path_to_id = {
        str(page["relpath"]): f"page-{hashlib.sha256(str(page['relpath']).encode('utf-8')).hexdigest()[:20]}"
        for page in pages
    }
    manifest_pages: List[Dict] = []
    rendered_pages: List[Tuple[str, str]] = []

    for page in pages:
        relpath = str(page["relpath"])
        page_id = path_to_id[relpath]
        related: List[str] = []
        body = _reader_markdown(_read_page_body(local_dir, relpath))

        def replace_image(match: re.Match) -> str:
            target, fragment = _resolve_markdown_target(relpath, match.group(2))
            if target and target.startswith("_images/"):
                return f"![{match.group(1)}](../assets/{target[len('_images/'):]}{fragment})"
            return match.group(0)

        def replace_link(match: re.Match) -> str:
            target, fragment = _resolve_markdown_target(relpath, match.group(2))
            target_id = path_to_id.get(target or "")
            if not target_id:
                return match.group(0)
            if target_id not in related:
                related.append(target_id)
            return f"[{match.group(1)}]({target_id}.md{fragment})"

        body = _MARKDOWN_IMAGE_RE.sub(replace_image, body)
        body = _MARKDOWN_LINK_RE.sub(replace_link, body)
        rendered_pages.append((page_id, body))
        manifest_pages.append({
            "id": page_id,
            "title": str(page.get("title") or relpath),
            "importance": "medium",
            "filePaths": [relpath],
            "relatedPages": related,
            "file": f"pages/{page_id}.md",
            "source_url": str(page.get("url") or ""),
            "categories": list(page.get("categories") or []),
        })

    exported_at = datetime.now(timezone.utc).isoformat()
    manifest: Dict = {
        "format_version": 1,
        "app": "hackdeepwikireader",
        "repo_url": entry["start_url"],
        "repo_type": "fanwiki",
        "owner": entry["owner"],
        "repo": entry["repo"],
        "title": entry["name"],
        "description": f"Imported MediaWiki XML source for {entry['name']}",
        "language": str(meta.get("language") or ""),
        "provider": "",
        "model": "",
        "wiki_version": None,
        "exported_at": exported_at,
        "sections": [],
        "root_sections": [],
        "pages": manifest_pages,
        "has_vuln_report": False,
        "has_web_vuln_report": False,
        "source_type": "mediawiki_xml",
    }

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for page_id, body in rendered_pages:
            zf.writestr(f"pages/{page_id}.md", body)
        assets = _write_shared_assets(zf, local_dir, "assets")
        manifest["assets"] = [f"assets/{path}" for path in assets]
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    return {
        "format": "hdwreader",
        "page_count": len(pages),
        "asset_count": len(assets),
        "title": entry["name"],
    }


def export_zim(entry_id: str, output_path: str) -> Dict:
    """Write an offline .zim archive for an imported wiki -- same page/link/
    image resolution as export_hdwreader (path_to_id mapping, regex-based
    markdown link/image rewriting), but targeting the ZIM format instead of
    a zip bundle so any Kiwix-family reader (or this app's own
    HackDeepWikiReader) can browse it with no internet connection and this
    app not even running.
    """
    from api.zim_export import ZimAssetSpec, ZimPageSpec, build_zim, guess_mimetype

    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    local_dir, meta = source
    entry = _entry_from_manifest(local_dir, meta)
    pages = _manifest_pages(meta)
    path_to_id = {
        str(page["relpath"]): f"page-{hashlib.sha256(str(page['relpath']).encode('utf-8')).hexdigest()[:20]}"
        for page in pages
    }

    zim_pages: List[ZimPageSpec] = []
    for page in pages:
        relpath = str(page["relpath"])
        page_id = path_to_id[relpath]
        body = _reader_markdown(_read_page_body(local_dir, relpath))

        def replace_image(match: "re.Match") -> str:
            target, fragment = _resolve_markdown_target(relpath, match.group(2))
            if target and target.startswith("_images/"):
                return f"![{match.group(1)}](../assets/{target[len('_images/'):]}{fragment})"
            return match.group(0)

        def replace_link(match: "re.Match") -> str:
            target, fragment = _resolve_markdown_target(relpath, match.group(2))
            target_id = path_to_id.get(target or "")
            if not target_id:
                return match.group(0)
            return f"[{match.group(1)}](../pages/{target_id}.html{fragment})"

        body = _MARKDOWN_IMAGE_RE.sub(replace_image, body)
        body = _MARKDOWN_LINK_RE.sub(replace_link, body)
        zim_pages.append(ZimPageSpec(page_id=page_id, title=str(page.get("title") or relpath), markdown=body))

    assets_dir = os.path.join(local_dir, "_images")
    zim_assets: List[ZimAssetSpec] = []
    if os.path.isdir(assets_dir):
        for root, dirs, files in os.walk(assets_dir, followlinks=False):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
            for filename in files:
                source_path = os.path.join(root, filename)
                if os.path.islink(source_path) or not os.path.isfile(source_path):
                    continue
                relative = os.path.relpath(source_path, assets_dir).replace(os.sep, "/")
                zim_assets.append(ZimAssetSpec(
                    archive_path=f"assets/{relative}",
                    filepath=source_path,
                    mimetype=guess_mimetype(relative),
                ))

    result = build_zim(
        output_path,
        title=entry["name"],
        description=f"Imported MediaWiki XML source for {entry['name']}",
        language=str(meta.get("language") or "en"),
        creator="HackDeepWiki",
        publisher="HackDeepWiki",
        zim_name=_safe_archive_name(entry["name"]).replace("_", "-").lower() or "fanwiki",
        pages=zim_pages,
        assets=zim_assets,
        index_intro_html=f'<p>Source: <a href="{html.escape(entry["start_url"])}">{html.escape(entry["start_url"])}</a></p>',
    )
    return {
        "format": "zim",
        "page_count": result["page_count"],
        "asset_count": result["asset_count"],
        "title": entry["name"],
    }


def resolve_asset(entry_id: str, relpath: str) -> str:
    source = _find(entry_id)
    if source is None:
        raise KeyError(entry_id)
    local_dir, _ = source
    base = os.path.realpath(local_dir)
    candidate = os.path.realpath(os.path.join(base, relpath.replace("/", os.sep)))
    if os.path.commonpath((base, candidate)) != base or not os.path.isfile(candidate):
        raise FileNotFoundError(relpath)
    if candidate.lower().endswith((".md", ".json")):
        raise FileNotFoundError(relpath)
    return candidate


def _force_tree_writable(top: str) -> None:
    """chmod ``top`` and every directory beneath it to rwx before a recursive
    delete. On POSIX, removing a file needs write permission on the *containing
    directory* -- not on the file -- so read-only files are harmless, but a
    read-only DIRECTORY blocks unlinking its children and makes
    shutil.rmtree abort with PermissionError partway through. Crawled/imported
    fanwiki trees are frequently written with non-writable directories, so walk
    top-down and make each directory writable first. os.walk does not follow
    symlinked directories by default, so this never chmods outside the tree.
    """
    try:
        os.chmod(top, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
    except OSError:
        pass
    for root, dirs, _files in os.walk(top):
        for name in dirs:
            try:
                os.chmod(
                    os.path.join(root, name),
                    stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO,
                )
            except OSError:
                pass


def _force_writable_then_retry(func, path, exc_info):
    """Belt-and-suspenders onerror for shutil.rmtree: if a specific path
    still can't be removed after the directory pre-walk (e.g. a read-only
    file on a platform whose unlink needs file-write permission), force it
    writable and retry the same operation. If it STILL fails (e.g. the path
    is owned by another user), let the error propagate so the caller can
    surface it instead of silently swallowing data.
    """
    try:
        os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
    except OSError:
        pass
    func(path)


def delete(start_url: str) -> bool:
    """Delete only a verified fanwiki source tree and its embeddings cache."""
    local_dir = website_local_dir(start_url)
    meta = read_site_meta(local_dir)
    if not meta or not _is_fanwiki_manifest(meta):
        return False
    if str(meta.get("start_url") or "").strip() != start_url.strip():
        return False

    # Imported/crawled trees often contain non-writable directories, which
    # makes a plain shutil.rmtree abort with PermissionError (previously a
    # 500). Make every directory writable first, then rmtree with an onerror
    # that chmods-and-retries any residual read-only path.
    _force_tree_writable(local_dir)
    shutil.rmtree(local_dir, onerror=_force_writable_then_retry)
    repo_name = os.path.basename(local_dir)
    database_path = os.path.join(get_data_root(), "databases", f"{repo_name}.pkl")
    try:
        os.remove(database_path)
    except FileNotFoundError:
        pass
    return True
