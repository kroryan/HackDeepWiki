"""Ties the crawler + site_store together: crawl a site, write each page to
disk as it arrives, and produce the manifest wiki generation reads.

Mirrors the shape of ``api.vuln_scanner.orchestrator.run_vuln_scan`` (an
async function that takes an ``on_progress`` callback and does the
synchronous/heavy work via the crawler's own async Playwright loop) so the
websocket handler in api.py can follow the same streaming pattern already
used for repo clone and vuln scan.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Dict, List, Optional

from api.web_crawler.crawler import crawl_site
from api.web_crawler.models import CrawlPage, CrawlScope, ProgressCb
from api.web_crawler.site_store import website_local_dir, write_page, write_site_meta

logger = logging.getLogger(__name__)


async def run_site_crawl(
    start_url: str,
    scope: CrawlScope,
    on_progress: Optional[ProgressCb] = None,
    fresh: bool = False,
) -> Dict:
    """Crawl ``start_url`` and persist it as a local Markdown tree.

    ``fresh`` wipes any previous crawl of this site first (used by "refresh
    wiki" the same way a git repo refresh re-pulls) -- without it, a re-crawl
    just overwrites/adds pages, leaving stale pages from a shrunk site behind.

    Returns a dict with ``local_dir``, ``page_count``, and ``pages`` (the
    manifest list also written to ``_site_meta.json``).
    """
    local_dir = website_local_dir(start_url)
    if fresh and os.path.isdir(local_dir):
        shutil.rmtree(local_dir, ignore_errors=True)
    os.makedirs(local_dir, exist_ok=True)

    manifest: List[dict] = []

    async def _on_page(page: CrawlPage) -> None:
        relpath = write_page(local_dir, page)
        manifest.append({
            "relpath": relpath,
            "url": page.url,
            "title": page.title,
            "likely_user_content": page.likely_user_content,
            "depth": page.depth,
        })

    diagnostics: Dict = {}
    page_count = await crawl_site(start_url, scope, _on_page, on_progress, diagnostics=diagnostics)
    write_site_meta(local_dir, start_url, manifest)

    logger.info("Crawled %d page(s) from %s into %s", page_count, start_url, local_dir)
    return {"local_dir": local_dir, "page_count": page_count, "pages": manifest, "diagnostics": diagnostics}
