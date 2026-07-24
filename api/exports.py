"""Wiki export format generators, extracted verbatim from ``api/api.py``.

Each generator turns a list of ``WikiPage`` Pydantic models into a bytes
payload in one of the supported export formats (markdown, json, .zim,
Obsidian vault zip, hdwreader portable bundle). They are pure with respect
to app state -- they take all inputs as params and depend only on stdlib +
``api.models`` + two local imports (``api.zim_export`` for the .zim path and
``api.vuln_scanner.obsidian_export`` for the Obsidian Security folder), so
moving them out of the 3800-line route module is mechanical: ``api/api.py``
imports them back, and nothing else in the tree imported them from
``api.api`` (verified).

The only consumer is the ``/export/wiki`` route.
"""

import html as html_lib
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.models import WikiPage, WikiSection

logger = logging.getLogger(__name__)


def generate_markdown_export(repo_url: str, pages: List[WikiPage]) -> str:
    """
    Generate Markdown export of wiki pages.

    Args:
        repo_url: The repository URL
        pages: List of wiki pages

    Returns:
        Markdown content as string
    """
    # Start with metadata
    markdown = f"# Wiki Documentation for {repo_url}\n\n"
    markdown += f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    # Add table of contents
    markdown += "## Table of Contents\n\n"
    for page in pages:
        markdown += f"- [{page.title}](#{page.id})\n"
    markdown += "\n"

    # Add each page
    for page in pages:
        markdown += f"<a id='{page.id}'></a>\n\n"
        markdown += f"## {page.title}\n\n"



        # Add related pages
        if page.relatedPages and len(page.relatedPages) > 0:
            markdown += "### Related Pages\n\n"
            related_titles = []
            for related_id in page.relatedPages:
                # Find the title of the related page
                related_page = next((p for p in pages if p.id == related_id), None)
                if related_page:
                    related_titles.append(f"[{related_page.title}](#{related_id})")

            if related_titles:
                markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"

        # Add page content
        markdown += f"{page.content}\n\n"
        markdown += "---\n\n"

    return markdown


def generate_json_export(repo_url: str, pages: List[WikiPage]) -> str:
    """
    Generate JSON export of wiki pages.

    Args:
        repo_url: The repository URL
        pages: List of wiki pages

    Returns:
        JSON content as string
    """
    # Create a dictionary with metadata and pages
    export_data = {
        "metadata": {
            "repository": repo_url,
            "generated_at": datetime.now().isoformat(),
            "page_count": len(pages)
        },
        "pages": [page.model_dump() for page in pages]
    }

    # Convert to JSON string with pretty formatting
    return json.dumps(export_data, indent=2)


def generate_zim_export(
    repo_url: str,
    pages: List[WikiPage],
    title: str,
    description: str,
    language: str,
) -> bytes:
    """Generate an offline .zim archive of the wiki pages.

    Writes via a temp file (libzim.writer.Creator only writes to a real
    filesystem path, not an in-memory buffer) and reads it back into bytes,
    matching every other export format's `content: bytes` return shape so
    the /export/wiki route stays a single uniform Response(...) call.
    """
    from api.zim_export import ZimPageSpec, build_zim

    fd, tmp_path = tempfile.mkstemp(suffix=".zim")
    os.close(fd)
    try:
        os.remove(tmp_path)  # Creator refuses to write over an existing file
        build_zim(
            tmp_path,
            title=title,
            description=description or title,
            language=language,
            creator="HackDeepWiki",
            publisher="HackDeepWiki",
            zim_name=re.sub(r"[^a-z0-9.]+", "-", title.lower()).strip("-") or "wiki",
            pages=[
                ZimPageSpec(page_id=page.id, title=page.title, markdown=page.content)
                for page in pages
            ],
            index_intro_html=f'<p><a href="{html_lib.escape(repo_url)}">{html_lib.escape(repo_url)}</a></p>',
        )
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


def _obsidian_safe_filename(title: str) -> str:
    """Turn a page title into a safe Obsidian note filename (no extension).

    Obsidian disallows these characters in note names: * " \\ / < > : | ?
    Also strip control chars and trailing dots/spaces (Windows compatibility,
    important because the vault zip must travel across OSes).
    """
    name = re.sub(r'[*"\\/<>:|?]', '-', title)
    name = re.sub(r'[\x00-\x1f]', '', name)
    name = re.sub(r'-{2,}', '-', name)          # collapse runs of dashes
    name = re.sub(r'\s+-\s*$', '', name)        # drop dangling " -" endings
    name = name.strip().rstrip('.-').strip()
    return name or 'Untitled'


def generate_obsidian_vault_export(
    repo_url: str,
    pages: List[WikiPage],
    title: str = "Wiki",
    version: Optional[int] = None,
    vuln_report: Optional[Dict[str, Any]] = None,
    include_vulns: bool = False,
    include_vuln_graph: bool = True,
) -> bytes:
    """Generate a complete Obsidian vault as a .zip (returned as bytes).

    Layout inside the zip:
        <Vault>/
          Home.md                  – index note linking every page with [[wikilinks]]
          <Page Title>.md          – one note per wiki page, YAML frontmatter +
                                     "Related" section using [[wikilinks]]
          .obsidian/app.json       – minimal config so Obsidian opens the folder
                                     as a vault directly after unzipping
          🔐 Security/             – (optional) vulnerability report notes +
                                     Canvas board, when include_vulns is set

    An Obsidian vault is just a folder of Markdown files, so the zip can be
    unzipped anywhere (Windows/macOS/Linux) and opened with
    "Open folder as vault" — matching this project's portability goal.
    """
    vault_name = _obsidian_safe_filename(title)

    # Map page id -> unique safe filename, deduping title collisions.
    id_to_name: Dict[str, str] = {}
    used_names: Dict[str, int] = {}
    for page in pages:
        base = _obsidian_safe_filename(page.title)
        count = used_names.get(base, 0)
        used_names[base] = count + 1
        id_to_name[page.id] = base if count == 0 else f"{base} ({count + 1})"

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Minimal .obsidian config so the unzipped folder is recognized as a vault.
        zf.writestr(f"{vault_name}/.obsidian/app.json", json.dumps({}, indent=2))

        # Home/index note with links to every page.
        home = f"# {title}\n\n"
        home += f"- **Repository:** {repo_url}\n"
        if version:
            home += f"- **Wiki release:** v{version}\n"
        home += f"- **Exported:** {generated_at}\n\n"
        home += "## Pages\n\n"
        for page in pages:
            home += f"- [[{id_to_name[page.id]}]]\n"

        # 🔐 Security section link + folder (optional)
        if include_vulns and vuln_report:
            home += "\n## Security\n\n"
            home += "- [[Security Overview]]\n"
            try:
                from api.vuln_scanner.obsidian_export import build_security_folder
                for rel_path, content in build_security_folder(
                        vuln_report, include_graph=include_vuln_graph).items():
                    zf.writestr(f"{vault_name}/{rel_path}", content)
            except Exception as exc:
                logger.warning("Failed to build Security folder for Obsidian export: %s", exc)

        zf.writestr(f"{vault_name}/Home.md", home)

        # One note per page.
        for page in pages:
            note = "---\n"
            note += f"title: {json.dumps(page.title)}\n"
            note += f"page_id: {json.dumps(page.id)}\n"
            note += f"importance: {json.dumps(page.importance)}\n"
            if version:
                note += f"wiki_release: {version}\n"
            if page.filePaths:
                note += "source_files:\n"
                for fp in page.filePaths:
                    note += f"  - {json.dumps(fp)}\n"
            note += "---\n\n"
            note += f"{page.content}\n"
            related_links = [
                f"[[{id_to_name[rid]}]]"
                for rid in (page.relatedPages or [])
                if rid in id_to_name
            ]
            if related_links:
                note += "\n## Related\n\n"
                note += " · ".join(related_links) + "\n"
            zf.writestr(f"{vault_name}/{id_to_name[page.id]}.md", note)

    return buffer.getvalue()


HDWREADER_FORMAT_VERSION = 1


def generate_hdwreader_export(
    repo_url: str,
    repo_type: str,
    owner: str,
    repo: str,
    pages: List[WikiPage],
    sections: List[WikiSection],
    root_sections: List[str],
    title: str,
    description: str,
    language: str,
    provider: str,
    model: str,
    version: Optional[int] = None,
    vuln_report: Optional[Dict[str, Any]] = None,
    include_vulns: bool = False,
    web_vuln_report: Optional[Dict[str, Any]] = None,
    include_web_vulns: bool = False,
) -> bytes:
    """Generate a portable offline bundle (.zip, ".hdwreader" extension) for
    the HackDeepWikiReader companion app (Android/Linux/Windows). Unlike the
    Obsidian export (human-edited notes with [[wikilinks]]), this is meant to
    be parsed programmatically, so pages are plain per-id Markdown files and
    metadata/hierarchy/security reports are plain JSON -- no Markdown
    frontmatter or wikilink resolution needed on the reading end.

    Layout inside the zip:
        manifest.json                    -- metadata, section tree, page index
        pages/<page-id>.md               -- raw page content, one file per page
        security/vuln_report.json        -- (optional) dependency scan report
        security/web_vuln_report.json    -- (optional) website scan report
    """
    generated_at = datetime.now(timezone.utc).isoformat()

    manifest: Dict[str, Any] = {
        "format_version": HDWREADER_FORMAT_VERSION,
        "app": "hackdeepwikireader",
        "repo_url": repo_url,
        "repo_type": repo_type,
        "owner": owner,
        "repo": repo,
        "title": title,
        "description": description,
        "language": language,
        "provider": provider,
        "model": model,
        "wiki_version": version,
        "exported_at": generated_at,
        "sections": [s.model_dump() for s in sections],
        "root_sections": root_sections,
        "pages": [
            {
                "id": page.id,
                "title": page.title,
                "importance": page.importance,
                "filePaths": page.filePaths,
                "relatedPages": page.relatedPages,
                "file": f"pages/{page.id}.md",
            }
            for page in pages
        ],
        "has_vuln_report": bool(include_vulns and vuln_report),
        "has_web_vuln_report": bool(include_web_vulns and web_vuln_report),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for page in pages:
            zf.writestr(f"pages/{page.id}.md", page.content)
        if include_vulns and vuln_report:
            zf.writestr("security/vuln_report.json", json.dumps(vuln_report, indent=2, ensure_ascii=False))
        if include_web_vulns and web_vuln_report:
            zf.writestr("security/web_vuln_report.json", json.dumps(web_vuln_report, indent=2, ensure_ascii=False))

    return buffer.getvalue()