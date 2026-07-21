"""Neo4j graph persistence for web vulnerability scan results -- HackDeepWiki's
own independent port of the schema/connection pattern used by RedAmon's
graph_db module (tmp/redamon/graph_db/), trimmed to what a single-user local
app actually needs.

Differences from RedAmon's graph_db:
    - No user_id/project_id tenant scoping -- HackDeepWiki is a local,
      single-user app, not a multi-tenant SaaS. Nodes are scoped by
      ``site_url`` instead.
    - One flat client, no mixin-per-topic split -- the node/relationship
      surface here is a small fraction of RedAmon's (Domain, IP, Port,
      Service, Technology, Certificate, CVE, Finding), so a single class is
      more legible than the multi-mixin architecture RedAmon needs at its
      scale.
    - No GVM/OSINT/secret-hunt/attack-chain node types -- those model
      RedAmon's other scan modules (Shodan/Censys enrichment, credential
      leaks, multi-step exploitation chains), which have no HackDeepWiki
      equivalent.

Entirely optional: every call site wraps this in a try/except and the
report still renders without a graph if Neo4j isn't running (see
orchestrator.py) -- same fail-open posture as the rest of the vuln scanners.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from api.web_vuln_scanner.models import WebFinding, WebVulnReport

logger = logging.getLogger(__name__)

DEFAULT_URI = os.environ.get("HACKDEEPWIKI_NEO4J_URI", "bolt://localhost:7687")
DEFAULT_USER = os.environ.get("HACKDEEPWIKI_NEO4J_USER", "neo4j")
DEFAULT_PASSWORD = os.environ.get("HACKDEEPWIKI_NEO4J_PASSWORD", "hackdeepwiki_secret")

CONSTRAINTS = [
    "CREATE CONSTRAINT fdw_site_unique IF NOT EXISTS FOR (s:Site) REQUIRE s.url IS UNIQUE",
    "CREATE CONSTRAINT fdw_port_unique IF NOT EXISTS FOR (p:Port) REQUIRE (p.number, p.site_url) IS UNIQUE",
    "CREATE CONSTRAINT fdw_technology_unique IF NOT EXISTS FOR (t:Technology) REQUIRE (t.name, t.version, t.site_url) IS UNIQUE",
    "CREATE CONSTRAINT fdw_finding_unique IF NOT EXISTS FOR (f:Finding) REQUIRE (f.id, f.site_url) IS UNIQUE",
    "CREATE CONSTRAINT fdw_cve_unique IF NOT EXISTS FOR (c:CVE) REQUIRE c.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX fdw_finding_severity IF NOT EXISTS FOR (f:Finding) ON (f.severity)",
    "CREATE INDEX fdw_finding_category IF NOT EXISTS FOR (f:Finding) ON (f.category)",
    "CREATE INDEX fdw_site_scanned_at IF NOT EXISTS FOR (s:Site) ON (s.last_scanned_at)",
]


def _init_schema(session) -> None:
    for stmt in CONSTRAINTS + INDEXES:
        try:
            session.run(stmt)
        except Exception as exc:  # noqa: BLE001
            if "already exists" not in str(exc).lower():
                logger.debug("graph_db schema statement failed (non-fatal): %s", exc)


class WebVulnGraphClient:
    """Thin Neo4j client. Use as a context manager; every public method is
    itself resilient (returns quietly on failure) so a scan never breaks
    because the graph store is unreachable."""

    def __init__(self, uri: Optional[str] = None, user: Optional[str] = None,
                password: Optional[str] = None):
        from neo4j import GraphDatabase  # lazy import -- optional dependency
        self.driver = GraphDatabase.driver(
            uri or DEFAULT_URI, auth=(user or DEFAULT_USER, password or DEFAULT_PASSWORD),
        )
        with self.driver.session() as session:
            _init_schema(session)

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "WebVulnGraphClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def verify_connection(self) -> bool:
        try:
            with self.driver.session() as session:
                return session.run("RETURN 1 AS test").single()["test"] == 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("Neo4j connection check failed: %s", exc)
            return False

    def clear_site_data(self, site_url: str) -> int:
        """Wipe all nodes for this site before writing a fresh scan -- same
        "clear before rescan" pattern RedAmon uses, scoped by site_url
        instead of (user_id, project_id)."""
        try:
            with self.driver.session() as session:
                result = session.run(
                    "MATCH (n {site_url: $site_url}) DETACH DELETE n RETURN count(n) AS deleted",
                    site_url=site_url,
                )
                record = result.single()
                return record["deleted"] if record else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_db: clear_site_data failed (non-fatal): %s", exc)
            return 0

    def write_report(self, report: WebVulnReport) -> None:
        """Persist a full WebVulnReport as a graph: Site node, Port/
        Technology/Finding/CVE nodes, and relationships between them."""
        try:
            self._write_report_inner(report)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_db: write_report failed (non-fatal, report still saved to wikicache): %s", exc)

    def _write_report_inner(self, report: WebVulnReport) -> None:
        hostname = urlparse(report.site_url).netloc.split(":")[0]
        with self.driver.session() as session:
            session.run(
                """
                MERGE (s:Site {url: $site_url})
                SET s.hostname = $hostname,
                    s.last_scanned_at = $generated_at,
                    s.pages_scanned = $pages_scanned,
                    s.total_findings = $total_findings
                """,
                site_url=report.site_url, hostname=hostname,
                generated_at=report.generated_at, pages_scanned=report.pages_scanned,
                total_findings=report.total_findings,
            )

            for tech in report.detected_technologies:
                name = tech.get("name", "") if isinstance(tech, dict) else str(tech)
                if not name:
                    continue
                base_name, _, version = name.partition("/")
                session.run(
                    """
                    MERGE (t:Technology {name: $name, version: $version, site_url: $site_url})
                    WITH t
                    MATCH (s:Site {url: $site_url})
                    MERGE (s)-[:RUNS]->(t)
                    """,
                    name=base_name or name, version=version or "", site_url=report.site_url,
                )

            for finding in report.all_findings:
                self._write_finding(session, report.site_url, finding)

    @staticmethod
    def _write_finding(session, site_url: str, finding: Dict[str, Any]) -> None:
        session.run(
            """
            MERGE (f:Finding {id: $id, site_url: $site_url})
            SET f.category = $category, f.severity = $severity, f.title = $title,
                f.description = $description, f.url = $url, f.evidence = $evidence,
                f.remediation = $remediation, f.ai_proposed = $ai_proposed,
                f.ai_dismissed = $ai_dismissed
            WITH f
            MATCH (s:Site {url: $site_url})
            MERGE (s)-[:HAS_FINDING]->(f)
            """,
            id=finding.get("id", ""), site_url=site_url,
            category=finding.get("category", ""), severity=finding.get("severity", "INFO"),
            title=finding.get("title", ""), description=finding.get("description", ""),
            url=finding.get("url", ""), evidence=finding.get("evidence", ""),
            remediation=finding.get("remediation", ""),
            ai_proposed=bool(finding.get("ai_proposed")), ai_dismissed=bool(finding.get("ai_dismissed")),
        )
        cve_id = finding.get("cve_id")
        if cve_id:
            session.run(
                """
                MERGE (c:CVE {id: $cve_id})
                SET c.cvss_score = $cvss_score
                WITH c
                MATCH (f:Finding {id: $finding_id, site_url: $site_url})
                MERGE (f)-[:REFERENCES]->(c)
                """,
                cve_id=cve_id, cvss_score=finding.get("cvss_score"),
                finding_id=finding.get("id", ""), site_url=site_url,
            )

    def get_site_history(self, site_url: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return this site's finding counts over recent scans, if Postgres
        run-history is also being used this only reflects the graph's
        current (latest) state -- Postgres (postgres_store.py) is the
        source of truth for historical trend data."""
        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (s:Site {url: $site_url})-[:HAS_FINDING]->(f:Finding)
                    RETURN f.severity AS severity, count(f) AS count
                    """,
                    site_url=site_url,
                )
                return [dict(r) for r in result]
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph_db: get_site_history failed: %s", exc)
            return []


def try_persist_report(report: WebVulnReport) -> bool:
    """Best-effort helper for the orchestrator: connect, write, close.
    Returns False (never raises) if Neo4j isn't reachable -- the report
    still gets saved to wikicache regardless."""
    try:
        with WebVulnGraphClient() as client:
            client.clear_site_data(report.site_url)
            client.write_report(report)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("Neo4j not available for graph persistence (scan report still saved): %s", exc)
        return False
