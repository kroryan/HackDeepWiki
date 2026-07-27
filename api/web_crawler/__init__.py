"""Website crawler: turns a live website into a local directory of Markdown
files laid out to mirror the site's own URL structure, so the existing wiki
generation pipeline (structure inference, RAG, page citation) can treat a
crawled site exactly like a cloned code repo -- no separate generation path.

    https://example.com/blog/post-1
        -> <data_root>/repos/website_example.com/blog/post-1.md

Each Markdown file carries a small YAML front-matter block (url, title,
crawled_at, is_user_content) so downstream code (category split, citation
links back to the live URL) doesn't have to re-derive it from the path.

Pure Playwright (headless Chromium) for fetching -- the user chose this over
a lighter requests-only crawler specifically to handle JS-rendered sites.
BeautifulSoup + markdownify do the HTML -> Markdown conversion.
"""

from api.web_crawler.crawler import crawl_site
from api.web_crawler.models import CrawlPage, CrawlProgress, CrawlScope

__all__ = ["CrawlPage", "CrawlScope", "CrawlProgress", "crawl_site"]
