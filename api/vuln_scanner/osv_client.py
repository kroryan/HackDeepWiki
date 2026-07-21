"""OSV.dev API client.

OSV.dev (run by Google/OpenSSF) is free, requires no API key, and aggregates
30+ advisory sources (NVD, GitHub Advisory, PyPA, RustSec, Go vuln db, ...).

Two-step lookup:
    1. ``POST /v1/querybatch`` -> returns only vulnerability IDs (and
       ``modified``) per query. Accepts up to 1000 queries per call; we chunk
       well below that.
    2. ``GET /v1/vulns/{id}`` -> the full record (summary, details, severity,
       affected ranges w/ fixed events, references, CWE ids, ...).

We dedupe IDs across deps (one vuln often hits several deps) so the detail
fetch is one HTTP call per unique vuln, not per (dep, vuln).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import requests

from api.vuln_scanner.models import (
    CVEFinding,
    Dependency,
    severity_from_score,
)

logger = logging.getLogger(__name__)

OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{vuln_id}"
_BATCH_SIZE = 500  # well under OSV's 1000-query cap
_HTTP_TIMEOUT = 30
_MAX_RETRIES = 2


def _post_with_retry(url: str, json_body: dict) -> Optional[dict]:
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=json_body, timeout=_HTTP_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - network is unpredictable
            last_exc = exc
            if attempt < _MAX_RETRIES:
                time.sleep(1.5 ** attempt)
                continue
            logger.warning("OSV POST %s failed: %s", url, exc)
    if last_exc:
        logger.warning("OSV POST %s giving up: %s", url, last_exc)
    return None


def _get_with_retry(url: str) -> Optional[dict]:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=_HTTP_TIMEOUT)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt < _MAX_RETRIES:
                time.sleep(1.5 ** attempt)
                continue
            logger.debug("OSV GET %s failed: %s", url, exc)
            return None
    return None


# ---------------------------------------------------------------------------
# Version comparison (best-effort, dotted numeric)
# ---------------------------------------------------------------------------

def _version_key(v: str) -> Tuple:
    """Turn a version string into a tuple of ints for ordering. Non-numeric
    segments contribute 0 so comparison degrades gracefully."""
    parts = []
    for tok in str(v).lstrip("v").replace("-", ".").split("."):
        num = ""
        for ch in tok:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


# ---------------------------------------------------------------------------
# OSV record -> CVEFinding
# ---------------------------------------------------------------------------

# Advisory sources spell severity labels inconsistently (GHSA uses
# "MODERATE" where our scale uses "MEDIUM"; some ecosystems use lowercase or
# "IMPORTANT"/"MINOR"). Normalise the ones actually seen in the wild instead
# of silently discarding them as UNKNOWN.
_SEVERITY_LABEL_ALIASES = {
    "MODERATE": "MEDIUM",
    "IMPORTANT": "HIGH",
    "MINOR": "LOW",
    "NEGLIGIBLE": "LOW",
    "NONE": "UNKNOWN",
}


def _normalise_severity_label(label: str) -> str:
    label = label.strip().upper()
    label = _SEVERITY_LABEL_ALIASES.get(label, label)
    if label not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return "UNKNOWN"
    return label


def _extract_severity(record: dict) -> Tuple[str, Optional[float]]:
    """Return (severity_label, cvss_score). OSV stores severity in a few places;
    we check them in order of reliability."""
    # 1. top-level `severity` array (CVSS_V3/V4 vectors or numeric)
    score = None
    for entry in (record.get("severity") or []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("score")
        if isinstance(raw, (int, float)):
            score = float(raw)
            break
        if isinstance(raw, str):
            # NVD-style vector string sometimes carries a numeric score in
            # database_specific; try to parse a trailing number, else None.
            m = None
            score = _cvss_score_from_vector(raw)
            if score is not None:
                break
    # 2. database_specific.cvss.score (GitHub Advisory often has this)
    if score is None:
        db = record.get("database_specific") or {}
        cvss = db.get("cvss") if isinstance(db, dict) else None
        if isinstance(cvss, dict) and isinstance(cvss.get("score"), (int, float)):
            score = float(cvss["score"])

    label = "UNKNOWN"
    # 3. database_specific.severity label (GHSA: "CRITICAL"/"HIGH"/"MODERATE"/...)
    db = record.get("database_specific") or {}
    if isinstance(db, dict) and db.get("severity"):
        label = _normalise_severity_label(str(db["severity"]))
    # 4. affected[].ecosystem_specific.severity (some ecosystems)
    if label == "UNKNOWN":
        for aff in (record.get("affected") or []):
            eco = (aff or {}).get("ecosystem_specific") or {}
            if isinstance(eco, dict) and eco.get("severity"):
                label = _normalise_severity_label(str(eco["severity"]))
                if label != "UNKNOWN":
                    break
    # 5. derive from numeric score
    if label == "UNKNOWN" and score is not None:
        label = severity_from_score(score)
    return label, score


def _cvss_score_from_vector(vector: str) -> Optional[float]:
    """OSV severity `score` is usually a CVSS vector string, which does NOT
    embed the numeric base score. We can't reliably compute it from the vector
    here, so return None and let NVD enrichment (if a key is configured) fill
    the numeric score. A few records put a bare number in the field, handled
    by the caller."""
    # Some records (rare) embed "CVSS:3.1/... (7.5)" style annotations.
    m = None
    import re as _re
    m = _re.search(r'(\d{1,2}(?:\.\d+)?)\s*$', vector.strip())
    # Heuristic only: a trailing lone float at the end of the string
    if m and not vector.startswith("CVSS:"):
        try:
            val = float(m.group(1))
            if 0.0 <= val <= 10.0:
                return val
        except ValueError:
            pass
    return None


def _extract_fixed_version(record: dict, dep: Dependency) -> Optional[str]:
    """Find the fix version relevant to ``dep``'s installed version from the
    record's `affected` ranges."""
    installed = dep.version
    installed_key = _version_key(installed)
    candidates: List[str] = []
    for aff in (record.get("affected") or []):
        if not isinstance(aff, dict):
            continue
        pkg = aff.get("package") or {}
        if not isinstance(pkg, dict):
            continue
        if pkg.get("ecosystem") != dep.ecosystem:
            continue
        if (pkg.get("name") or "").lower() != dep.name.lower():
            continue
        for rng in (aff.get("ranges") or []):
            if not isinstance(rng, dict):
                continue
            events = rng.get("events") or []
            # walk events: an affected interval is [introduced, fixed).
            introduced = None
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                if "introduced" in ev:
                    introduced = ev["introduced"]
                if "fixed" in ev:
                    fixed = ev["fixed"]
                    fixed_key = _version_key(fixed)
                    # Only treat this as THE fix if installed actually falls
                    # in this vulnerable interval: introduced <= installed < fixed.
                    # A fix at/below the installed version belongs to a different
                    # (already-patched) interval and would be misleading to show.
                    if fixed_key > installed_key and (
                        introduced in (None, "0")
                        or installed_key >= _version_key(introduced)
                    ):
                        candidates.append(fixed)
    if not candidates:
        return None
    # smallest fix strictly greater than the installed version
    return min(candidates, key=_version_key)


def _extract_cwe_ids(record: dict) -> List[str]:
    cwes: List[str] = []
    db = record.get("database_specific") or {}
    if isinstance(db, dict):
        for cwe in (db.get("cwe_ids") or []):
            if isinstance(cwe, str) and cwe not in cwes:
                cwes.append(cwe)
    # some records list CWE under affected[].database_specific
    for aff in (record.get("affected") or []):
        adb = (aff or {}).get("database_specific") or {}
        if isinstance(adb, dict):
            for cwe in (adb.get("cwe_ids") or []):
                if isinstance(cwe, str) and cwe not in cwes:
                    cwes.append(cwe)
    return cwes


def _record_to_finding(record: dict, dep: Dependency) -> CVEFinding:
    severity, score = _extract_severity(record)
    refs = []
    for ref in (record.get("references") or []):
        if isinstance(ref, dict) and ref.get("url"):
            refs.append(ref["url"])
    aliases = [a for a in (record.get("aliases") or []) if isinstance(a, str)]
    return CVEFinding(
        id=record.get("id", ""),
        aliases=aliases,
        package_name=dep.name,
        package_ecosystem=dep.ecosystem,
        installed_version=dep.version,
        fixed_version=_extract_fixed_version(record, dep),
        severity=severity,
        cvss_score=score,
        summary=record.get("summary") or "",
        details=record.get("details") or "",
        references=refs,
        published=record.get("published") or "",
        cwe_ids=_extract_cwe_ids(record),
        category=dep.category,
        dev=dep.dev,
        source_files=list(dep.source_files),
        usage_files=list(dep.usage_files),
    )


def _canonical_id_map(records: Dict[str, dict]) -> Dict[str, str]:
    """Group vuln ids into alias-equivalence classes and pick one canonical id
    per class, so the same real-world vulnerability reported by OSV under
    multiple ids (e.g. a PyPA ``PYSEC-*`` advisory and its GitHub ``GHSA-*``
    alias -- OSV returns both as separate matches from querybatch) collapses
    into a single finding instead of being counted/shown twice.

    Preference order for the canonical id: CVE-* (most widely recognized) >
    GHSA-* (richest metadata) > whatever else was matched first.
    """
    # Union-find over ids present in `records`, linked via their `aliases`.
    parent: Dict[str, str] = {vid: vid for vid in records}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for vid, rec in records.items():
        for alias in (rec.get("aliases") or []):
            if alias in records:
                union(vid, alias)

    groups: Dict[str, List[str]] = {}
    for vid in records:
        groups.setdefault(find(vid), []).append(vid)

    def _rank(vid: str) -> int:
        if vid.startswith("CVE-"):
            return 0
        if vid.startswith("GHSA-"):
            return 1
        return 2

    canonical: Dict[str, str] = {}
    for members in groups.values():
        winner = sorted(members, key=lambda v: (_rank(v), v))[0]
        for vid in members:
            canonical[vid] = winner
    return canonical


def _backfill_severity_from_aliases(records: Dict[str, dict]) -> None:
    """Many advisory-database records (notably PyPA's PYSEC-* -- the primary
    source OSV returns for PyPI packages) carry no severity data at all, even
    though a GHSA/CVE alias for the exact same vulnerability does. Without
    this, most Python findings would misleadingly show as UNKNOWN severity.

    Mutates ``records`` in place: for any record with no usable severity, if
    one of its aliases is *also* in ``records`` (deduped in the same batch)
    and has a severity, copy it over. This never invents data -- it only
    reuses severity OSV itself already published under the sibling id.
    """
    def _has_severity(rec: dict) -> bool:
        label, score = _extract_severity(rec)
        return label != "UNKNOWN" or score is not None

    for vid, rec in records.items():
        if _has_severity(rec):
            continue
        for alias in (rec.get("aliases") or []):
            sibling = records.get(alias)
            if sibling and _has_severity(sibling):
                sib_label, sib_score = _extract_severity(sibling)
                # Graft the sibling's severity fields onto a copy so
                # _extract_severity's normal precedence picks them up.
                rec["database_specific"] = dict(rec.get("database_specific") or {})
                rec["database_specific"]["severity"] = sib_label
                if sib_score is not None and not rec.get("severity"):
                    rec["severity"] = [{"type": "CVSS_V3", "score": sib_score}]
                break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query_vulnerabilities(deps: List[Dependency],
                          nvd_enricher=None) -> List[CVEFinding]:
    """Query OSV for all ``deps`` and return a flat list of findings.

    ``nvd_enricher`` is an optional callable ``(findings) -> None`` that
    mutates findings in place to fill missing CVSS scores (used only when the
    user supplied an NVD API key).
    """
    if not deps:
        return []

    # 1. Batch query -> map (ecosystem, name, version) -> set of vuln ids
    dep_to_ids: Dict[int, Set[str]] = {i: set() for i in range(len(deps))}
    all_ids: Set[str] = set()

    queries = []
    idx_map: List[int] = []  # which dep each query corresponds to
    for i, dep in enumerate(deps):
        queries.append({
            "package": {"name": dep.name, "ecosystem": dep.ecosystem},
            "version": dep.version,
        })
        idx_map.append(i)

    for start in range(0, len(queries), _BATCH_SIZE):
        chunk = queries[start:start + _BATCH_SIZE]
        chunk_idx = idx_map[start:start + _BATCH_SIZE]
        body = {"queries": chunk}
        data = _post_with_retry(OSV_QUERYBATCH_URL, body)
        if not data:
            continue
        results = data.get("results") or []
        for q_i, result in enumerate(results):
            if q_i >= len(chunk_idx):
                break
            dep_i = chunk_idx[q_i]
            for v in (result.get("vulns") or []):
                vid = v.get("id")
                if vid:
                    dep_to_ids[dep_i].add(vid)
                    all_ids.add(vid)

    logger.info("OSV matched %d unique vulns across %d deps", len(all_ids), len(deps))
    if not all_ids:
        return []

    # 2. Fetch full records (deduped)
    records: Dict[str, dict] = {}
    for vid in all_ids:
        rec = _get_with_retry(OSV_VULN_URL.format(vuln_id=vid))
        if rec and rec.get("id"):
            records[vid] = rec

    # OSV often returns the SAME real-world vulnerability under two ids for
    # one match (e.g. PyPA's PYSEC-* and its GHSA-* alias) -- collapse those
    # into one canonical id so it isn't counted/shown twice, and backfill
    # severity across aliases (PYSEC records frequently carry none, while
    # their GHSA sibling does).
    _backfill_severity_from_aliases(records)
    canonical = _canonical_id_map(records)

    # 3. Build findings (one per (dep, canonical vuln) that OSV confirmed)
    findings: List[CVEFinding] = []
    for i, dep in enumerate(deps):
        seen_canonical: Set[str] = set()
        for vid in dep_to_ids[i]:
            rec = records.get(vid)
            if not rec:
                continue
            canon_id = canonical.get(vid, vid)
            if canon_id in seen_canonical:
                continue
            seen_canonical.add(canon_id)
            # Use the canonical record itself (richer/preferred source) when
            # it was also matched for this dep; otherwise fall back to the
            # record we actually have.
            canon_rec = records.get(canon_id, rec)
            try:
                findings.append(_record_to_finding(canon_rec, dep))
            except Exception as exc:  # never let one record break the scan
                logger.debug("Failed to map OSV record %s: %s", vid, exc)

    # 4. Optional NVD enrichment for missing CVSS scores
    if nvd_enricher is not None:
        try:
            nvd_enricher(findings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NVD enrichment failed (non-fatal): %s", exc)

    return findings