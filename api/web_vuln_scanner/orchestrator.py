"""Ties the deterministic checks + fingerprinting + optional LLM cross-check
together into a WebVulnReport.

Unlike the dependency vuln_scanner (which reads already-cloned files off
disk), this needs live HTTP responses -- headers, cookies, raw HTML for
fingerprinting -- which the crawler didn't keep (it only persists converted
Markdown). So this makes its own lightweight ``requests`` pass over a sample
of the already-crawled URLs (read from the crawl manifest) instead of
re-crawling with Playwright: header/cookie/exposure checks don't need JS
rendering, and doing this with plain HTTP keeps the scan fast and avoids a
second browser-automation pass over the whole site.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional
from urllib.parse import urlparse

import requests

from api.web_crawler.site_store import read_site_meta, website_local_dir
from api.web_vuln_scanner.checks import (
    check_cookies,
    check_exposed_paths,
    check_headers,
    check_mixed_content,
    check_tls,
)
from api.web_vuln_scanner.docker_tools import run_docker_toolkit
from api.web_vuln_scanner.fingerprint import fingerprint_page, js_libs_to_osv_queries, known_server_cves
from api.web_vuln_scanner.models import SEVERITY_RANKS, WebFinding, WebVulnReport
from api.web_vuln_scanner.portscan import scan_ports

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, Optional[int]], Awaitable[None]]

_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (compatible; HackDeepWikiBot/1.0; security-check)"
# Sample size for the header/cookie/fingerprint pass -- checking every page
# on a large crawl adds little signal (headers are near-identical site-wide)
# for a lot more requests, so a representative sample keeps this fast.
_MAX_PAGES_TO_CHECK = 15


def _dedupe_site_wide(findings: List[WebFinding]) -> List[WebFinding]:
    """Headers and cookie flags are near-always identical across every page
    of a site (same server config, same session cookie) -- checking N pages
    would otherwise report the exact same "missing HSTS" finding N times.
    Collapse to one finding per (category, id) for headers/cookies, keeping
    the first URL seen and noting how many pages exhibited it. Exposure/TLS/
    CVE findings are already checked once site-wide, so they pass through
    unchanged.
    """
    deduped: List[WebFinding] = []
    seen: dict = {}
    for f in findings:
        if f.category not in ("headers", "cookies"):
            deduped.append(f)
            continue
        key = (f.category, f.id)
        if key in seen:
            seen[key][1] += 1
            continue
        seen[key] = [f, 1]

    for f, count in seen.values():
        if count > 1:
            f.description = f"{f.description} (observed on {count} pages checked)".strip()
        deduped.append(f)
    return deduped


def _fetch_page(url: str) -> Optional[requests.Response]:
    try:
        return requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}, allow_redirects=True)
    except requests.RequestException as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


def _query_osv_for_js_libs(queries: List[dict]) -> List[WebFinding]:
    """Reuse api.vuln_scanner's OSV client for the npm-mapped JS libraries
    detected by the fingerprinter, converting its CVEFinding results into
    WebFinding (category='cve')."""
    if not queries:
        return []
    from api.vuln_scanner.models import Dependency
    from api.vuln_scanner.osv_client import query_vulnerabilities

    deps = [Dependency(name=q["name"], version=q["version"], ecosystem="npm") for q in queries]
    try:
        cve_findings = query_vulnerabilities(deps, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OSV lookup for detected JS libs failed: %s", exc)
        return []

    out = []
    for f in cve_findings:
        out.append(WebFinding(
            id=f"js-lib-cve-{f.id.lower()}",
            category="cve",
            severity=f.severity,
            title=f"{f.id}: {f.package_name}@{f.installed_version}",
            description=f.summary or f.details[:300],
            evidence=f"{f.package_name}@{f.installed_version}",
            cve_id=f.id,
            cvss_score=f.cvss_score,
            technology=f"{f.package_name}@{f.installed_version}",
            technology_version=f.installed_version,
            remediation=(f"Upgrade {f.package_name} to {f.fixed_version}." if f.fixed_version
                        else "No fixed version published yet; monitor the advisory."),
            references=f.references,
        ))
    return out


def _build_report(site_url: str, owner: str, repo: str, language: str,
                  provider: str, model: str, pages_scanned: int,
                  findings: List[WebFinding], technologies: List[str],
                  ai_analyzed: bool) -> WebVulnReport:
    counts = {s: 0 for s in SEVERITY_RANKS}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    def _split(cat: str) -> List[dict]:
        items = [f for f in findings if f.category == cat]
        items.sort(key=lambda f: SEVERITY_RANKS.index(f.severity) if f.severity in SEVERITY_RANKS else 99)
        return [f.to_dict() for f in items]

    return WebVulnReport(
        site_url=site_url, owner=owner, repo=repo, language=language,
        generated_at=datetime.now(timezone.utc).isoformat(),
        provider=provider, model=model or "",
        pages_scanned=pages_scanned,
        counts=counts,
        total_findings=len(findings),
        header_findings=_split("headers"),
        cookie_findings=_split("cookies"),
        tls_findings=_split("tls"),
        exposure_findings=_split("exposure"),
        cve_findings=_split("cve"),
        all_findings=[f.to_dict() for f in findings],
        detected_technologies=[{"name": t} for t in technologies],
        ai_analyzed=ai_analyzed,
    )


async def run_web_vuln_scan(
    *,
    site_url: str,
    owner: str,
    repo: str,
    language: str,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    api_endpoint: Optional[str],
    run_llm: bool = True,
    enable_deep_scan: bool = False,
    on_progress: Optional[ProgressCb] = None,
) -> WebVulnReport:
    async def _p(msg: str, pct: Optional[int] = None) -> None:
        # Always logged (console + logfile, see api.logging_config), not
        # just streamed to the frontend -- so anyone watching the terminal
        # (AppImage console, docker logs, etc.) sees real-time scan
        # progress even without the browser open.
        logger.info("[web-vuln-scan] %s%s", msg, f" ({pct}%)" if pct is not None else "")
        if on_progress:
            try:
                await on_progress(msg, pct)
            except Exception:  # noqa: BLE001
                pass

    local_dir = website_local_dir(site_url)
    meta = read_site_meta(local_dir)
    pages_meta = meta.get("pages") or []
    urls = [p["url"] for p in pages_meta if isinstance(p, dict) and p.get("url")] or [site_url]
    sample = urls[:_MAX_PAGES_TO_CHECK]

    await _p(f"Checking {len(sample)} page(s) and scanning common ports…", 10)

    findings: List[WebFinding] = []
    technologies: set = set()
    js_lib_queries: List[dict] = []
    checked_server_headers: set = set()

    def _scan_sync() -> None:
        for url in sample:
            resp = _fetch_page(url)
            if resp is None:
                continue
            findings.extend(check_headers(url, dict(resp.headers)))
            findings.extend(check_cookies(url, resp))
            findings.extend(check_mixed_content(url, resp.text))

            server = resp.headers.get("Server")
            if server and server not in checked_server_headers:
                checked_server_headers.add(server)
                findings.extend(known_server_cves(server))

            detected = fingerprint_page(resp.text, dict(resp.headers))
            for name, version in detected:
                technologies.add(f"{name}@{version}" if version else name)
            js_lib_queries.extend(js_libs_to_osv_queries(detected))

        # TLS + exposed-path + port checks only need to run once against the
        # root domain -- these are server/route config, not per-page.
        parsed = urlparse(site_url)
        hostname = parsed.netloc.split(":")[0]
        if parsed.scheme == "https":
            findings.extend(check_tls(hostname))
        findings.extend(check_exposed_paths(site_url))
        findings.extend(scan_ports(hostname))

    await asyncio.to_thread(_scan_sync)
    findings = _dedupe_site_wide(findings)
    await _p("Cross-referencing detected technologies with OSV.dev…", 50)

    findings.extend(await asyncio.to_thread(_query_osv_for_js_libs, js_lib_queries))

    # Docker-backed deep scan toolkit (nmap/httpx/whatweb/nikto/testssl.sh/
    # nuclei/subfinder+dnsx/ffuf/dalfox/wpscan -- see docker_tools.py): this
    # is the RedAmon-equivalent pass (real tool-based CORS/HTTP-method/
    # CMS-specific/CVE-template/XSS/subdomain checks that a hand-rolled
    # Python heuristic can't cover reliably). Opt-in (enable_deep_scan) since
    # it requires Docker and a multi-GB image on first use, unlike the
    # always-on pure-Python checks above.
    if enable_deep_scan:
        docker_findings, docker_technologies = await run_docker_toolkit(
            site_url, on_progress, crawled_urls=urls,
        )
        findings.extend(docker_findings)
        technologies.update(docker_technologies)

    ai_analyzed = False
    if run_llm and findings:
        await _p("Running AI cross-check for missed/false-positive CVEs…", 75)
        try:
            from api.web_vuln_scanner.llm_analyzer import analyze_web_findings
            new_findings = await analyze_web_findings(
                findings, sorted(technologies),
                provider=provider, model=model, api_key=api_key,
                api_endpoint=api_endpoint, language=language,
                on_progress=lambda msg: _p(msg, 80),
            )
            findings.extend(new_findings)
            ai_analyzed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Web vuln AI cross-check failed (non-fatal): %s", exc)

    await _p("Building report…", 95)
    report = _build_report(
        site_url=site_url, owner=owner, repo=repo, language=language,
        provider=provider, model=model or "", pages_scanned=len(sample),
        findings=findings, technologies=sorted(technologies), ai_analyzed=ai_analyzed,
    )

    from api.vuln_common.remediation import build_remediation_plan
    report.remediation_plan = build_remediation_plan([f.to_dict() for f in findings]).to_dict()

    from api.web_vuln_scanner.models import build_web_graph
    report.graph = build_web_graph(findings, sorted(technologies), site_url).to_dict()

    # Best-effort graph persistence (Neo4j) -- entirely optional, never
    # blocks or fails the scan if the graph stack isn't running (see
    # docker/vulnscan/docker-compose.yml; the user starts it themselves).
    try:
        from api.web_vuln_scanner.graph_db import try_persist_report
        await asyncio.to_thread(try_persist_report, report)
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_db persistence skipped: %s", exc)

    await _p("Scan complete.", 100)
    return report
