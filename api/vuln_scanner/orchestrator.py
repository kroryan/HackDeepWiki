"""Scan orchestrator: ties parser + OSV + (optional) NVD + LLM together and
builds the final ``VulnReport``.

The heavy/synchronous work (parsing manifests, OSV HTTP, usage-file grep) is
run via ``asyncio.to_thread`` so the WebSocket handler's event loop stays
responsive. The LLM step is natively async (streaming).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from api.vuln_scanner import dep_parser, llm_analyzer, nvd_client, osv_client
from api.vuln_scanner.models import (
    SEVERITY_RANKS,
    CVEFinding,
    Dependency,
    GraphData,
    VulnReport,
    build_graph,
)
from api.vuln_scanner.prompts import build_stack_summary

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, Optional[int]], Awaitable[None]]


def _enabled_categories(enable_client: bool, enable_server: bool,
                        enable_deps: bool) -> set:
    cats = set()
    if enable_client:
        cats.add("client")
    if enable_server:
        cats.add("server")
    if enable_deps:
        cats.add("dependency")
    return cats or {"client", "server", "dependency"}


async def run_vuln_scan(
    *,
    repo_dir: str,
    repo_url: str,
    repo_type: str,
    owner: str,
    repo: str,
    language: str,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    api_endpoint: Optional[str],
    excluded_dirs: Optional[List[str]] = None,
    excluded_files: Optional[List[str]] = None,
    nvd_key: Optional[str] = None,
    enable_client: bool = True,
    enable_server: bool = True,
    enable_deps: bool = True,
    run_llm: bool = True,
    enable_code_scan: bool = False,
    on_progress: Optional[ProgressCb] = None,
) -> VulnReport:
    """Run the full scan and return a ``VulnReport``.

    ``on_progress`` (if given) is awaited with ``(message, percent)`` at each
    stage so the caller can stream progress to the client.
    """

    async def _p(msg: str, pct: Optional[int] = None) -> None:
        # Always logged (console + logfile) so the scan is visible in
        # real time to anyone watching the terminal, not just the frontend.
        logger.info("[dep-vuln-scan] %s%s", msg, f" ({pct}%)" if pct is not None else "")
        if on_progress:
            try:
                await on_progress(msg, pct)
            except Exception:  # noqa: BLE001 - progress must never break the scan
                pass

    enabled = _enabled_categories(enable_client, enable_server, enable_deps)

    # --- Stage 1: parse dependencies -------------------------------------
    await _p("Scanning dependency manifests…", 5)
    deps: List[Dependency] = await asyncio.to_thread(
        dep_parser.parse_dependencies, repo_dir, excluded_dirs, excluded_files,
    )
    if not deps:
        await _p("No supported dependency manifests found.", 100)
        return _empty_report(repo_url, repo_type, owner, repo, language,
                             provider, model)
    await _p(f"Found {len(deps)} dependencies. Querying OSV.dev…", 20)

    # --- Stage 2: OSV (+NVD enrichment) ----------------------------------
    enricher = nvd_client.make_enricher(nvd_key) if nvd_key else None
    findings: List[CVEFinding] = await asyncio.to_thread(
        osv_client.query_vulnerabilities, deps, enricher,
    )
    await _p(f"OSV returned {len(findings)} vulnerabilities.", 55)

    # --- Stage 3: usage files (only for vulnerable deps) -----------------
    if findings:
        vuln_deps = [d for d in deps
                     if f"{d.ecosystem}:{d.name}" in
                     {f"{f.package_ecosystem}:{f.package_name}" for f in findings}]
        await _p("Locating where vulnerable dependencies are used…", 60)
        usage = await asyncio.to_thread(
            dep_parser.find_usage_files, repo_dir, vuln_deps, excluded_dirs,
        )
        for f in findings:
            f.usage_files = usage.get(f"{f.package_ecosystem}:{f.package_name}", []) \
                or f.usage_files

    # --- Stage 4: filter by enabled categories ---------------------------
    findings = [f for f in findings if f.category in enabled]

    # --- Stage 5: LLM analysis -------------------------------------------
    ai_analyzed = False
    if findings and run_llm:
        await _p("Running AI impact analysis…", 70)
        app_context = build_stack_summary(deps)
        try:
            ai_analyzed = await llm_analyzer.analyze_findings(
                findings,
                repo_dir=repo_dir,
                provider=provider,
                model=model,
                api_key=api_key,
                api_endpoint=api_endpoint,
                language=language,
                app_context=app_context,
                on_progress=(lambda msg, _pct=None: _p(msg, 75)) if on_progress else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM analysis failed (defaults kept): %s", exc)
            await _p(f"AI analysis skipped ({exc}); using defaults.", 90)
    elif findings:
        llm_analyzer._apply_defaults(findings)

    # --- Stage 6: optional Docker-toolkit code scan (gitleaks + semgrep) --
    # Opt-in (Docker required, adds real wall-clock time) -- covers leaked
    # secrets and SAST findings, which OSV/dependency lookups can't surface
    # since those only see declared package versions, not the code itself.
    code_scan_findings: List[dict] = []
    code_scan_ran = False
    if enable_code_scan:
        await _p("Scanning source for leaked secrets and SAST findings…", 80)
        try:
            from api.web_vuln_scanner.docker_tools import run_code_scan_toolkit
            web_findings = await run_code_scan_toolkit(
                repo_dir, on_progress=(lambda msg, pct=None: _p(msg, pct)) if on_progress else None,
            )
            from api.web_vuln_scanner.exploitation_defaults import apply_exploitation_defaults
            apply_exploitation_defaults(web_findings)
            code_scan_findings = [f.to_dict() for f in web_findings]
            code_scan_ran = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Code scan toolkit failed (non-fatal): %s", exc)

    await _p("Building report…", 95)

    # --- Stage 7: build report -------------------------------------------
    report = _build_report(
        findings=findings,
        deps=deps,
        repo_url=repo_url, repo_type=repo_type, owner=owner, repo=repo,
        language=language, provider=provider, model=model,
        ai_analyzed=ai_analyzed,
    )
    report.code_scan_findings = code_scan_findings
    report.code_scan_ran = code_scan_ran

    from api.vuln_common.remediation import build_remediation_plan
    report.remediation_plan = build_remediation_plan(
        [f.to_dict() for f in findings] + code_scan_findings
    ).to_dict()

    from api.vuln_common.exploitation import build_exploitation_plan
    report.exploitation_plan = build_exploitation_plan(
        [f.to_dict() for f in findings] + code_scan_findings
    ).to_dict()

    await _p("Scan complete.", 100)
    return report


def _empty_report(repo_url, repo_type, owner, repo, language, provider, model) -> VulnReport:
    r = VulnReport(
        repo_url=repo_url, repo_type=repo_type, owner=owner, repo=repo,
        language=language, provider=provider, model=model,
        generated_at=datetime.now(timezone.utc).isoformat(),
        counts={s: 0 for s in SEVERITY_RANKS},
    )
    return r


def _build_report(*, findings, deps, repo_url, repo_type, owner, repo,
                  language, provider, model, ai_analyzed) -> VulnReport:
    counts = {s: 0 for s in SEVERITY_RANKS}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    def _split(cat: str) -> List[dict]:
        items = [f for f in findings if f.category == cat]
        items.sort(key=lambda f: (
            {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(f.severity, 4),
            -(f.ai_priority or 0),
        ))
        return [f.to_dict() for f in items]

    graph: GraphData = build_graph(findings, deps)

    return VulnReport(
        repo_url=repo_url, repo_type=repo_type, owner=owner, repo=repo,
        language=language, provider=provider, model=model,
        generated_at=datetime.now(timezone.utc).isoformat(),
        counts=counts,
        total_findings=len(findings),
        total_dependencies_scanned=len(deps),
        client_findings=_split("client"),
        server_findings=_split("server"),
        dependency_findings=_split("dependency"),
        all_findings=[f.to_dict() for f in findings],
        scanned_dependencies=[d.to_dict() for d in deps],
        graph=graph.to_dict(),
        ai_analyzed=ai_analyzed,
    )