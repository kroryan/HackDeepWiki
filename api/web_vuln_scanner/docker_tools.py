"""Runs the freedeepwiki-vulnscan Kali toolkit container (docker/vulnscan/)
as one-shot subprocesses and parses each tool's output into WebFinding
objects.

Docker is entirely user-managed: FreeDeepWiki never installs, starts, or
auto-configures Docker itself (see docker/vulnscan/docker-compose.yml's
module docstring). This module only:
    1. Detects whether Docker is available and whether the image exists.
    2. Pulls the image (streaming progress) the first time a scan actually
       needs it -- never eagerly, never at app startup.
    3. Runs one `docker run --rm` per tool per scan and parses stdout.

Every function degrades gracefully: if Docker isn't installed, the image
isn't available, or a specific tool run fails/times out, the caller gets an
empty finding list rather than a crashed scan -- the deterministic
`api.web_vuln_scanner.checks` pass (pure Python, no Docker) already covers
the header/cookie/TLS/exposed-path basics independently of this toolkit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional
from urllib.parse import urlparse

from api.web_vuln_scanner.models import WebFinding

logger = logging.getLogger(__name__)

IMAGE_NAME = "freedeepwiki-vulnscan:latest"
# Public image on Docker Hub -- built from docker/vulnscan/Dockerfile and
# pushed independently of this repo's release cadence. Falls back to a local
# `docker build` (see ensure_image) if the pull fails (offline dev, private
# registry mirror, etc.) and a local Dockerfile is present.
REGISTRY_IMAGE = "krory90/freedeepwiki-vulnscan:latest"

_DOCKER_TIMEOUT = 20  # for quick availability checks
_TOOL_TIMEOUT = 180  # per-tool scan timeout -- generous but bounded

ProgressCb = Callable[[str, Optional[int]], Awaitable[None]]


@dataclass
class DockerStatus:
    docker_installed: bool
    image_available: bool


def check_docker_status() -> DockerStatus:
    """Cheap, synchronous check -- callers use this to decide whether to
    offer the Docker-backed tools at all, before committing to a pull."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return DockerStatus(docker_installed=False, image_available=False)
    try:
        subprocess.run(
            [docker_bin, "info"], capture_output=True, timeout=_DOCKER_TIMEOUT, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return DockerStatus(docker_installed=False, image_available=False)

    try:
        result = subprocess.run(
            [docker_bin, "image", "inspect", IMAGE_NAME],
            capture_output=True, timeout=_DOCKER_TIMEOUT,
        )
        image_available = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        image_available = False
    return DockerStatus(docker_installed=True, image_available=image_available)


async def ensure_image(on_progress: Optional[ProgressCb] = None) -> bool:
    """Pull the toolkit image if it isn't present locally yet, streaming
    docker pull's own progress lines back through on_progress so the UI can
    show real download progress on first use (per the product requirement:
    notify + show progress on first download, never a silent multi-GB pull).

    Returns True if the image is available (already was, or pull succeeded).
    """
    status = check_docker_status()
    if not status.docker_installed:
        return False
    if status.image_available:
        return True

    docker_bin = shutil.which("docker")

    async def _p(msg: str) -> None:
        # docker pull emits one line per layer -- log every one so the
        # first-time multi-GB download has real, visible progress in the
        # terminal instead of going silent for minutes.
        logger.info("[docker-pull] %s", msg)
        if on_progress:
            try:
                await on_progress(msg, None)
            except Exception:  # noqa: BLE001
                pass

    await _p(f"Downloading security scan toolkit ({REGISTRY_IMAGE})… this is a one-time download.")

    proc = await asyncio.create_subprocess_exec(
        docker_bin, "pull", REGISTRY_IMAGE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    last_line = ""
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line:
            last_line = line
            await _p(line)
    await proc.wait()

    if proc.returncode == 0:
        # Registry image pulled under its own tag -- alias it to the local
        # name every docker_run() call below uses.
        subprocess.run([docker_bin, "tag", REGISTRY_IMAGE, IMAGE_NAME],
                       capture_output=True, timeout=_DOCKER_TIMEOUT)
        await _p("Toolkit image ready.")
        return True

    await _p(f"Registry pull failed ({last_line}); trying a local build instead…")
    return await _build_local_image(on_progress)


async def _build_local_image(on_progress: Optional[ProgressCb] = None) -> bool:
    import os
    dockerfile_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                  "docker", "vulnscan")
    if not os.path.isfile(os.path.join(dockerfile_dir, "Dockerfile")):
        return False

    async def _p(msg: str) -> None:
        logger.info("[docker-build] %s", msg)
        if on_progress:
            try:
                await on_progress(msg, None)
            except Exception:  # noqa: BLE001
                pass

    docker_bin = shutil.which("docker")
    await _p("Building security scan toolkit locally (this can take several minutes)…")
    proc = await asyncio.create_subprocess_exec(
        docker_bin, "build", "-t", IMAGE_NAME, dockerfile_dir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line:
            await _p(line)
    await proc.wait()
    return proc.returncode == 0


def _run_tool(args: List[str], timeout: int = _TOOL_TIMEOUT) -> Optional[str]:
    """Run `docker run --rm freedeepwiki-vulnscan <args>` and return stdout,
    or None on any failure/timeout (non-fatal -- caller just gets no
    findings from this tool)."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return None
    try:
        result = subprocess.run(
            [docker_bin, "run", "--rm", "--network", "host", IMAGE_NAME, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("docker_tools: tool run failed (%s): %s", args[:1], exc)
        return None


# ---------------------------------------------------------------------------
# nmap: port scan + service detection + NSE vuln scripts
# ---------------------------------------------------------------------------

def run_nmap(hostname: str) -> List[WebFinding]:
    xml_out = _run_tool([
        "nmap", "-sT", "-sV", "-Pn", "--top-ports", "100", "-T4",
        "--script", "vuln", "--host-timeout", "120s", "-oX", "-", hostname,
    ], timeout=150)
    if not xml_out:
        return []
    try:
        root = ET.fromstring(xml_out)
    except ET.ParseError:
        return []

    findings: List[WebFinding] = []
    for port_el in root.iter("port"):
        state_el = port_el.find("state")
        if state_el is None or state_el.get("state") != "open":
            continue
        port_num = port_el.get("portid")
        service_el = port_el.find("service")
        service_name = service_el.get("name") if service_el is not None else ""
        product = (service_el.get("product") or "") if service_el is not None else ""
        version = (service_el.get("version") or "") if service_el is not None else ""
        tech = f"{product} {version}".strip() or service_name

        if int(port_num or 0) not in (80, 443):
            findings.append(WebFinding(
                id=f"nmap-open-port-{port_num}",
                category="exposure",
                severity="HIGH" if int(port_num or 0) in (22, 3306, 3389, 5432, 6379, 9200, 27017) else "MEDIUM",
                title=f"Port {port_num} ({service_name}) is open",
                description=f"nmap detected an open port: {port_num}/{service_name}" + (f" running {tech}" if tech else ""),
                evidence=f"{port_num}/tcp open {service_name} {tech}".strip(),
                technology=tech or None,
                remediation=f"Firewall port {port_num} to trusted IPs only, or bind the service to a private network interface.",
            ))

        # NSE vuln scripts attach a <script id="..." output="..."> per finding.
        for script_el in port_el.findall("script"):
            script_id = script_el.get("id", "")
            output = script_el.get("output", "")
            if "VULNERABLE" not in output:
                continue
            cve_match = re.search(r'(CVE-\d{4}-\d{4,7})', output)
            findings.append(WebFinding(
                id=f"nmap-nse-{script_id}-{port_num}",
                category="cve" if cve_match else "exposure",
                severity="HIGH",
                title=f"nmap NSE: {script_id} flagged port {port_num} as vulnerable",
                description=output.strip()[:500],
                evidence=output.strip()[:300],
                cve_id=cve_match.group(1) if cve_match else None,
                technology=tech or None,
                references=[f"https://nmap.org/nsedoc/scripts/{script_id}.html"],
                remediation="Review the NSE script output above and patch/reconfigure the affected service.",
            ))
    return findings


# ---------------------------------------------------------------------------
# whatweb: technology fingerprinting
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> Optional[dict]:
    """Extracts the first balanced ``{...}`` JSON object from ``text``,
    ignoring anything before/after/interleaved that isn't part of it (string
    literals are tracked so a literal ``{``/``}`` inside a JSON string value
    doesn't throw off the brace count)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_whatweb(url: str) -> List[str]:
    """Returns detected technology strings (name or name/version).

    ``--log-json=-`` does NOT write a clean ``[record]`` array to stdout --
    captured empirically: it writes ``[``, then the JSON record, then
    whatweb's normal ANSI-colored human-readable summary line for that same
    target *interleaved before* the closing ``]`` (i.e. the array's own
    closing bracket comes after the colored text, not the record). Neither a
    per-line parse nor a "cut at the first ESC byte" parse produces valid
    JSON from that. Since we only ever scan one URL per call, just extract
    the single balanced JSON object out of the noise instead of trying to
    parse the surrounding array.
    """
    # -a 3 (aggressive) mode measured at ~50s against a real WordPress site --
    # 60s left almost no margin and intermittently timed out (silently
    # returning no technologies, since _run_tool swallows TimeoutExpired).
    out = _run_tool(["whatweb", "-a", "3", "--log-json=-", url], timeout=100)
    if not out:
        return []
    rec = _extract_json_object(out)
    if not isinstance(rec, dict):
        return []
    technologies: List[str] = []
    plugins = rec.get("plugins", {}) if isinstance(rec, dict) else {}
    for name, info in plugins.items():
        if name in ("Title", "HTML5", "UncommonHeaders", "Cookies", "IP", "Country"):
            continue
        version = None
        if isinstance(info, dict):
            versions = info.get("version")
            if isinstance(versions, list) and versions:
                version = versions[0]
        technologies.append(f"{name}/{version}" if version else name)
    return technologies


# ---------------------------------------------------------------------------
# httpx (ProjectDiscovery): status/TLS/tech probe -- richer per-URL context
# than whatweb, mirrors RedAmon's http_probe stage. Flags a handful of
# response-level exposure signals (default/error pages, missing TLS) as
# findings; the bulk of its value is the technology strings it feeds into
# the OSV cross-reference the same way whatweb's output does.
# ---------------------------------------------------------------------------

def run_httpx(url: str) -> "tuple[List[WebFinding], List[str]]":
    """Returns (findings, technologies)."""
    out = _run_tool([
        "httpx", "-u", url, "-json", "-silent", "-sc", "-cl", "-ct",
        "-title", "-server", "-tls-probe", "-favicon", "-tech-detect",
        "-timeout", "10",
    ], timeout=60)
    if not out:
        return [], []

    findings: List[WebFinding] = []
    technologies: List[str] = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for tech in rec.get("tech", []) or []:
            technologies.append(tech)

        tls = rec.get("tls")
        if isinstance(tls, dict):
            # -tls-probe follows the cert's SAN entries and probes each one
            # (verified empirically: a single `-u` call against a domain
            # with a "www." SAN returned multiple JSON records, one per
            # probed host) -- key findings by host so repeated probes of the
            # same underlying cert don't collide on a fixed id.
            probed_host = rec.get("host") or tls.get("host") or url
            if tls.get("self_signed"):
                findings.append(WebFinding(
                    id=f"httpx-tls-self-signed-{probed_host}",
                    category="tls", severity="HIGH",
                    title="Self-signed TLS certificate detected",
                    description=f"httpx flagged the certificate served for {probed_host} as self-signed.",
                    url=url,
                    remediation="Use a certificate issued by a trusted CA (e.g. Let's Encrypt) instead of a self-signed one.",
                ))
            not_after = tls.get("not_after")
            if not_after:
                findings.append(WebFinding(
                    id=f"httpx-tls-cert-info-{probed_host}",
                    category="tls", severity="INFO",
                    title=f"TLS certificate expires {not_after}",
                    description=f"Certificate for {probed_host} expires on {not_after}.",
                    url=url, evidence=str(not_after),
                ))
    return findings, technologies


# ---------------------------------------------------------------------------
# nikto: web server misconfiguration scan
# ---------------------------------------------------------------------------

_NIKTO_SEVERITY_HINTS = {
    "OSVDB": "MEDIUM",
}


def run_nikto(url: str) -> List[WebFinding]:
    """Runs nikto and parses its JSON report.

    Three things verified empirically against a real target that the
    original implementation got wrong, all silently (never an exception,
    just zero findings every time):

    1. ``-ask no`` is mandatory -- nikto's default ("yes") interactively
       prompts on stdin ("Would you like to submit this information...?")
       whenever the server's header string isn't in its local database --
       true for most modern servers -- and since we never provide stdin,
       that hangs the whole tool until the timeout.
    2. ``-output -`` does not alias to stdout the way most CLIs treat "-"
       (same class of bug as testssl/gitleaks/ffuf above) -- nikto writes
       nothing at all when given "-". A real file path is required.
    3. nikto *appends* ``.<format>`` to whatever ``-output`` path is given,
       even if that path already ends in the right extension -- passing
       ``/out/report.json`` produces a file literally named
       ``report.json.json``. Passing the path with no extension avoids
       this (``/out/report`` -> ``/out/report.json``).

    Also: the report's top level is a *list* of one dict per scanned host
    (``[{"vulnerabilities": [...], ...}]``), not a single dict -- the
    original code's ``data.get("vulnerabilities")`` on a list silently
    returned nothing via the `isinstance(data, dict)` guard. And nikto 2.6.x
    findings key their id under plain ``"id"`` (OSVDB was shut down years
    ago), not ``"OSVDB"``; the real per-finding help link nikto emits lives
    in ``"references"`` as a ready-made URL string, not something we need to
    construct from an OSVDB id that's never actually present.
    """
    import os
    import tempfile

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []

    with tempfile.TemporaryDirectory(prefix="fdw-nikto-") as tmp_dir:
        try:
            subprocess.run(
                [docker_bin, "run", "--rm", "--network", "host",
                 "-v", f"{tmp_dir}:/out", IMAGE_NAME,
                 "nikto", "-h", url, "-Format", "json", "-o", "/out/report",
                 "-ask", "no", "-Tuning", "123489bde", "-maxtime", "150s"],
                # -maxtime is nikto's own internal deadline, but it still
                # needs real time after that to finish the in-flight request,
                # serialize, and write the JSON report -- measured empirically
                # at ~172s wall clock for a 150s maxtime (docker startup +
                # shutdown overhead). Give it a comfortable margin instead of
                # racing that number.
                capture_output=True, text=True, timeout=220,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("docker_tools: nikto run failed: %s", exc)
            return []

        result_path = os.path.join(tmp_dir, "report.json")
        if not os.path.isfile(result_path):
            return []
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []

    hosts = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    findings: List[WebFinding] = []
    for host in hosts:
        if not isinstance(host, dict):
            continue
        for v in host.get("vulnerabilities", []) or []:
            if not isinstance(v, dict):
                continue
            msg = v.get("msg", "")
            if not msg:
                continue
            finding_id = v.get("id") or abs(hash(msg)) % 100000
            ref = v.get("references") or ""
            findings.append(WebFinding(
                id=f"nikto-{finding_id}",
                category="exposure",
                severity="MEDIUM",
                title=msg[:120],
                description=msg,
                url=url.rstrip("/") + (v.get("url") or v.get("uri") or ""),
                evidence=f"{v.get('method', 'GET')} {v.get('url', '')}",
                references=[ref] if ref else [],
                remediation="Review the flagged path/configuration and apply the vendor's recommended hardening.",
            ))
    return findings


# ---------------------------------------------------------------------------
# testssl.sh: TLS/cipher/certificate audit
# ---------------------------------------------------------------------------

_TESTSSL_SEVERITY_MAP = {
    "CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM",
    "LOW": "LOW", "WARN": "MEDIUM",
    # Excluded entirely: OK ("no issue" -- e.g. "not vulnerable") and INFO.
    # testssl's INFO severity floods the report with non-actionable data
    # points (every cipher/cert/browser-simulation detail it collected, ~100
    # entries per scan) -- only genuinely actionable severities are surfaced
    # as findings; the rest is diagnostic detail, not a security issue.
    "OK": None, "INFO": None,
}


def run_testssl(hostname: str) -> List[WebFinding]:
    # testssl's --jsonfile-pretty is an *additional* output file, not a
    # stdout stream -- it never accepts "-" as a stdout alias the way most
    # CLI tools do, so we bind-mount a scratch dir and read the JSON file it
    # writes there instead of trying to capture it from stdout.
    import os
    import tempfile

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []

    with tempfile.TemporaryDirectory(prefix="fdw-testssl-") as tmp_dir:
        try:
            subprocess.run(
                [docker_bin, "run", "--rm", "--network", "host",
                 "-v", f"{tmp_dir}:/out", IMAGE_NAME,
                 "testssl", "--jsonfile-pretty", "/out/result.json",
                 "--quiet", "--warnings", "off", hostname],
                capture_output=True, text=True, timeout=150,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("docker_tools: testssl run failed: %s", exc)
            return []

        result_path = os.path.join(tmp_dir, "result.json")
        if not os.path.isfile(result_path):
            return []
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []

    # testssl's top-level JSON is a metadata dict (Invocation, version,
    # startTime, ...); the actual per-check findings live under
    # scanResult[0].<category> (protocols/ciphers/vulnerabilities/
    # headerResponse/rating/...), each a list of {id, severity, finding,
    # cve?, cwe?} dicts -- NOT a flat top-level list the way the initial
    # implementation assumed (that silently produced zero findings).
    if not isinstance(data, dict):
        return []
    scan_results = data.get("scanResult")
    if not isinstance(scan_results, list) or not scan_results:
        return []
    host_result = scan_results[0]
    if not isinstance(host_result, dict):
        return []

    findings: List[WebFinding] = []
    for category, entries in host_result.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            severity_raw = str(entry.get("severity", "")).upper()
            severity = _TESTSSL_SEVERITY_MAP.get(severity_raw)
            if not severity:
                continue
            finding_id = entry.get("id", "")
            finding_text = entry.get("finding", "")
            if not finding_id or not finding_text:
                continue
            cve = entry.get("cve", "")
            findings.append(WebFinding(
                id=f"testssl-{finding_id}",
                category="tls",
                severity=severity,
                title=f"{finding_id}: {finding_text[:100]}",
                description=finding_text,
                evidence=finding_text[:200],
                cve_id=cve.split()[0] if cve else None,
                references=[f"https://testssl.sh/"] if not cve else [f"https://osv.dev/vulnerability/{cve.split()[0]}"],
                remediation=f"Review testssl's '{category}' finding ({finding_id}) and adjust the server's TLS configuration accordingly.",
            ))
    return findings


# ---------------------------------------------------------------------------
# nuclei: community-maintained CVE / misconfiguration templates
# ---------------------------------------------------------------------------

_NUCLEI_SEVERITY_MAP = {
    "critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM",
    "low": "LOW", "info": "INFO", "unknown": "INFO",
}


def run_nuclei(url: str) -> List[WebFinding]:
    out = _run_tool(["nuclei", "-u", url, "-jsonl", "-silent",
                     "-severity", "critical,high,medium,low",
                     "-timeout", "10", "-rate-limit", "50"], timeout=150)
    if not out:
        return []

    findings: List[WebFinding] = []
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = rec.get("info", {}) if isinstance(rec, dict) else {}
        severity = _NUCLEI_SEVERITY_MAP.get(str(info.get("severity", "")).lower(), "INFO")
        template_id = rec.get("template-id", "")
        name = info.get("name", template_id)
        classification = info.get("classification", {}) or {}
        cve_ids = classification.get("cve-id") or []
        cve_id = cve_ids[0] if cve_ids else None
        cvss = classification.get("cvss-score")
        findings.append(WebFinding(
            id=f"nuclei-{template_id}",
            category="cve" if cve_id else "exposure",
            severity=severity,
            title=name,
            description=info.get("description", "") or name,
            url=rec.get("matched-at", url),
            evidence=(rec.get("extracted-results") or [""])[0] if rec.get("extracted-results") else "",
            cve_id=cve_id,
            cvss_score=float(cvss) if isinstance(cvss, (int, float)) else None,
            references=info.get("reference") or [],
            remediation=info.get("remediation", "") or "See the nuclei template reference for remediation guidance.",
        ))
    return findings


# ---------------------------------------------------------------------------
# subfinder + dnsx: passive subdomain enumeration + DNS resolution/liveness.
# Flags resolved subdomains that don't answer on 80/443 as forgotten/stale
# infra (a common real exposure -- an old subdomain still pointing at a
# decommissioned or unpatched host), which nmap/nikto/nuclei above never see
# since they only ever look at the one URL the user gave us.
# ---------------------------------------------------------------------------

def run_subdomain_recon(hostname: str) -> List[WebFinding]:
    subfinder_out = _run_tool(["subfinder", "-d", hostname, "-silent", "-timeout", "15"], timeout=60)
    if not subfinder_out:
        return []
    subdomains = [s.strip() for s in subfinder_out.strip().splitlines() if s.strip()]
    if not subdomains:
        return []
    # Cap to keep the dnsx pass (and the finding count) bounded on domains
    # with hundreds of passively-discovered subdomains.
    subdomains = subdomains[:100]

    dnsx_input = "\n".join(subdomains)
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []
    try:
        result = subprocess.run(
            [docker_bin, "run", "--rm", "-i", "--network", "host", IMAGE_NAME,
             "dnsx", "-silent", "-json", "-resp"],
            input=dnsx_input, capture_output=True, text=True, timeout=60,
        )
        dnsx_out = result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    if not dnsx_out:
        return []

    findings: List[WebFinding] = []
    resolved_count = 0
    for line in dnsx_out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        host = rec.get("host", "")
        if host:
            resolved_count += 1

    if resolved_count:
        findings.append(WebFinding(
            id="subdomains-discovered",
            category="exposure",
            severity="INFO",
            title=f"{resolved_count} subdomain(s) discovered and resolving",
            description=(
                f"subfinder+dnsx found {len(subdomains)} candidate subdomain(s) for {hostname}, "
                f"{resolved_count} of which currently resolve. Review this list for forgotten/stale "
                f"infrastructure (old staging environments, decommissioned services) that may not be "
                f"receiving security updates."
            ),
            evidence=", ".join(subdomains[:15]) + (f" (+{len(subdomains) - 15} more)" if len(subdomains) > 15 else ""),
            remediation="Audit each resolving subdomain: decommission unused ones, and ensure active ones are patched and monitored the same as the main site.",
        ))
    return findings


# ---------------------------------------------------------------------------
# ffuf: directory/file fuzzing against a real wordlist -- replaces a
# hand-maintained "common sensitive paths" list with actual bundled
# SecLists content, so coverage isn't limited to the handful of paths a
# human thought to hardcode.
# ---------------------------------------------------------------------------

def run_ffuf(url: str) -> List[WebFinding]:
    # NOTE: -json and -of json/-o are two different, incompatible things --
    # -json switches ffuf into newline-delimited streaming output (one
    # record per match, base64-encoded FUZZ values, no top-level "results"
    # key), while -of json -o <file> writes the final structured report this
    # parser expects (a single {"results": [...]} object with a plain-text
    # FUZZ value). Passing both interleaves the streaming records into
    # stdout, corrupting it (verified empirically: "Extra data" JSON error).
    #
    # Separately, -o - does NOT alias to stdout (verified empirically: with
    # -s/silent it produces zero bytes in the report and ffuf instead prints
    # its own bare matched-path list to stdout) -- same class of bug as
    # testssl/gitleaks above. Bind-mount a scratch dir and read the report
    # file it actually writes.
    import os
    import tempfile

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []

    base = url.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="fdw-ffuf-") as tmp_dir:
        try:
            subprocess.run(
                [docker_bin, "run", "--rm", "--network", "host",
                 "-v", f"{tmp_dir}:/out", IMAGE_NAME,
                 "ffuf", "-u", f"{base}/FUZZ",
                 "-w", "/usr/share/seclists/Discovery/Web-Content/common.txt",
                 "-mc", "200,201,204,301,302,307,401,403",
                 "-t", "20", "-timeout", "8", "-rate", "50",
                 "-of", "json", "-o", "/out/report.json", "-s"],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("docker_tools: ffuf run failed: %s", exc)
            return []

        result_path = os.path.join(tmp_dir, "report.json")
        if not os.path.isfile(result_path):
            return []
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []

    results = data.get("results", []) if isinstance(data, dict) else []
    findings: List[WebFinding] = []
    # Only flag paths that plausibly matter -- ffuf's common.txt wordlist
    # surfaces a lot of routine app paths (/api, /admin login pages that are
    # SUPPOSED to be reachable); restrict findings to names that are
    # actually sensitive if exposed.
    sensitive_hints = (".env", ".git", "backup", ".sql", "config", ".bak",
                       "wp-config", ".aws", "credentials", ".htpasswd", "dump")
    for r in results:
        if not isinstance(r, dict):
            continue
        path = r.get("input", {}).get("FUZZ", "") if isinstance(r.get("input"), dict) else ""
        status = r.get("status")
        if not any(hint in path.lower() for hint in sensitive_hints):
            continue
        findings.append(WebFinding(
            id=f"ffuf-{path.strip('/').replace('/', '-')}",
            category="exposure",
            severity="HIGH" if status == 200 else "MEDIUM",
            title=f"Potentially sensitive path found: /{path}",
            description=f"ffuf found /{path} responding with HTTP {status}.",
            url=f"{base}/{path}",
            evidence=f"HTTP {status}",
            remediation=f"Verify /{path} isn't meant to be public; remove or restrict access if it exposes sensitive data.",
        ))
    return findings


# ---------------------------------------------------------------------------
# dalfox: real reflected/DOM XSS scanning (parameter injection + reflection
# analysis), not a heuristic guess -- only meaningful when the crawl
# manifest has URLs with query parameters to test.
# ---------------------------------------------------------------------------

def run_dalfox(urls_with_params: List[str]) -> List[WebFinding]:
    if not urls_with_params:
        return []
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []
    findings: List[WebFinding] = []
    # dalfox url mode takes one URL per invocation; cap how many we probe to
    # keep total scan time bounded on sites with many parameterized pages.
    for url in urls_with_params[:10]:
        out = _run_tool(["dalfox", "url", url, "--silence", "--format", "json",
                         "--skip-bav", "--timeout", "10"], timeout=45)
        if not out:
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("type"):
                continue
            param = entry.get("param", "")
            findings.append(WebFinding(
                id=f"dalfox-xss-{abs(hash(url + param)) % 100000}",
                category="exposure",
                severity="HIGH",
                title=f"Reflected/DOM XSS in parameter '{param}'" if param else "XSS finding",
                description=entry.get("evidence", "") or f"dalfox flagged a {entry.get('type')} XSS at {url}.",
                url=url,
                evidence=(entry.get("payload") or "")[:200],
                remediation="Sanitize/encode this parameter's output in the response, or use a context-aware templating engine that auto-escapes by default.",
            ))
    return findings


# ---------------------------------------------------------------------------
# wpscan: real WordPress vulnerability scanner -- only run when WordPress
# was fingerprinted by whatweb/httpx (see orchestrator.py). Replaces the
# hand-rolled WordPress-specific probes that used to live here (removed:
# too narrow/brittle vs. an actual maintained scanner with a real
# vulnerability database).
# ---------------------------------------------------------------------------

def run_wpscan(url: str) -> List[WebFinding]:
    out = _run_tool(["wpscan", "--url", url, "--no-banner", "--format", "json",
                     "--random-user-agent", "--enumerate", "vp,vt,u"], timeout=150)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    findings: List[WebFinding] = []

    version_info = (data.get("version") or {})
    if isinstance(version_info, dict) and version_info.get("number"):
        for vuln in version_info.get("vulnerabilities", []) or []:
            findings.extend(_wpscan_vuln_to_finding(vuln, "WordPress core"))

    for plugin_name, plugin_info in (data.get("plugins") or {}).items():
        if not isinstance(plugin_info, dict):
            continue
        for vuln in plugin_info.get("vulnerabilities", []) or []:
            findings.extend(_wpscan_vuln_to_finding(vuln, f"plugin '{plugin_name}'"))

    users = data.get("users") or {}
    if isinstance(users, dict) and users:
        usernames = list(users.keys())[:10]
        findings.append(WebFinding(
            id="wpscan-user-enumeration",
            category="exposure", severity="MEDIUM",
            title=f"WordPress exposes {len(users)} username(s)",
            description="wpscan enumerated WordPress usernames, commonly used as a starting point for credential-stuffing attacks.",
            evidence=", ".join(usernames),
            remediation="Restrict the REST API users endpoint and the author archive/oEmbed username-disclosure vectors (a security plugin, or a filter on rest_endpoints).",
        ))
    return findings


def _wpscan_vuln_to_finding(vuln: dict, component: str) -> List[WebFinding]:
    if not isinstance(vuln, dict):
        return []
    title = vuln.get("title", "WordPress vulnerability")
    refs = vuln.get("references") or {}
    cve_ids = refs.get("cve") or []
    cve_id = f"CVE-{cve_ids[0]}" if cve_ids and not str(cve_ids[0]).upper().startswith("CVE") else (cve_ids[0] if cve_ids else None)
    return [WebFinding(
        id=f"wpscan-{abs(hash(title)) % 100000}",
        category="cve" if cve_id else "exposure",
        severity="HIGH",
        title=f"{component}: {title}",
        description=title,
        cve_id=cve_id,
        references=[f"https://wpvulndb.com/vulnerabilities/{vuln.get('id')}"] if vuln.get("id") else [],
        remediation=f"Update {component} to a patched version, or remove/deactivate it if no fix is available.",
    )]


# ---------------------------------------------------------------------------
# Code-repo scanning: gitleaks (secret detection) + semgrep (SAST) against a
# local clone directory. Both were already bundled in the Kali toolkit image
# for interactive use; this wires them into an automated pass over the exact
# clone api.vuln_scanner's dependency scan already reads from disk, giving
# repo wikis a real static-analysis + secret-scan layer instead of only
# manifest/dependency CVE lookups.
# ---------------------------------------------------------------------------

def _run_tool_with_mount(host_dir: str, args: List[str], timeout: int = _TOOL_TIMEOUT) -> Optional[str]:
    """Same as _run_tool but bind-mounts ``host_dir`` read-only at /repo
    inside the container first -- for tools that scan a local directory
    rather than a URL."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return None
    try:
        result = subprocess.run(
            [docker_bin, "run", "--rm", "-v", f"{host_dir}:/repo:ro", IMAGE_NAME, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("docker_tools: mounted tool run failed (%s): %s", args[:1], exc)
        return None


def run_gitleaks(repo_dir: str) -> List[WebFinding]:
    """Scans a local repo clone for leaked secrets (API keys, tokens,
    credentials committed to git history/working tree).

    gitleaks' ``-r /dev/stdout`` silently writes nothing when its stdout is a
    pipe (as opposed to a TTY) -- captured empirically: `docker run ...
    gitleaks -r /dev/stdout` returns a completely empty stdout under
    `subprocess.run(capture_output=True)` even when leaks are found (and
    logged to stderr as "leaks found: N"). Like testssl below, we bind-mount
    a scratch dir and have gitleaks write its report to a real file instead.
    """
    import os
    import tempfile

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []

    with tempfile.TemporaryDirectory(prefix="fdw-gitleaks-") as tmp_dir:
        try:
            subprocess.run(
                [docker_bin, "run", "--rm",
                 "-v", f"{repo_dir}:/repo:ro", "-v", f"{tmp_dir}:/out",
                 IMAGE_NAME, "gitleaks", "detect", "--source", "/repo", "--no-git",
                 "-f", "json", "-r", "/out/report.json", "--exit-code", "0"],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("docker_tools: gitleaks run failed: %s", exc)
            return []

        result_path = os.path.join(tmp_dir, "report.json")
        if not os.path.isfile(result_path):
            return []
        try:
            with open(result_path, "r", encoding="utf-8") as fh:
                entries = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
    if not isinstance(entries, list):
        return []

    findings: List[WebFinding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("RuleID", "unknown")
        file_path = entry.get("File", "")
        line = entry.get("StartLine", "")
        findings.append(WebFinding(
            id=f"gitleaks-{rule_id}-{abs(hash(file_path + str(line))) % 100000}",
            category="exposure",
            severity="CRITICAL",
            title=f"Leaked secret detected: {rule_id}",
            description=f"gitleaks found a likely {rule_id} secret in {file_path}" + (f" (line {line})" if line else ""),
            url=file_path,
            evidence=f"{file_path}:{line}" if line else file_path,
            remediation=(
                "Revoke/rotate the exposed credential immediately, remove it from git history "
                "(git-filter-repo or BFG), and move secrets to environment variables or a secrets manager."
            ),
        ))
    return findings


def run_semgrep(repo_dir: str) -> List[WebFinding]:
    """Runs semgrep's auto-config rule set (community-maintained SAST rules
    covering OWASP Top 10 patterns across most common languages) against a
    local repo clone."""
    out = _run_tool_with_mount(repo_dir, [
        "semgrep", "scan", "--config=auto", "--json", "--quiet",
        "--timeout", "120", "/repo",
    ], timeout=180)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    severity_map = {"ERROR": "HIGH", "WARNING": "MEDIUM", "INFO": "LOW"}
    findings: List[WebFinding] = []
    for result in (data.get("results") or []):
        if not isinstance(result, dict):
            continue
        check_id = result.get("check_id", "unknown")
        path = result.get("path", "")
        start_line = (result.get("start") or {}).get("line", "")
        extra = result.get("extra") or {}
        message = extra.get("message", check_id)
        severity = severity_map.get(str(extra.get("severity", "")).upper(), "MEDIUM")
        metadata = extra.get("metadata") or {}
        cwe = metadata.get("cwe")
        cwe_ids = cwe if isinstance(cwe, list) else ([cwe] if cwe else [])
        findings.append(WebFinding(
            id=f"semgrep-{check_id.replace('.', '-')}-{abs(hash(path + str(start_line))) % 100000}",
            category="exposure",
            severity=severity,
            title=message[:120],
            description=message,
            url=path,
            evidence=f"{path}:{start_line}" if start_line else path,
            references=metadata.get("references", []) or [],
            remediation=metadata.get("fix", "") or "Review the flagged code pattern and apply the recommended fix from the semgrep rule.",
        ))
    return findings


async def run_code_scan_toolkit(
    repo_dir: str,
    on_progress: Optional[ProgressCb] = None,
) -> List[WebFinding]:
    """Run gitleaks + semgrep against a local repo clone directory. Returns
    an empty list (never raises) if Docker/the image aren't available."""
    async def _p(msg: str, pct: Optional[int] = None) -> None:
        logger.info("[code-scan] %s%s", msg, f" ({pct}%)" if pct is not None else "")
        if on_progress:
            try:
                await on_progress(msg, pct)
            except Exception:  # noqa: BLE001
                pass

    status = check_docker_status()
    if not status.docker_installed:
        await _p("Docker not detected -- skipping secret/SAST scan (gitleaks, semgrep). Install Docker for deeper code scanning.", None)
        return []
    if not status.image_available:
        ok = await ensure_image(on_progress)
        if not ok:
            await _p("Could not obtain the scan toolkit image -- skipping secret/SAST scan.", None)
            return []

    def _run_all() -> List[WebFinding]:
        out: List[WebFinding] = []
        logger.info("[code-scan] Running gitleaks…")
        gitleaks_findings = run_gitleaks(repo_dir)
        logger.info("[code-scan] gitleaks finished (%d finding(s)).", len(gitleaks_findings))
        out.extend(gitleaks_findings)
        logger.info("[code-scan] Running semgrep…")
        semgrep_findings = run_semgrep(repo_dir)
        logger.info("[code-scan] semgrep finished (%d finding(s)).", len(semgrep_findings))
        out.extend(semgrep_findings)
        return out

    await _p("Scanning source for leaked secrets and SAST findings (gitleaks, semgrep)…", 60)
    findings = await asyncio.to_thread(_run_all)
    await _p(f"Code scan found {len(findings)} finding(s).", 70)
    return findings


# ---------------------------------------------------------------------------
# Orchestration: run all Docker-backed tools for one target
# ---------------------------------------------------------------------------

async def run_docker_toolkit(
    site_url: str,
    on_progress: Optional[ProgressCb] = None,
    crawled_urls: Optional[List[str]] = None,
) -> "tuple[List[WebFinding], List[str]]":
    """Run the full Docker-backed tool suite against ``site_url``. Returns
    (findings, technologies) -- both empty if Docker/the image aren't
    available; the caller's deterministic Python checks stand on their own.
    ``technologies`` feeds the same OSV cross-reference the pure-Python
    fingerprinter's results already go through (see orchestrator.py).
    ``crawled_urls`` (optional, from the site crawl manifest) is used to
    pick parameterized URLs for dalfox's XSS probing and to decide whether
    WordPress-specific tooling is worth running."""
    async def _p(msg: str, pct: Optional[int] = None) -> None:
        if on_progress:
            try:
                await on_progress(msg, pct)
            except Exception:  # noqa: BLE001
                pass

    status = check_docker_status()
    if not status.docker_installed:
        await _p("Docker not detected -- skipping the deep scan toolkit (nmap/httpx/nikto/whatweb/testssl.sh/nuclei/ffuf/dalfox/subfinder/wpscan). Install Docker for a more thorough scan.", None)
        return [], []

    if not status.image_available:
        ok = await ensure_image(on_progress)
        if not ok:
            await _p("Could not obtain the scan toolkit image -- skipping the deep scan toolkit.", None)
            return [], []

    hostname = urlparse(site_url).netloc.split(":")[0]
    urls_with_params = [u for u in (crawled_urls or []) if "?" in u]

    def _timed(tool_name: str, fn, *args):
        """Runs one tool, logging start/finish/count to the console in real
        time -- so anyone watching the terminal sees exactly which tool is
        running right now, not just a single aggregate "running toolkit"
        message that gives no visibility into a multi-minute scan."""
        logger.info("[web-vuln-scan] Running %s…", tool_name)
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - one tool failing must not abort the rest
            logger.warning("[web-vuln-scan] %s failed: %s", tool_name, exc)
            raise
        count = len(result[0]) if isinstance(result, tuple) else len(result)
        logger.info("[web-vuln-scan] %s finished (%d finding(s)).", tool_name, count)
        return result

    def _timed_safe(tool_name: str, fn, *args, default):
        try:
            return _timed(tool_name, fn, *args)
        except Exception:  # noqa: BLE001 - already logged in _timed
            return default

    # Every tool below is independent of every other (none consumes another's
    # output) except wpscan, which only makes sense once WordPress has been
    # fingerprinted -- so it runs after this batch, not inside it. Measured
    # empirically per-tool: nmap ~150s, httpx ~5-60s, whatweb ~35-100s,
    # nikto ~175-220s, testssl ~30-150s, nuclei ~10-150s, subfinder+dnsx
    # ~5-60s, ffuf ~95-120s, dalfox up to 450s worst case (10 URLs x 45s).
    # Running them sequentially in one thread (the original implementation)
    # sums to well over the frontend's scan timeout; running them
    # concurrently instead bounds total wall-clock to roughly the slowest
    # single tool rather than the sum of all of them.
    await _p("Running deep scan toolkit (nmap, httpx, whatweb, nikto, testssl.sh, nuclei, subfinder, ffuf, dalfox) in parallel…", 60)
    (
        nmap_findings,
        (httpx_findings, httpx_techs),
        whatweb_techs,
        nikto_findings,
        testssl_findings,
        nuclei_findings,
        subdomain_findings,
        ffuf_findings,
        dalfox_findings,
    ) = await asyncio.gather(
        asyncio.to_thread(_timed_safe, "nmap", run_nmap, hostname, default=[]),
        asyncio.to_thread(_timed_safe, "httpx", run_httpx, site_url, default=([], [])),
        asyncio.to_thread(_timed_safe, "whatweb", run_whatweb, site_url, default=[]),
        asyncio.to_thread(_timed_safe, "nikto", run_nikto, site_url, default=[]),
        asyncio.to_thread(_timed_safe, "testssl", run_testssl, hostname, default=[]),
        asyncio.to_thread(_timed_safe, "nuclei", run_nuclei, site_url, default=[]),
        asyncio.to_thread(_timed_safe, "subfinder+dnsx", run_subdomain_recon, hostname, default=[]),
        asyncio.to_thread(_timed_safe, "ffuf", run_ffuf, site_url, default=[]),
        asyncio.to_thread(_timed_safe, "dalfox", run_dalfox, urls_with_params, default=[]),
    )

    findings: List[WebFinding] = []
    technologies: List[str] = list(httpx_techs) + list(whatweb_techs)
    findings.extend(nmap_findings)
    findings.extend(httpx_findings)
    findings.extend(nikto_findings)
    findings.extend(testssl_findings)
    findings.extend(nuclei_findings)
    findings.extend(subdomain_findings)
    findings.extend(ffuf_findings)
    findings.extend(dalfox_findings)

    is_wordpress = any("wordpress" in t.lower() for t in technologies)
    if is_wordpress:
        await _p("WordPress detected -- running wpscan…", 65)
        wpscan_findings = await asyncio.to_thread(
            _timed_safe, "wpscan", run_wpscan, site_url, default=[])
        findings.extend(wpscan_findings)

    await _p(f"Deep scan toolkit found {len(findings)} additional finding(s).", 70)
    return findings, technologies
