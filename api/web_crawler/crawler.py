"""Headless-browser crawler: fetches a site with Playwright (so JS-rendered
pages come back complete), converts each page to Markdown, and yields
``CrawlPage`` objects breadth-first, same-site-only, respecting the scope
the user picked (page count / explicit subdomain list / whole site).

This module only crawls and converts -- it does not write files. See
``api/web_crawler/site_store.py`` for turning the yielded pages into the
on-disk Markdown tree the wiki pipeline reads.
"""

from __future__ import annotations

import logging
import re
import urllib.robotparser as robotparser
from collections import deque
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from markdownify import markdownify as _html_to_md

from api.web_crawler.models import CrawlPage, CrawlScope, ProgressCb

logger = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 20_000
_MAX_CONCURRENT_PAGES = 4

# URL-path segments that strongly suggest user-generated content (profile
# pages, comment threads, forum posts, ...) rather than site/subject content.
# Heuristic only -- the wiki-structure LLM makes the final client/user-content
# call; this just seeds a hint so it doesn't have to guess from nothing.
_USER_CONTENT_PATH_HINTS = (
    "/user/", "/users/", "/profile/", "/profiles/", "/member/", "/members/",
    "/u/", "/account/", "/comment/", "/comments/", "/forum/", "/forums/",
    "/thread/", "/threads/", "/discussion/", "/discussions/", "/reply/",
    "/replies/", "/talk/",  # MediaWiki-style user talk pages
)

# File extensions that are definitely not HTML pages -- skip without fetching.
_SKIP_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".webm", ".wav",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".eot",
    ".exe", ".dmg", ".apk",
)


def _normalize_url(url: str) -> str:
    """Strip fragments and trailing slashes so the same logical page isn't
    queued twice under trivially different URLs."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _same_site(url: str, root_netloc: str, include_subdomains: bool) -> bool:
    netloc = urlparse(url).netloc
    if include_subdomains:
        return netloc == root_netloc or netloc.endswith("." + root_netloc.split(":")[0])
    return netloc == root_netloc


def _is_user_content(path: str) -> bool:
    lower = f"/{path.strip('/').lower()}/"
    return any(hint in lower for hint in _USER_CONTENT_PATH_HINTS)


def _looks_skippable(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _SKIP_EXTENSIONS)


class _RobotsCache:
    """One ``robotparser`` per (scheme, netloc), fetched lazily and cached
    for the crawl's lifetime -- avoids re-fetching robots.txt per page."""

    def __init__(self, respect: bool):
        self._respect = respect
        self._parsers: dict = {}

    def allowed(self, url: str, user_agent: str = "HackDeepWikiBot") -> bool:
        if not self._respect:
            return True
        parsed = urlparse(url)
        key = (parsed.scheme, parsed.netloc)
        rp = self._parsers.get(key)
        if rp is None:
            rp = robotparser.RobotFileParser()
            robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:  # noqa: BLE001 - missing/broken robots.txt -> allow
                rp = None
            self._parsers[key] = rp
        if rp is None:
            return True
        try:
            return rp.can_fetch(user_agent, url)
        except Exception:  # noqa: BLE001
            return True


def _extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        links.append(urljoin(base_url, href))
    return links


def _page_to_markdown(html: str, url: str) -> Tuple[str, str]:
    """Return (title, markdown). Strips nav/script/style/footer noise before
    converting so the wiki generator's RAG isn't drowned in boilerplate that
    repeats identically on every page (menus, cookie banners, footers)."""
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    title = (title_tag.get_text(strip=True) if title_tag else "") \
        or (h1_tag.get_text(strip=True) if h1_tag else "") \
        or url

    for tag_name in ("script", "style", "noscript", "svg", "iframe"):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    # Common boilerplate landmarks -- best-effort, not present on every site.
    for selector in ("nav", "footer", "[role='navigation']", "[role='banner']",
                      ".cookie-banner", "#cookie-banner", ".cookie-consent"):
        for tag in soup.select(selector):
            tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    markdown = _html_to_md(str(main), heading_style="ATX")
    # Collapse >2 consecutive blank lines left over from stripped elements.
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return title, markdown


async def crawl_site(
    start_url: str,
    scope: CrawlScope,
    on_page,  # async callback: (CrawlPage) -> None, called as each page completes
    on_progress: Optional[ProgressCb] = None,
) -> int:
    """Crawl breadth-first from ``start_url`` within ``scope``, calling
    ``on_page`` for each fetched page as it completes (so the caller can
    stream pages to disk incrementally instead of holding the whole site in
    memory) and ``on_progress`` periodically. Returns the total pages crawled.

    Same-site only: links to other domains are recorded on the page but never
    followed. "Same site" includes subdomains when the crawl was seeded from
    a bare domain (not a specific subdomain), matching how a user would
    intuitively describe "example.com" as one site.
    """
    from playwright.async_api import async_playwright  # heavy import, keep lazy

    root_parsed = urlparse(start_url)
    root_netloc = root_parsed.netloc
    include_subdomains = root_parsed.netloc.count(".") <= 1 or root_parsed.netloc.startswith("www.")
    robots = _RobotsCache(scope.respect_robots)

    seed_urls = [start_url]
    if scope.mode == "subdomains" and scope.subdomains:
        seed_urls = []
        for entry in scope.subdomains:
            entry = entry.strip()
            if not entry:
                continue
            if not entry.startswith(("http://", "https://")):
                entry = f"{root_parsed.scheme}://{entry}"
            seed_urls.append(entry)

    max_pages = scope.hard_cap
    if scope.mode == "count":
        max_pages = min(scope.max_pages, scope.hard_cap)
    elif scope.mode == "all":
        max_pages = scope.hard_cap
    # mode == "subdomains": each seed crawls up to hard_cap collectively too

    visited: Set[str] = set()
    queue: deque = deque((u, 0) for u in seed_urls)
    pages_done = 0

    async def _p(msg: str) -> None:
        # Always logged (console + logfile) so a long crawl is visible in
        # real time to anyone watching the terminal, not just the frontend.
        logger.info("[web-crawl] %s (%d/%d pages)", msg, pages_done, max_pages)
        if on_progress:
            from api.web_crawler.models import CrawlProgress
            try:
                await on_progress(CrawlProgress(
                    message=msg, pages_done=pages_done,
                    pages_total_estimate=max_pages,
                    percent=min(99, int(pages_done / max_pages * 100)) if max_pages else None,
                ))
            except Exception:  # noqa: BLE001 - progress must never break the crawl
                pass

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; HackDeepWikiBot/1.0; +https://github.com/kroryan/HackDeepWiki)",
        )
        try:
            await _p(f"Starting crawl of {root_netloc}…")
            while queue and pages_done < max_pages:
                url, depth = queue.popleft()
                norm = _normalize_url(url)
                if norm in visited:
                    continue
                visited.add(norm)
                if depth > scope.max_depth:
                    continue
                if not _same_site(url, root_netloc, include_subdomains):
                    continue
                if _looks_skippable(url):
                    continue
                if not robots.allowed(url):
                    logger.debug("robots.txt disallows %s", url)
                    continue

                page = await context.new_page()
                try:
                    resp = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="networkidle")
                    status = resp.status if resp else 0
                    content_type = (resp.headers.get("content-type", "") if resp else "") or "text/html"
                    if status >= 400 or "text/html" not in content_type:
                        continue
                    html = await page.content()
                except Exception as exc:  # noqa: BLE001 - one bad page must never abort the crawl
                    logger.debug("Failed to fetch %s: %s", url, exc)
                    continue
                finally:
                    await page.close()

                title, markdown = _page_to_markdown(html, url)
                if not markdown.strip():
                    continue

                soup_links = BeautifulSoup(html, "html.parser")
                links = _extract_links(soup_links, url)
                same_site_links = [
                    l for l in links
                    if _same_site(l, root_netloc, include_subdomains) and not _looks_skippable(l)
                ]

                crawl_page = CrawlPage(
                    url=url,
                    path=urlparse(url).path or "/",
                    title=title,
                    markdown=markdown,
                    depth=depth,
                    links=same_site_links,
                    likely_user_content=_is_user_content(urlparse(url).path),
                    status_code=status,
                    content_type=content_type,
                )
                pages_done += 1
                await on_page(crawl_page)
                if pages_done % 5 == 0 or pages_done == 1:
                    await _p(f"Crawled {pages_done} page(s)… (last: {crawl_page.path})")

                for link in same_site_links:
                    if _normalize_url(link) not in visited:
                        queue.append((link, depth + 1))
        finally:
            await context.close()
            await browser.close()

    await _p(f"Crawl complete: {pages_done} page(s).")
    return pages_done
