"""LLM analysis of CVE findings in context of the scanned repo.

Enriches each ``CVEFinding`` with four AI fields (impact, exploitability,
remediation, priority) by asking the configured model to judge the CVE against
the actual files where the vulnerable dependency is used.

Robustness strategy (this must never break a report):
    * Deterministic defaults are written to every finding FIRST. The LLM only
      *overwrites* them -- so even total LLM failure leaves a coherent report.
    * Findings are analysed in small batches with strict-JSON prompts; parsing
      is lenient (fence stripping, trailing-comma repair, best-effort matching
      by CVE id).
    * Every LLM call is wrapped; a failure on one batch doesn't abort the rest.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Awaitable, Callable, List, Optional

from api.vuln_scanner.models import SEVERITY_ORDER, CVEFinding
from api.vuln_scanner.prompts import build_analysis_prompt

logger = logging.getLogger(__name__)

_DEFAULT_PRIORITY = {
    "CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "UNKNOWN": 1,
}

# Import the LLM facade lazily so a missing/misconfigured provider never breaks
# import of the subpackage (and so unit tests can stub it).


def _apply_defaults(findings: List[CVEFinding]) -> None:
    """Write deterministic ai_* defaults so the report is coherent even with
    no LLM."""
    for f in findings:
        if f.ai_remediation:
            continue  # don't clobber a pre-existing value
        if f.fixed_version:
            f.ai_remediation = (
                f"Upgrade {f.package_name} from {f.installed_version} to "
                f"{f.fixed_version}. After upgrading, re-test the affected "
                f"flows and check for transitive dependents that pin the old "
                f"version in lockfiles."
            )
        else:
            f.ai_remediation = (
                f"No fixed version has been published yet for {f.id}. Monitor "
                f"the advisory, apply vendor mitigations/workarounds if any, "
                f"and consider replacing or sandboxing {f.package_name} if the "
                f"risk is unacceptable."
            )
        if not f.ai_impact_analysis:
            base = (f"{f.package_name}@{f.installed_version} is affected by "
                    f"{f.id} (severity: {f.severity}")
            if f.cvss_score is not None:
                base += f", CVSS {f.cvss_score}"
            base += ")."
            if f.summary:
                base += f" {f.summary}"
            f.ai_impact_analysis = base
        if not f.ai_exploitability:
            f.ai_exploitability = (
                "Not individually assessed for this codebase. Refer to the "
                "CVSS vector and references to gauge exploitability."
            )
        if not f.ai_exploit_vector:
            f.ai_exploit_vector = (
                "Not individually assessed for this codebase — refer to the "
                "CVSS vector (AV/AC/PR/UI) in the advisory for the attack "
                "path and prerequisites."
            )
        if not f.ai_exploit_plan:
            f.ai_exploit_plan = (
                f"1. Confirm {f.package_name}@{f.installed_version} is reachable in a running "
                f"deployment of this app.\n"
                f"2. Read the advisory ({f.id}) and any public proof-of-concept/exploit-db entry "
                f"for the exact trigger conditions.\n"
                f"3. Reproduce the trigger against this app's usage of the package and confirm "
                f"impact before treating this as exploitable in practice."
            )
        if not f.ai_priority:
            f.ai_priority = _DEFAULT_PRIORITY.get(f.severity, 1)


def _extract_json_array(text: str):
    """Pull a JSON array out of a possibly-fenced / chatty model response."""
    if not text:
        return None
    t = text.strip()
    # strip ```json ... ``` fences
    fence = re.match(r"^```[a-zA-Z]*\s*\n", t)
    if fence:
        t = t[fence.end():]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    start = t.find("[")
    end = t.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = t[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # repair trailing commas
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(fixed)
    except Exception:
        return None


def _merge_llm_result(findings: List[CVEFinding], parsed) -> int:
    """Apply parsed JSON objects to matching findings by id. Returns how many
    were updated."""
    if not isinstance(parsed, list):
        return 0
    by_id = {}
    for obj in parsed:
        if isinstance(obj, dict) and obj.get("id"):
            by_id[str(obj["id"]).strip()] = obj
    updated = 0
    for f in findings:
        obj = by_id.get(f.id) or by_id.get(f.id.upper()) or by_id.get(f.id.lower())
        if not obj:
            # try aliases
            for alias in f.aliases:
                obj = by_id.get(alias) or by_id.get(alias.upper())
                if obj:
                    break
        if not obj:
            continue
        impact = obj.get("impact")
        exploit = obj.get("exploitability")
        vector = obj.get("exploit_vector")
        plan = obj.get("exploit_plan")
        rem = obj.get("remediation")
        prio = obj.get("priority")
        if isinstance(impact, str) and impact.strip():
            f.ai_impact_analysis = impact.strip()
            updated += 1
        if isinstance(exploit, str) and exploit.strip():
            f.ai_exploitability = exploit.strip()
        if isinstance(vector, str) and vector.strip():
            f.ai_exploit_vector = vector.strip()
        if isinstance(plan, str) and plan.strip():
            f.ai_exploit_plan = plan.strip()
        if isinstance(rem, str) and rem.strip():
            f.ai_remediation = rem.strip()
        if isinstance(prio, (int, float)) and 1 <= int(prio) <= 5:
            f.ai_priority = int(prio)
        elif isinstance(prio, str) and prio.isdigit() and 1 <= int(prio) <= 5:
            f.ai_priority = int(prio)
    return updated


def _read_usage_snippets(repo_dir: str, finding: CVEFinding,
                         max_files: int = 3, max_bytes: int = 4000) -> str:
    """Read short excerpts of the files that use the vulnerable dependency,
    to give the model concrete context."""
    snippets: List[str] = []
    for rel in (finding.usage_files or [])[:max_files]:
        full = os.path.join(repo_dir, rel)
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > 256 * 1024:
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                chunk = fh.read(max_bytes)
            snippets.append(f"--- {rel} (first {max_bytes} chars) ---\n{chunk}")
        except Exception:
            continue
    if not snippets:
        return "(no source snippets available)"
    return "\n\n".join(snippets)


async def analyze_findings(
    findings: List[CVEFinding],
    *,
    repo_dir: str,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    api_endpoint: Optional[str],
    language: str,
    app_context: Optional[str] = None,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    batch_size: int = 5,
) -> bool:
    """Enrich findings in place with AI analysis. Returns True if at least one
    LLM batch was attempted and succeeded in updating findings."""
    # Always apply deterministic defaults first.
    _apply_defaults(findings)

    if not findings:
        return False

    # Sort by severity (worst first) so the most important get analysed even
    # if the user/cost limits interrupt early.
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 0), reverse=True)

    # Lazily import the LLM facade + config so a misconfigured provider never
    # breaks the report (defaults already applied above).
    try:
        from api.agent_loop import stream_chat
        from api.config import get_model_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM modules unavailable, using defaults only: %s", exc)
        if on_progress:
            await on_progress("LLM unavailable — using deterministic analysis only.")
        return False

    try:
        model_config_kwargs = get_model_config(provider, model)["model_kwargs"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve model config for provider=%s model=%s: %s",
                       provider, model, exc)
        if on_progress:
            await on_progress("Model not configured — using deterministic analysis only.")
        return False

    if app_context is None:
        # Build a minimal context from the findings' packages.
        from api.vuln_scanner.dep_parser import Dependency  # noqa: F401 (type only)
        app_context = build_stack_summary_from_findings(ordered)

    batches = [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]
    any_success = False
    for bi, batch in enumerate(batches):
        if on_progress:
            await on_progress(
                f"Analysing CVEs batch {bi + 1}/{len(batches)} "
                f"({', '.join(f.id for f in batch)})"
            )
        # Attach usage snippets to the prompt's app_context for this batch.
        snippet_ctx = app_context + "\n\n## Relevant source snippets for this batch\n"
        for f in batch:
            snippet_ctx += f"\n### {f.id} — {f.package_name} used in:\n"
            snippet_ctx += _read_usage_snippets(repo_dir, f) + "\n"

        prompt = build_analysis_prompt(batch, snippet_ctx, language)
        try:
            collected: List[str] = []
            async for chunk in stream_chat(
                provider=provider,
                requested_model=model,
                prompt=prompt,
                model_config_kwargs=model_config_kwargs,
                api_key=api_key,
                api_endpoint=api_endpoint,
            ):
                if chunk:
                    collected.append(chunk)
            text = "".join(collected)
            parsed = _extract_json_array(text)
            n = _merge_llm_result(batch, parsed)
            if n > 0:
                any_success = True
            else:
                logger.debug("LLM batch %d produced no usable JSON; defaults kept.", bi + 1)
        except Exception as exc:  # noqa: BLE001 - one batch failing is fine
            logger.warning("LLM analysis batch %d failed (defaults kept): %s", bi + 1, exc)
            if on_progress:
                await on_progress(f"Batch {bi + 1} failed ({exc}); defaults kept.")

    return any_success


def build_stack_summary_from_findings(findings: List[CVEFinding]) -> str:
    """Lightweight stack summary when we only have findings (not the full dep
    list)."""
    ecosystems = sorted({f.package_ecosystem for f in findings})
    eco_names = {
        "npm": "JavaScript/TypeScript (npm)", "PyPI": "Python",
        "Go": "Go", "crates.io": "Rust", "Maven": "Java/JVM",
        "RubyGems": "Ruby", "Packagist": "PHP", "NuGet": ".NET",
    }
    parts = [eco_names.get(e, e) for e in ecosystems]
    cats = sorted({f.category for f in findings})
    arch = ", ".join(cats) if cats else "mixed"
    return (f"Application exposes dependencies in: {', '.join(parts) if parts else 'unknown'}. "
            f"Affected surfaces: {arch}. {len(findings)} vulnerabilities under analysis.")