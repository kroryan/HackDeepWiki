"""LLM prompts for vulnerability impact analysis.

The analyzer asks the model to produce a strict JSON array (one object per
CVE in the batch) so the result is machine-parseable. Every prompt is built
to degrade gracefully: if the model ignores the format, ``llm_analyzer``
falls back to deterministic defaults so the report is never broken.
"""

from __future__ import annotations

from typing import List

from api.vuln_scanner.models import CVEFinding

# Map wiki language code -> a human language name the model understands. Falls
# back to English for anything unmapped (matches the wiki's own behaviour).
_LANG_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "pt": "Portuguese", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
    "zh-CN": "Chinese (Simplified)", "ko": "Korean", "ru": "Russian",
    "ar": "Arabic", "hi": "Hindi", "nl": "Dutch", "pl": "Polish",
    "tr": "Turkish", "id": "Indonesian", "vi": "Vietnamese",
    "uk": "Ukrainian", "cs": "Czech", "sv": "Swedish",
}


def language_name(code: str) -> str:
    return _LANG_NAMES.get((code or "en").strip(), "English")


SYSTEM_PROMPT = (
    "You are a senior application security engineer. You analyse known CVEs "
    "in the context of a SPECIFIC codebase and judge how each vulnerability "
    "actually affects THAT application (not the generic advisory text). You "
    "are precise, concrete, and never invent facts. If you cannot tell "
    "whether a vulnerable code path is reachable, say so explicitly. You "
    "always answer as a single valid JSON array and nothing else."
)


def build_analysis_prompt(
    findings: List[CVEFinding],
    app_context: str,
    language: str,
) -> str:
    """Build the user prompt for one batch of findings."""
    lang = language_name(language)
    blocks: List[str] = []
    for f in findings:
        refs = "\n".join(f"  - {r}" for r in f.references[:4]) or "  (none)"
        usage = ", ".join(f.usage_files[:6]) or "(not directly imported in scanned source)"
        block = (
            f"### CVE {f.id}\n"
            f"- Package: {f.package_name}@{f.installed_version} "
            f"({f.package_ecosystem}, category={f.category})\n"
            f"- Severity: {f.severity}"
            + (f" (CVSS {f.cvss_score})" if f.cvss_score is not None else "") + "\n"
            + (f"- Fixed in: {f.fixed_version}\n" if f.fixed_version else "- Fixed in: (no fix published yet)\n")
            + (f"- CWE: {', '.join(f.cwe_ids)}\n" if f.cwe_ids else "")
            + f"- Summary: {f.summary or '(none)'}\n"
            f"- Files where this dependency is used: {usage}\n"
            f"- References:\n{refs}\n"
        )
        blocks.append(block)

    findings_block = "\n".join(blocks)
    ids = ", ".join(f.id for f in findings)

    return f"""You are auditing the codebase described below for the impact of {len(findings)} known vulnerability/vulnerabilities: {ids}.

## Application context
{app_context}

## Vulnerabilities to analyse
{findings_block}

## Task
For EACH CVE above, produce an analysis tailored to THIS application. Consider:
- Impact: how does this CVE concretely affect this app given the listed files where the dependency is used? Is the vulnerable function/feature likely reachable?
- Exploitability: an exhaustive, concrete description of how an attacker would actually abuse this CVE in THIS app's context (not the generic advisory summary). Does it require network access, user interaction, authentication, or a specific configuration? What does the attacker gain?
- Exploit vector: ONE short line naming the attack path and prerequisites (e.g. "Remote, unauthenticated" / "Requires an authenticated low-privilege session" / "Local access + ability to write a crafted file" / "Requires user interaction (phishing/social engineering)").
- Exploit plan: a concrete, NUMBERED, step-by-step sequence of what an attacker would actually do to exploit this CVE in THIS application end to end (recon -> trigger -> impact). Reference the actual usage files/context above where possible instead of generic advisory boilerplate.
- Remediation: concrete steps to fix. If a fixed version exists, name the exact upgrade target and the command. Add config/workaround if relevant.
- Priority: an integer 1-5 (5 = fix immediately) based on severity AND real-world exploitability in this app.

## Output format
Respond with ONLY a JSON array (no markdown fences, no prose). One object per CVE, in the same order, each with exactly these string fields and an integer priority:
[
  {{"id": "{findings[0].id if findings else ''}", "impact": "...", "exploitability": "...", "exploit_vector": "...", "exploit_plan": "1. ...\\n2. ...\\n3. ...", "remediation": "...", "priority": 4}}
]

Write the impact / exploitability / exploit_vector / exploit_plan / remediation text in {lang}. Keep impact/exploitability/remediation concise (2-5 sentences); exploit_vector is a single short line; exploit_plan is a numbered list of concrete steps.
"""


def build_stack_summary(deps) -> str:
    """Produce a short human-readable description of the detected tech stack
    to give the model application context."""
    ecosystems = sorted({d.ecosystem for d in deps})
    eco_names = {
        "npm": "JavaScript/TypeScript (npm)", "PyPI": "Python",
        "Go": "Go", "crates.io": "Rust", "Maven": "Java/JVM (Maven)",
        "RubyGems": "Ruby", "Packagist": "PHP", "NuGet": ".NET (C#)",
    }
    parts = [eco_names.get(e, e) for e in ecosystems]
    stack = ", ".join(parts) if parts else "unknown"

    # surface notable frameworks
    notable = []
    names = {d.name.lower() for d in deps}
    framework_signals = {
        "react": "React (frontend)", "vue": "Vue (frontend)",
        "angular": "Angular (frontend)", "svelte": "Svelte (frontend)",
        "next": "Next.js", "express": "Express (backend)", "koa": "Koa (backend)",
        "fastify": "Fastify (backend)", "nestjs": "NestJS (backend)",
        "django": "Django (backend)", "flask": "Flask (backend)",
        "fastapi": "FastAPI (backend)", "rails": "Ruby on Rails",
        "spring-boot": "Spring Boot (backend)", "gin": "Gin (backend)",
        "actix-web": "Actix web (backend)", "axum": "Axum (backend)",
        "laravel": "Laravel (backend)", "grpc": "gRPC",
    }
    for key, label in framework_signals.items():
        if any(key in n for n in names):
            notable.append(label)
    notable_str = ("Notable frameworks detected: " + ", ".join(notable) + ".") if notable else ""

    client = sum(1 for d in deps if d.category == "client")
    server = sum(1 for d in deps if d.category == "server")
    arch = []
    if client:
        arch.append("client-side code")
    if server:
        arch.append("server-side code")
    arch_str = f"Appears to have {' and '.join(arch)}." if arch else ""

    bits = [f"Primary ecosystems: {stack}."]
    if notable_str:
        bits.append(notable_str)
    if arch_str:
        bits.append(arch_str)
    bits.append(f"{len(deps)} dependencies were scanned.")
    return " ".join(bits)