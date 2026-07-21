"""Deterministic checks: security headers, cookies, TLS, and common exposed
paths. All best-effort and non-destructive (GET requests only, no fuzzing,
no auth bypass attempts) -- this is a hygiene pass, not a pentest tool.
"""

from __future__ import annotations

import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from api.web_vuln_scanner.models import (
    CATEGORY_COOKIES,
    CATEGORY_EXPOSURE,
    CATEGORY_HEADERS,
    CATEGORY_TLS,
    WebFinding,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (compatible; HackDeepWikiBot/1.0; security-check)"


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

# (header name, severity if missing, remediation)
_EXPECTED_HEADERS = [
    ("Strict-Transport-Security", "HIGH",
     "Add `Strict-Transport-Security: max-age=31536000; includeSubDomains` to force HTTPS on all future requests."),
    ("Content-Security-Policy", "MEDIUM",
     "Add a Content-Security-Policy header to restrict which scripts/styles/frames the browser may load."),
    ("X-Frame-Options", "MEDIUM",
     "Add `X-Frame-Options: DENY` (or a CSP frame-ancestors directive) to prevent clickjacking via iframes."),
    ("X-Content-Type-Options", "LOW",
     "Add `X-Content-Type-Options: nosniff` to stop browsers from MIME-sniffing responses."),
    ("Referrer-Policy", "LOW",
     "Add a `Referrer-Policy` header (e.g. `strict-origin-when-cross-origin`) to limit referrer leakage."),
    ("Permissions-Policy", "LOW",
     "Add a `Permissions-Policy` header to explicitly disable unused browser features (camera, geolocation, ...)."),
]

_INFO_LEAK_HEADERS = ["Server", "X-Powered-By", "X-AspNet-Version", "X-Generator"]


def check_headers(url: str, headers: Dict[str, str]) -> List[WebFinding]:
    findings: List[WebFinding] = []
    lower_headers = {k.lower(): v for k, v in headers.items()}

    for name, severity, remediation in _EXPECTED_HEADERS:
        if name.lower() not in lower_headers:
            findings.append(WebFinding(
                id=f"missing-{name.lower()}",
                category=CATEGORY_HEADERS,
                severity=severity,
                title=f"Missing {name} header",
                description=f"The response for {url} does not set a {name} header.",
                url=url,
                remediation=remediation,
            ))

    for name in _INFO_LEAK_HEADERS:
        if name.lower() in lower_headers:
            findings.append(WebFinding(
                id=f"info-leak-{name.lower()}",
                category=CATEGORY_HEADERS,
                severity="LOW",
                title=f"{name} header reveals server details",
                description=f"{name}: {lower_headers[name.lower()]}",
                url=url,
                evidence=lower_headers[name.lower()],
                remediation=f"Remove or generalize the {name} header to avoid advertising exact software/versions to attackers.",
            ))

    return findings


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def check_cookies(url: str, response: requests.Response) -> List[WebFinding]:
    findings: List[WebFinding] = []
    for cookie in response.cookies:
        flags_missing = []
        if not cookie.secure:
            flags_missing.append("Secure")
        # http.cookiejar.Cookie stores HttpOnly in _rest (case varies by source)
        rest = getattr(cookie, "_rest", {}) or {}
        has_httponly = any(k.lower() == "httponly" for k in rest)
        if not has_httponly:
            flags_missing.append("HttpOnly")
        samesite = next((v for k, v in rest.items() if k.lower() == "samesite"), None)
        if not samesite:
            flags_missing.append("SameSite")

        if flags_missing:
            findings.append(WebFinding(
                id=f"cookie-missing-flags-{cookie.name}",
                category=CATEGORY_COOKIES,
                severity="MEDIUM" if "Secure" in flags_missing or "HttpOnly" in flags_missing else "LOW",
                title=f"Cookie '{cookie.name}' missing {', '.join(flags_missing)}",
                description=(
                    f"The cookie '{cookie.name}' set by {url} is missing: {', '.join(flags_missing)}."
                ),
                url=url,
                remediation=(
                    "Set Secure (send only over HTTPS), HttpOnly (block JS access, mitigates XSS "
                    "cookie theft), and SameSite=Lax/Strict (mitigates CSRF) on all session/auth cookies."
                ),
            ))
    return findings


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def check_tls(hostname: str, port: int = 443) -> List[WebFinding]:
    findings: List[WebFinding] = []
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                proto = ssock.version()

        if proto in ("TLSv1", "TLSv1.1", "SSLv3", "SSLv2"):
            findings.append(WebFinding(
                id="weak-tls-protocol",
                category=CATEGORY_TLS,
                severity="HIGH",
                title=f"Weak TLS protocol in use: {proto}",
                description=f"The server negotiated {proto}, which is deprecated and has known weaknesses.",
                url=f"https://{hostname}",
                evidence=proto,
                remediation="Disable TLS 1.0/1.1 and SSLv3 server-side; require TLS 1.2 or newer.",
            ))

        not_after = cert.get("notAfter") if cert else None
        if not_after:
            try:
                expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                days_left = (expiry - datetime.now(timezone.utc)).days
                if days_left < 0:
                    findings.append(WebFinding(
                        id="tls-cert-expired",
                        category=CATEGORY_TLS,
                        severity="CRITICAL",
                        title="TLS certificate has expired",
                        description=f"The certificate for {hostname} expired on {not_after}.",
                        url=f"https://{hostname}",
                        evidence=not_after,
                        remediation="Renew the TLS certificate immediately.",
                    ))
                elif days_left < 14:
                    findings.append(WebFinding(
                        id="tls-cert-expiring-soon",
                        category=CATEGORY_TLS,
                        severity="MEDIUM",
                        title=f"TLS certificate expires in {days_left} day(s)",
                        description=f"The certificate for {hostname} expires on {not_after}.",
                        url=f"https://{hostname}",
                        evidence=not_after,
                        remediation="Renew the TLS certificate before it expires (consider automated renewal, e.g. Let's Encrypt/ACME).",
                    ))
            except ValueError:
                pass
    except ssl.SSLCertVerificationError as exc:
        findings.append(WebFinding(
            id="tls-cert-invalid",
            category=CATEGORY_TLS,
            severity="HIGH",
            title="TLS certificate verification failed",
            description=str(exc),
            url=f"https://{hostname}",
            remediation="Use a certificate signed by a trusted CA (self-signed/expired/mismatched-hostname certs break HTTPS for visitors).",
        ))
    except (socket.timeout, socket.gaierror, ConnectionRefusedError, OSError) as exc:
        logger.debug("TLS check failed for %s: %s", hostname, exc)
    return findings


# ---------------------------------------------------------------------------
# Common exposed paths
# ---------------------------------------------------------------------------

# (path, id-slug, severity, description)
_SENSITIVE_PATHS = [
    ("/.env", "exposed-dotenv", "CRITICAL", "Environment file exposed -- often contains secrets/API keys/DB credentials."),
    ("/.git/config", "exposed-git-config", "CRITICAL", "Exposed .git directory -- source code and history may be fully downloadable."),
    ("/.git/HEAD", "exposed-git-head", "CRITICAL", "Exposed .git directory -- source code and history may be fully downloadable."),
    ("/wp-config.php.bak", "exposed-wpconfig-bak", "CRITICAL", "WordPress config backup exposed -- likely contains DB credentials."),
    ("/.htpasswd", "exposed-htpasswd", "HIGH", "Exposed .htpasswd file -- may contain crackable password hashes."),
    ("/config.json", "exposed-config-json", "MEDIUM", "A config.json is publicly accessible -- verify it holds no secrets."),
    ("/.aws/credentials", "exposed-aws-credentials", "CRITICAL", "AWS credentials file exposed."),
    ("/backup.sql", "exposed-db-backup", "CRITICAL", "Database backup file exposed."),
    ("/.DS_Store", "exposed-ds-store", "LOW", "macOS .DS_Store exposed -- can leak directory listings/filenames."),
    ("/server-status", "exposed-server-status", "MEDIUM", "Apache mod_status page exposed -- reveals active requests/IPs."),
    ("/phpinfo.php", "exposed-phpinfo", "HIGH", "phpinfo() page exposed -- reveals detailed server configuration."),
]

# Response bodies under this size for a "sensitive" hit are almost always a
# generic SPA/404 fallback page, not the real file -- skip to avoid noise.
_MIN_HIT_BYTES = 10


def check_exposed_paths(base_url: str, timeout: int = _TIMEOUT) -> List[WebFinding]:
    findings: List[WebFinding] = []
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # Establish what a genuine 404 looks like on this site first, so a SPA
    # that returns 200 + its index page for every path doesn't false-positive
    # every single probe below.
    probe_404_body = None
    try:
        r = requests.get(urljoin(root, "/__hackdeepwiki_nonexistent_probe__"),
                         timeout=timeout, headers={"User-Agent": _USER_AGENT}, allow_redirects=False)
        probe_404_body = r.text[:200] if r.status_code == 200 else None
    except requests.RequestException:
        pass

    for path, finding_id, severity, description in _SENSITIVE_PATHS:
        url = urljoin(root, path)
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT}, allow_redirects=False)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        body = resp.text[:200]
        if len(resp.content) < _MIN_HIT_BYTES:
            continue
        if probe_404_body is not None and body == probe_404_body:
            continue  # SPA fallback served the same page as a known-bogus path
        findings.append(WebFinding(
            id=finding_id,
            category=CATEGORY_EXPOSURE,
            severity=severity,
            title=f"Sensitive path exposed: {path}",
            description=description,
            url=url,
            evidence=f"HTTP {resp.status_code}, {len(resp.content)} bytes",
            remediation=f"Remove or block public access to {path} (web server config / .htaccess / firewall rule).",
        ))
    return findings
