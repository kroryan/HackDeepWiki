"""Import MediaWiki XML export dumps (Special:Export -- the format Fandom/
Wikia and any other MediaWiki-based fan wiki produce) as a local Markdown
tree, using the exact same on-disk shape ``api.web_crawler.site_store``
already writes for a crawled website (front-matter pages + ``_site_meta.json``
manifest). That means an imported fanwiki dump is handed to the *same*
wiki-generation code path (RAG indexing, page citation, chat) as a
live-crawled site, just under a synthetic start URL (the dump's own
``<base>``) instead of one Playwright actually visited -- and it sidesteps
Cloudflare/bot-protection entirely, since nothing is fetched over the
network.

These dumps can be multi-gigabyte (a "full history" export repeats every
past revision of every page), so everything here streams:
  * ``inspect_dump`` only reads up to the end of ``<siteinfo>`` to list the
    wiki's namespaces for the user to choose from -- it never touches the
    (potentially huge) rest of the file.
  * ``import_dump`` uses ``ElementTree.iterparse`` + explicit
    ``element.clear()`` after each ``<page>``, so at most one page's
    revision history is resident at a time, never the whole dump.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO, Callable, Dict, List, Optional, Set
from urllib.parse import quote

import mwparserfromhell

from api.web_crawler.models import CrawlPage
from api.web_crawler.site_store import (
    page_to_relpath, read_site_meta, website_local_dir, write_page, write_site_meta,
)

logger = logging.getLogger(__name__)

# The two namespaces a "content wiki" is almost always about: articles
# themselves, and the categories that organize them. Everything else
# (Talk/User/File/Template/Forum/Message Wall/...) is noise for wiki
# generation purposes -- but which namespaces to keep is the user's call
# (see the namespaces param below), not hardcoded.
MEDIAWIKI_MAIN_NS = 0
MEDIAWIKI_CATEGORY_NS = 14

_CATEGORY_PREFIX_RE = re.compile(r'^\s*category\s*:', re.IGNORECASE)
_FILE_PREFIX_RE = re.compile(r'^\s*(file|image)\s*:', re.IGNORECASE)

# Params after the filename in [[File:x.png|thumb|left|300px|Caption text]]
# that are display keywords, not a caption -- MediaWiki convention is that
# the caption (if any) is whichever trailing param isn't one of these and
# isn't a size ("300px"/"x300px") or a "key=value" option.
_FILE_LINK_KEYWORDS = {
    'thumb', 'thumbnail', 'frame', 'framed', 'frameless', 'border',
    'left', 'right', 'center', 'centre', 'none', 'upright',
    'baseline', 'sub', 'super', 'top', 'text-top', 'middle',
    'bottom', 'text-bottom',
}
_FILE_LINK_SIZE_RE = re.compile(r'^\d*x?\d+px$', re.IGNORECASE)
_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.tif', '.tiff')

# Internal (non-file, non-category) [[wikilinks]] can't be resolved to a real
# relative Markdown link during the streaming import itself: the target page
# might not have been written yet (it could come later in the dump, or even
# in a separate import batch), and this pass never holds the whole page
# index in memory anyway (that's the whole point of streaming a 7GB file).
# So each one is left as this marker instead of plain display text, and
# repair_internal_links() below -- run once the *full* page index is known,
# either automatically at the end of import_dump or re-run standalone by the
# user later -- resolves it into a real link (or degrades to plain text for
# a genuine red link, exactly like MediaWiki itself does). Unicode Private
# Use Area code points: guaranteed never to appear in real wikitext, so
# there's no risk of colliding with actual page content the way any
# printable-ASCII marker syntax theoretically could.
_WIKILINK_MARK_START = ""
_WIKILINK_MARK_SEP = ""
_WIKILINK_MARK_END = ""
_WIKILINK_MARK_RE = re.compile(
    f'{_WIKILINK_MARK_START}(.*?){_WIKILINK_MARK_SEP}(.*?){_WIKILINK_MARK_END}',
    re.DOTALL,
)


def mediawiki_filename_key(name: str) -> str:
    """MediaWiki filename normalization for matching a [[File:...]] reference
    against an actual file on disk: the first letter is auto-capitalized by
    MediaWiki regardless of how it's written in wikitext, and spaces/
    underscores are interchangeable ("Aleena Paladinstar.png" and
    "Aleena_Paladinstar.png" are the same file)."""
    name = name.strip().replace(' ', '_')
    if name:
        name = name[0].upper() + name[1:]
    return name.lower()


def build_image_index(images_dir: str) -> Dict[str, str]:
    """Maps a normalized filename to the actual file path in ``images_dir``
    (a user-supplied folder of images downloaded separately -- a MediaWiki
    XML export never includes media, only text referencing filenames)."""
    index: Dict[str, str] = {}
    if not images_dir or not os.path.isdir(images_dir):
        return index
    for entry in os.listdir(images_dir):
        full = os.path.join(images_dir, entry)
        if os.path.isfile(full) and entry.lower().endswith(_IMAGE_EXTENSIONS):
            index[mediawiki_filename_key(entry)] = full
    return index


def _file_link_caption(raw_text: str) -> str:
    """Best-effort caption extraction from a file link's pipe-separated
    trailing params (see _FILE_LINK_KEYWORDS docstring above)."""
    if not raw_text:
        return ""
    parts = [p.strip() for p in raw_text.split('|')]
    last = parts[-1] if parts else ""
    if not last:
        return ""
    lowered = last.lower()
    if (lowered in _FILE_LINK_KEYWORDS or _FILE_LINK_SIZE_RE.match(lowered)
            or re.match(r'^(alt|link|lang|class|page)\s*=', lowered)):
        return ""
    return last


def _strip_ns_prefix(tag: str) -> str:
    """'{http://www.mediawiki.org/xml/export-0.11/}page' -> 'page'."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    return next((c for c in elem if _strip_ns_prefix(c.tag) == name), None)


@dataclass
class MediaWikiNamespace:
    key: int
    name: str  # "" for the main/article namespace itself


@dataclass
class DumpInfo:
    sitename: str
    base_url: str
    dbname: str
    namespaces: List[MediaWikiNamespace]
    file_size: int


def inspect_dump(path: str) -> DumpInfo:
    """Read just enough of the file to list its namespaces for the import UI.

    Stops as soon as ``</siteinfo>`` closes (that element is always small --
    a namespace list, never page content), regardless of how large the rest
    of the dump is, so this is fast even against a multi-GB file.
    """
    sitename = ""
    base_url = ""
    dbname = ""
    namespaces: List[MediaWikiNamespace] = []
    with open(path, "rb") as fh:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            tag = _strip_ns_prefix(elem.tag)
            if tag == "sitename":
                sitename = elem.text or ""
            elif tag == "base":
                base_url = elem.text or ""
            elif tag == "dbname":
                dbname = elem.text or ""
            elif tag == "namespace":
                key = int(elem.get("key", "0"))
                namespaces.append(MediaWikiNamespace(key=key, name=elem.text or ""))
            elif tag == "siteinfo":
                break
    return DumpInfo(
        sitename=sitename or dbname or os.path.splitext(os.path.basename(path))[0],
        base_url=base_url,
        dbname=dbname,
        namespaces=namespaces,
        file_size=os.path.getsize(path),
    )


def _convert_nested(fragment) -> None:
    """In-place: convert templates and <ref> tags within a Wikicode fragment
    (typically one template parameter's value) so a citation template nested
    inside an infobox parameter -- e.g. ``{{Infobox|refs=<ref>{{Cite
    book|...}}</ref>}}`` -- doesn't survive as raw, unconverted "{{...}}"
    wikitext. ``_render_template`` calls this on every parameter's value,
    which in turn calls ``_render_template`` again for any template found
    there -- natural recursion, bottoming out once a fragment has no more
    nested templates/refs, so arbitrarily deep nesting is handled without
    the top-level ``filter_templates()`` pass (which only sees a nested
    template *before* its parent gets replaced, if it visits the parent
    first) needing to track visit order at all.
    """
    for template in fragment.filter_templates(recursive=True):
        try:
            fragment.replace(template, _render_template(template))
        except ValueError:
            pass
    for tag in fragment.filter_tags(recursive=True):
        if str(tag.tag).lower() == 'ref':
            try:
                inner = str(tag.contents).strip() if tag.contents else ''
                fragment.replace(tag, f" (nota: {inner})" if inner else "")
            except ValueError:
                pass


def _render_template(template) -> str:
    """Templates can't be expanded (a page dump doesn't include the site's
    template definitions), but most fan-wiki articles only use them for
    infoboxes -- rendering "Param: value" per named parameter keeps that
    information instead of silently discarding it. A formatting macro like
    {{convert|5|mi|km}} just degrades to its raw parameter list, which is
    still more useful to an LLM synthesizing a wiki page than either the
    literal template call or nothing at all.
    """
    name = str(template.name).strip()
    lines = [f"**{name}**"]
    for param in template.params:
        _convert_nested(param.value)
        pvalue = str(param.value).strip()
        if pvalue:
            lines.append(f"- {str(param.name).strip()}: {pvalue}")
    return ("\n".join(lines) + "\n\n") if len(lines) > 1 else ""


def wikitext_to_markdown(
    wikitext: str,
    image_index: Optional[Dict[str, str]] = None,
    images_relprefix: str = "",
) -> "tuple[str, List[str], List[tuple[str, str]]]":
    """Best-effort wikitext -> Markdown conversion. Returns ``(markdown,
    categories, images_used)``:
      * ``categories`` -- extracted separately since MediaWiki treats
        ``[[Category:X]]`` as page metadata, not inline body content.
      * ``images_used`` -- ``(source_path, dest_filename)`` pairs the caller
        must actually copy into the shared images folder (kept out of this
        function so it stays pure/testable -- see import_dump).

    This is deliberately not a full MediaWiki renderer (that needs a live
    template-expansion engine this import has no access to): the goal is
    clean-enough text for an LLM to write a wiki page from, not
    pixel-perfect rendering. In order:
      * ``[[Category:X]]`` extracted, removed from the body
      * ``[[File:...]]`` / ``[[Image:...]]`` -> a real Markdown image embed
        (``![caption](images_relprefix/filename)``) when ``image_index``
        has a matching file (see build_image_index -- the XML dump itself
        never includes media, only text referencing filenames); otherwise
        degrades to an italic placeholder noting the missing file so the
        information "there was an image here" isn't silently lost
      * other ``[[Link|Text]]`` -> its display text (plain text, not a
        hyperlink -- nothing downstream resolves MediaWiki page-title link
        targets)
      * ``{{templates}}`` -> a "Param: value" block (see _render_template)
      * ``<ref>...</ref>`` -> inlined as "(nota: ...)"; other tags' own
        content is kept, the tags themselves stripped
      * heading/bold/italic wikitext markup -> Markdown equivalents
    """
    code = mwparserfromhell.parse(wikitext)
    categories: List[str] = []
    images_used: List[tuple[str, str]] = []

    for link in code.filter_wikilinks():
        title = str(link.title)
        if _CATEGORY_PREFIX_RE.match(title):
            categories.append(_CATEGORY_PREFIX_RE.sub('', title).strip())
            try:
                code.remove(link)
            except ValueError:
                pass

    for link in code.filter_wikilinks():
        title = str(link.title)
        if not _FILE_PREFIX_RE.match(title):
            continue
        filename = _FILE_PREFIX_RE.sub('', title).strip()
        caption = _file_link_caption(str(link.text)) if link.text else ""
        key = mediawiki_filename_key(filename)
        source_path = (image_index or {}).get(key)
        try:
            if source_path:
                dest_filename = os.path.basename(source_path)
                images_used.append((source_path, dest_filename))
                alt = caption or filename
                rel = f"{images_relprefix}{dest_filename}" if images_relprefix else dest_filename
                code.replace(link, f"![{alt}]({rel})")
            else:
                # No matching file supplied -- keep the fact that an image
                # belongs here instead of silently vanishing.
                code.replace(link, f"*(imagen no disponible: {caption or filename})*")
        except ValueError:
            pass

    for link in code.filter_wikilinks():
        try:
            title = str(link.title).strip()
            text = str(link.text).strip() if link.text else title
            code.replace(link, f"{_WIKILINK_MARK_START}{title}{_WIKILINK_MARK_SEP}{text}{_WIKILINK_MARK_END}")
        except ValueError:
            pass

    # recursive=True (the default) so a template nested inside another node
    # -- most commonly a citation template inside a <ref> tag, e.g.
    # <ref>{{Cite book/Foo|148}}</ref> -- gets converted too, instead of
    # surviving as literal, unconverted "{{...}}" wikitext once the <ref>
    # tag's raw contents are inlined further down.
    for template in code.filter_templates(recursive=True):
        try:
            code.replace(template, _render_template(template))
        except ValueError:
            pass

    for tag in code.filter_tags(recursive=True):
        tag_name = str(tag.tag).lower()
        try:
            if tag_name == 'ref':
                inner = str(tag.contents).strip() if tag.contents else ''
                code.replace(tag, f" (nota: {inner})" if inner else "")
            elif tag_name in ('nowiki', 'gallery', 'poem'):
                code.replace(tag, str(tag.contents) if tag.contents else '')
        except ValueError:
            pass

    text = str(code)

    # Headings: longest (======) first so a shorter pattern can't partially
    # match inside a longer one.
    for level, marker in ((6, '======'), (5, '====='), (4, '===='), (3, '==='), (2, '==')):
        text = re.sub(
            rf'(?m)^{marker}\s*(.+?)\s*{marker}$',
            lambda m, lvl=level: f"{'#' * lvl} {m.group(1)}",
            text,
        )

    # Bold/italic: 5-quote (bold+italic) before 3 before 2, so the shorter
    # patterns don't eat into a longer run first.
    text = re.sub(r"'''''(.+?)'''''", r'***\1***', text)
    text = re.sub(r"'''(.+?)'''", r'**\1**', text)
    text = re.sub(r"''(.+?)''", r'*\1*', text)

    return text.strip(), categories, images_used


@dataclass
class ImportProgress:
    message: str
    pages_done: int
    bytes_done: int
    bytes_total: int
    percent: Optional[int] = None


# Synchronous callback -- import_dump runs in a worker thread (it's a long
# blocking loop), so the caller is responsible for bridging this back to its
# own event loop (e.g. asyncio.run_coroutine_threadsafe). See ws_fanwiki_import
# in api/api.py.
ImportProgressCb = Callable[[ImportProgress], None]


def import_dump(
    path: str,
    dump_info: DumpInfo,
    allowed_namespaces: Optional[Set[int]],
    on_progress: Optional[ImportProgressCb] = None,
    fresh: bool = False,
    progress_every: int = 25,
    max_pages: Optional[int] = None,
    images_dir: Optional[str] = None,
) -> Dict:
    """Streams a MediaWiki XML dump into the same local Markdown tree
    ``api.web_crawler.site_store`` writes for a crawled website.

    Args:
        path: Local path to the (possibly multi-GB) MediaWiki export XML.
        dump_info: Result of a prior ``inspect_dump(path)`` call -- reused
            rather than re-read so the caller can show namespaces to the
            user before committing to the full (slow) import.
        allowed_namespaces: Namespace keys to keep. ``None`` means "keep
            every namespace" -- the user's explicit choice to not filter,
            not a default; callers should not silently substitute a filtered
            set when the user asked for everything.
        fresh: Wipe any previous import of this same dump first (mirrors
            website crawl's "fresh" flag), rather than merging into it.
        images_dir: Optional local folder of image files (downloaded
            separately -- the XML dump itself never includes media) to match
            against ``[[File:...]]``/``[[Image:...]]`` references by
            filename. Matched images are copied into ``local_dir/_images/``
            -- i.e. into the app's own portable DATABASE tree, never left
            referencing the user's original folder -- so the import stays
            fully self-contained and portable like everything else this app
            writes under its data root.

    Returns a dict: ``local_dir``, ``page_count``, ``image_count``, ``pages``
    (manifest entries), ``start_url``.
    """
    start_url = dump_info.base_url or f"https://{dump_info.dbname or 'fanwiki'}"
    local_dir = website_local_dir(start_url)
    if fresh and os.path.isdir(local_dir):
        shutil.rmtree(local_dir, ignore_errors=True)
    os.makedirs(local_dir, exist_ok=True)

    image_index = build_image_index(images_dir) if images_dir else {}
    images_dest_dir = os.path.join(local_dir, "_images")
    copied_images: Dict[str, str] = {}  # dest_filename -> already copied (memoized)

    manifest: List[dict] = []
    pages_done = 0
    file_size = dump_info.file_size or os.path.getsize(path)

    fh: BinaryIO = open(path, "rb")
    try:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            if max_pages is not None and pages_done >= max_pages:
                break
            if _strip_ns_prefix(elem.tag) != "page":
                continue

            ns_el = _child(elem, "ns")
            page_ns = int(ns_el.text) if ns_el is not None and ns_el.text else 0

            if allowed_namespaces is not None and page_ns not in allowed_namespaces:
                elem.clear()
                continue

            title_el = _child(elem, "title")
            title = (title_el.text or "").strip() if title_el is not None else ""

            revisions = [c for c in elem if _strip_ns_prefix(c.tag) == "revision"]
            wikitext = ""
            if revisions:
                text_el = _child(revisions[-1], "text")
                wikitext = (text_el.text or "") if text_el is not None else ""

            # Free this page's (possibly many, for a full-history dump)
            # revisions before moving to the next -- the whole point of
            # streaming this instead of a plain ET.parse().
            elem.clear()

            if not title or not wikitext.strip():
                continue

            page_path = "/wiki/" + quote(title.replace(" ", "_"))
            # Placeholder markdown just to compute this page's on-disk
            # relpath (needs only path/url, see page_to_relpath) *before*
            # converting the wikitext, since the conversion needs to know
            # how many "../" bring it back to local_dir/_images/.
            crawl_page = CrawlPage(
                url=start_url.rstrip("/") + page_path,
                path=page_path,
                title=title,
                markdown="",
                depth=0,
                links=[],
                likely_user_content=False,
                status_code=200,
                content_type="text/html",
            )
            relpath = page_to_relpath(crawl_page)
            images_relprefix = "../" * relpath.count("/")

            markdown, categories, images_used = wikitext_to_markdown(
                wikitext, image_index=image_index, images_relprefix=images_relprefix,
            )
            if not markdown.strip():
                continue

            for source_path, dest_filename in images_used:
                if dest_filename not in copied_images:
                    os.makedirs(images_dest_dir, exist_ok=True)
                    try:
                        shutil.copy2(source_path, os.path.join(images_dest_dir, dest_filename))
                    except OSError as exc:
                        logger.warning("Could not copy image %s: %s", source_path, exc)
                        continue
                    copied_images[dest_filename] = source_path

            crawl_page.markdown = markdown
            relpath = write_page(local_dir, crawl_page)
            manifest.append({
                "relpath": relpath,
                "url": crawl_page.url,
                "title": title,
                "likely_user_content": False,
                "depth": 0,
                "categories": categories,
            })
            pages_done += 1

            if on_progress and pages_done % progress_every == 0:
                bytes_done = fh.tell()
                percent = min(99, int(bytes_done / file_size * 100)) if file_size else None
                on_progress(ImportProgress(
                    message=f"Importadas {pages_done} página(s)… (última: {title})",
                    pages_done=pages_done, bytes_done=bytes_done,
                    bytes_total=file_size, percent=percent,
                ))
    finally:
        fh.close()

    write_site_meta(local_dir, start_url, manifest)
    logger.info("Imported %d page(s) (%d image(s)) from fanwiki dump %s into %s",
                pages_done, len(copied_images), path, local_dir)

    # Internal-link resolution needs the *complete* page index (a link's
    # target may have been written earlier or later in the stream, or not at
    # all if its namespace was filtered out) -- so it always runs as a
    # second pass here, once every page from this import is on disk. Also
    # exposed standalone (see api.py's /ws/fanwiki/repair_links) so the user
    # can re-run it later without re-importing, e.g. after importing a
    # second batch of namespaces into the same site.
    if on_progress:
        on_progress(ImportProgress(
            message="Resolviendo enlaces internos…", pages_done=pages_done,
            bytes_done=file_size, bytes_total=file_size, percent=99,
        ))
    link_result = repair_internal_links(local_dir)

    return {
        "local_dir": local_dir,
        "page_count": pages_done,
        "image_count": len(copied_images),
        "links_resolved": link_result.links_resolved,
        "links_unresolved": link_result.links_unresolved,
        "pages": manifest,
        "start_url": start_url,
    }


def _mediawiki_title_key(title: str) -> str:
    """MediaWiki title normalization for matching a [[wikilink]] target
    against an actual imported page's title: first letter case-insensitively
    capitalized, spaces/underscores interchangeable -- the same rule
    MediaWiki applies to page titles (mirrors mediawiki_filename_key, which
    is the same rule applied to file names)."""
    title = title.strip().replace('_', ' ')
    if title:
        title = title[0].upper() + title[1:]
    return title.lower()


def _relative_link(from_relpath: str, to_relpath: str) -> str:
    """Relative path from one imported page's .md file to another's, for
    embedding directly in a Markdown link. Pure path arithmetic on the two
    (already local_dir-relative) strings -- no filesystem access, so it
    doesn't matter that neither actually exists relative to the process's
    cwd."""
    from_dir = os.path.dirname(from_relpath)
    rel = os.path.relpath(to_relpath, from_dir) if from_dir else to_relpath
    return rel.replace(os.sep, "/")


@dataclass
class LinkRepairResult:
    files_scanned: int
    links_resolved: int
    links_unresolved: int


def repair_internal_links(
    local_dir: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> LinkRepairResult:
    """Second pass over an imported (or partially imported) fanwiki: resolves
    every wikilink placeholder ``import_dump`` left behind (see
    ``_WIKILINK_MARK_*``) into a real relative Markdown link to the target
    page, now that the *complete* page index is available -- it wasn't yet,
    mid-stream, when each individual page was converted, since the target
    could be a page that hadn't been written (or wouldn't be written at all,
    if filtered by namespace) until later in the dump. A link whose target
    genuinely isn't among the imported pages degrades to plain display text
    -- exactly what MediaWiki itself renders for a "red link" to a page that
    doesn't exist, just without the red coloring.

    Safe -- and useful -- to re-run any time: after importing a second batch
    of namespaces into the same site, or just as a manual "repair links"
    action. Already-resolved links are plain Markdown by then, so re-running
    this is a fast no-op scan over files with no remaining markers.
    """
    meta = read_site_meta(local_dir)
    pages = meta.get("pages") or []

    title_index: Dict[str, str] = {}
    for p in pages:
        t, r = p.get("title"), p.get("relpath")
        if t and r:
            title_index[_mediawiki_title_key(t)] = r

    files_scanned = 0
    links_resolved = 0
    links_unresolved = 0

    for root, _dirs, files in os.walk(local_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            full_path = os.path.join(root, fname)
            own_relpath = os.path.relpath(full_path, local_dir).replace(os.sep, "/")
            try:
                with open(full_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue

            if _WIKILINK_MARK_START not in content:
                continue

            def _resolve(m: "re.Match") -> str:
                nonlocal links_resolved, links_unresolved
                title, text = m.group(1), m.group(2)
                target_relpath = title_index.get(_mediawiki_title_key(title))
                if not target_relpath or target_relpath == own_relpath:
                    links_unresolved += 1
                    return text
                links_resolved += 1
                return f"[{text}]({_relative_link(own_relpath, target_relpath)})"

            new_content = _WIKILINK_MARK_RE.sub(_resolve, content)
            files_scanned += 1
            if new_content != content:
                with open(full_path, "w", encoding="utf-8") as fh:
                    fh.write(new_content)

            if on_progress and files_scanned % 200 == 0:
                on_progress(f"Reparando enlaces… {files_scanned} página(s) revisadas")

    logger.info("Link repair for %s: %d file(s) scanned, %d link(s) resolved, %d unresolved",
                local_dir, files_scanned, links_resolved, links_unresolved)
    return LinkRepairResult(
        files_scanned=files_scanned,
        links_resolved=links_resolved,
        links_unresolved=links_unresolved,
    )


# ---------------------------------------------------------------------------
# Export: HackDeepWiki-generated pages -> a MediaWiki-compatible XML dump
# (the reverse direction -- import_dump above reads this same shape). Lets a
# generated wiki round-trip into a real MediaWiki instance, or any other
# tool that already speaks this standard format, instead of only this app's
# own hdwreader/Obsidian formats.
# ---------------------------------------------------------------------------

def _markdown_to_wikitext(markdown: str) -> str:
    """Best-effort Markdown -> wikitext, the reverse of wikitext_to_markdown.
    Not a full round-trip (Markdown here is LLM-generated wiki prose, not
    parsed from an original wikitext AST) -- headings/bold/italic/links map
    cleanly, everything else is left as plain text, which MediaWiki renders
    as-is anyway.
    """
    text = markdown

    # Images: ![alt](src) -> [[File:alt]] (best-effort; src is whatever this
    # app's own generation referenced, not necessarily a real MediaWiki file)
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', lambda m: f"[[File:{m.group(1) or 'image'}]]", text)

    # Links: [text](url) -> external-link wikitext if url looks absolute,
    # otherwise just the display text (an internal Markdown link from
    # generated content rarely maps to a real MediaWiki page title).
    def _link(m: "re.Match") -> str:
        label, url = m.group(1), m.group(2)
        if re.match(r'^https?://', url):
            return f"[{url} {label}]"
        return label
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _link, text)

    # Headings: Markdown '#'*N -> wikitext '='*N (both wrap the text)
    for level in range(6, 0, -1):
        marker = '#' * level
        eq = '=' * level
        text = re.sub(rf'(?m)^{marker}\s+(.+?)\s*$', rf'{eq} \1 {eq}', text)

    # Bold/italic: Markdown -> wikitext quote counts are swapped from HTML
    # conventions but identical to MediaWiki's own (*** = bold+italic,
    # ** = bold, * = italic) -- so this is a direct, position-preserving swap.
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r"'''''\1'''''", text)
    text = re.sub(r'\*\*(.+?)\*\*', r"'''\1'''", text)
    text = re.sub(r'(?<!\*)\*([^*]+?)\*(?!\*)', r"''\1''", text)

    return text


def export_mediawiki_xml(
    pages,  # List[WikiPage]-like objects with .id, .title, .content
    sitename: str,
    base_url: str,
    language: str = "en",
) -> bytes:
    """Builds a MediaWiki export-0.11 compatible XML document from a
    generated wiki's pages -- a single current revision per page, wikitext
    converted from the generated Markdown (see _markdown_to_wikitext).
    Missing fields real MediaWiki dumps carry (contributor, per-revision
    ids/timestamps/sha1) are filled with harmless placeholders; nothing
    downstream (this app's own import, or a real MediaWiki's
    Special:Import) requires them to be authentic to parse the file.
    """
    root = ET.Element("mediawiki", {
        "xmlns": "http://www.mediawiki.org/xml/export-0.11/",
        "version": "0.11",
        "xml:lang": language or "en",
    })
    siteinfo = ET.SubElement(root, "siteinfo")
    ET.SubElement(siteinfo, "sitename").text = sitename
    ET.SubElement(siteinfo, "base").text = base_url
    ET.SubElement(siteinfo, "generator").text = "HackDeepWiki"
    ET.SubElement(siteinfo, "case").text = "first-letter"
    namespaces_el = ET.SubElement(siteinfo, "namespaces")
    ET.SubElement(namespaces_el, "namespace", {"key": "0", "case": "first-letter"}).text = ""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for index, page in enumerate(pages, start=1):
        page_el = ET.SubElement(root, "page")
        ET.SubElement(page_el, "title").text = page.title
        ET.SubElement(page_el, "ns").text = "0"
        ET.SubElement(page_el, "id").text = str(index)
        revision_el = ET.SubElement(page_el, "revision")
        ET.SubElement(revision_el, "id").text = str(index)
        ET.SubElement(revision_el, "timestamp").text = now
        contributor_el = ET.SubElement(revision_el, "contributor")
        ET.SubElement(contributor_el, "username").text = "HackDeepWiki"
        ET.SubElement(contributor_el, "id").text = "0"
        ET.SubElement(revision_el, "model").text = "wikitext"
        ET.SubElement(revision_el, "format").text = "text/x-wiki"
        text_el = ET.SubElement(revision_el, "text", {"bytes": "0", "xml:space": "preserve"})
        text_el.text = _markdown_to_wikitext(page.content or "")

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
