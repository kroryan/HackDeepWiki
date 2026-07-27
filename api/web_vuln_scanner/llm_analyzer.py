"""LLM pass over the deterministic web findings.

Two responsibilities, mirroring the user's explicit request for the
dependency scanner: given the site's detected technologies + already-found
issues, the model can (a) propose additional CVE candidates the
OSV/fingerprint pass didn't surface (e.g. it recognizes a version string the
regex table doesn't cover) and (b) flag low-confidence findings as likely
false positives with a reason -- both are advisory and clearly marked
(ai_proposed / ai_dismissed), never silently mutating what the deterministic
checks found.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable, List, Optional

from api.web_vuln_scanner.models import WebFinding

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n", t)
    if fence:
        t = t[fence.end():]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = t[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        pass
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(fixed)
    except Exception:
        return None


def _build_prompt(technologies: List[str], existing: List[WebFinding], language: str) -> str:
    tech_list = "\n".join(f"- {t}" for t in technologies) or "(none detected)"
    findings_list = "\n".join(
        f"- [{f.severity}] {f.id}: {f.title} ({f.category})" for f in existing
    ) or "(none)"

    return f"""You are a security analyst reviewing an automated website scan's results.

Detected technologies/versions on this site:
{tech_list}

Findings already identified by deterministic checks (headers/cookies/TLS/exposed paths/OSV CVE lookups):
{findings_list}

Two tasks:
1. Propose additional CVEs that plausibly affect the detected technologies above but are NOT
   already in the findings list. Only propose CVEs you have reasonable confidence actually exist
   and apply to the stated technology/version. If genuinely unsure, do not propose it.
2. Review the existing findings list for any that are likely FALSE POSITIVES given the context
   (e.g. a generic exposed-path hit that's actually a custom 404 page, or a CVE that doesn't
   apply to this exact deployment). List their ids with a short reason.

Respond in {language} for all human-readable text (titles, descriptions, reasons).

Return ONLY a single JSON object, no markdown fences, no prose outside the JSON:
{{
  "proposed_cves": [
    {{"cve_id": "CVE-YYYY-NNNNN", "technology": "name@version", "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "title": "short title", "description": "why this applies", "confidence": "high|medium|low"}}
  ],
  "likely_false_positives": [
    {{"id": "existing-finding-id", "reason": "short reason"}}
  ]
}}
If you have nothing to add for either list, return an empty array for it."""


async def analyze_web_findings(
    findings: List[WebFinding],
    technologies: List[str],
    *,
    provider: str,
    model: Optional[str],
    api_key: Optional[str],
    api_endpoint: Optional[str],
    language: str,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> List[WebFinding]:
    """Returns the list of NEW WebFinding objects to append (AI-proposed
    CVEs). Mutates ``findings`` in place to mark ai_dismissed on any the
    model flagged as likely false positives -- never removes them, just
    annotates, so the user can judge for themselves."""
    try:
        from api.agent_loop import stream_chat
        from api.config import get_model_config
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM modules unavailable for web scan analysis: %s", exc)
        return []

    try:
        model_config_kwargs = get_model_config(provider, model)["model_kwargs"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resolve model config: %s", exc)
        return []

    if on_progress:
        await on_progress("Asking AI to cross-check for missed/false-positive CVEs…")

    prompt = _build_prompt(technologies, findings, language)
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
        parsed = _extract_json_object(text)
    except Exception as exc:  # noqa: BLE001 - AI cross-check is optional, never fatal
        logger.warning("Web vuln AI cross-check failed: %s", exc)
        return []

    if not isinstance(parsed, dict):
        return []

    new_findings: List[WebFinding] = []
    existing_ids = {f.id for f in findings}
    for item in (parsed.get("proposed_cves") or []):
        if not isinstance(item, dict):
            continue
        cve_id = str(item.get("cve_id") or "").strip()
        if not cve_id:
            continue
        slug = f"ai-proposed-{cve_id.lower()}"
        if slug in existing_ids:
            continue
        severity = str(item.get("severity") or "MEDIUM").upper()
        if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            severity = "MEDIUM"
        new_findings.append(WebFinding(
            id=slug,
            category="cve",
            severity=severity,
            title=str(item.get("title") or cve_id).strip(),
            description=str(item.get("description") or "").strip(),
            cve_id=cve_id,
            technology=str(item.get("technology") or "").strip() or None,
            ai_proposed=True,
            ai_notes=f"AI-proposed (confidence: {item.get('confidence', 'unknown')}); verify manually before acting.",
            references=[f"https://osv.dev/vulnerability/{cve_id}", f"https://nvd.nist.gov/vuln/detail/{cve_id}"],
        ))
        existing_ids.add(slug)

    dismiss_map = {}
    for item in (parsed.get("likely_false_positives") or []):
        if isinstance(item, dict) and item.get("id"):
            dismiss_map[str(item["id"]).strip()] = str(item.get("reason") or "").strip()

    for f in findings:
        if f.id in dismiss_map:
            f.ai_dismissed = True
            f.ai_dismiss_reason = dismiss_map[f.id]

    return new_findings
