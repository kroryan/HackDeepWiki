"""Dependency manifest/lockfile parser.

Walks a locally-cloned repo, discovers dependency declarations across the
supported ecosystems, and resolves them to ``(name, version, ecosystem)``.

Design goals:
    * Pure stdlib (``json``, ``re``, ``tomllib`` with a ``tomli`` fallback).
    * Never raise on malformed input -- a broken lockfile is logged and
      skipped, it never aborts the whole scan.
    * Prefer lockfile versions over manifest ranges (lockfiles are exact).
    * Ignore noise directories (``node_modules``, ``.git``, ``venv`` ...) and
      honour the wiki's ``excluded_dirs``/``excluded_files`` filters.

The "category" (client / server / dependency) is a best-effort heuristic used
only to route findings into the wiki's three subsections; it is not a security
claim.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from api.vuln_scanner.models import Dependency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TOML support (stdlib in 3.11+, optional tomli otherwise, regex fallback)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - environment dependent
    import tomllib  # type: ignore
    _HAS_TOMLLIB = True
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
        _HAS_TOMLLIB = True
    except ModuleNotFoundError:
        tomllib = None  # type: ignore
        _HAS_TOMLLIB = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Always-ignored directory names (would otherwise explode the scan with
# transitive vendored copies / build output).
_DEFAULT_IGNORE_DIRS: Set[str] = {
    "node_modules", ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "vendor", "dist", "build", "target",
    "__pycache__", ".next", ".nuxt", ".cache",
    " Pods",  # never matches; placeholder
    ".idea", ".vscode",
    "Pods",  # CocoaPods vendored deps
    ".mypy_cache", ".pytest_cache", ".tox",
    "bower_components",
}

# Known client-side framework package names (lowercased, no scope prefix for npm
# is handled by also matching the tail after '/').
_CLIENT_HINTS: Set[str] = {
    "react", "react-dom", "react-router", "react-router-dom", "redux",
    "vue", "vue-router", "vuex", "pinia", "@vue",
    "angular", "@angular", "rxjs", "ng-bootstrap",
    "svelte", "sveltekit", "@sveltejs",
    "solid-js", "preact", "jquery", "bootstrap", "tailwindcss",
    "vuetify", "quasar", "element-ui", "element-plus", "antd", "@mui",
    "material-ui", "chakra-ui", "@chakra-ui", "next", "nuxt", "gatsby",
    "vite", "webpack", "rollup", "parcel", "eslint",
    "lit", "stimulus", "htmx", "alpinejs", "ember", "ember-source",
    "thymeleaf",  # server-rendered but UI layer
    "tkinter", "PyQt", "PySide", "wxpython",
}

_SERVER_HINTS: Set[str] = {
    "express", "koa", "fastify", "hapi", "restify", "nestjs", "@nestjs",
    "next",  # can be server; ambiguous - left out below
    "django", "flask", "fastapi", "tornado", "bottle", "aiohttp",
    "starlette", "sanic", "pyramid", "celery", "uvicorn", "gunicorn",
    "rails", "sinatra", "rack", "puma",
    "spring-boot", "spring-boot-starter", "spring-web", "spring",
    "jetty", "tomcat", "undertow",
    "gin", "echo", "fiber", "iris", "gorilla",
    "actix-web", "axum", "rocket", "warp", "tower-http",
    "laravel", "symfony", "lumen",
    "aspnetcore", "microsoft.aspnetcore",
    "grpc", "grpc-tools",
}

# Directory segment names that hint at client vs server placement (matched as
# whole path segments, e.g. ".../frontend/package.json" -> segment "frontend").
_CLIENT_PATH_HINTS = ("client", "frontend", "web", "ui", "browser", "static",
                      "public", "components", "pages", "views", "app")
_SERVER_PATH_HINTS = ("server", "backend", "api", "services", "lambda",
                      "functions", "workers", "daemon")


def _norm(name: str) -> str:
    return name.strip().lower().lstrip("@").lstrip("/")


def _strip_semver_ops(version: str) -> str:
    """Strip npm/semver range operators (^, ~, >=, <=, >, <, =) and a leading
    'v' to get the bare version. Keeps things like '4.18.1' out of '^4.18.1'.
    For ranges like '>=4.0 <5.0' returns the lower bound."""
    v = version.strip()
    if not v:
        return ""
    # range with space: take first comparator
    v = v.split()[0]
    v = re.sub(r"^[=~^<>!]*\s*", "", v)
    v = v.lstrip("v")
    # strip wildcard / x ranges -> empty (can't query)
    if "x" in v.lower() or "*" in v:
        return ""
    return v


def _path_segments(source_files: List[str]) -> Set[str]:
    """Split each source file's directory path into lowercase segments (and
    the bare filename stem), so hint matching is exact-segment rather than
    substring -- a manifest literally named ``requirements.txt`` or
    ``build.gradle`` must never match the "ui" client hint just because "ui"
    happens to appear inside the filename."""
    segments: Set[str] = set()
    for src in source_files:
        norm = src.lower().replace("\\", "/")
        parts = [p for p in norm.split("/") if p]
        # Only directory components carry a path signal; drop the filename
        # itself (the manifest's own name, e.g. "requirements.txt", is not a
        # client/server hint).
        segments.update(parts[:-1])
    return segments


def _infer_category(name: str, ecosystem: str, source_files: List[str],
                    dev: bool) -> str:
    """Best-effort client/server/dependency classification."""
    n = _norm(name)
    # npm scoped: @scope/pkg -> check both whole and pkg
    tail = n.split("/")[-1]

    # Path hints take precedence (strong signal) -- matched against whole
    # directory segments only (e.g. ".../frontend/package.json" -> "frontend"),
    # never as a substring of the manifest filename itself.
    segments = _path_segments(source_files)
    client_path_hints = {h.rstrip("/") for h in _CLIENT_PATH_HINTS}
    server_path_hints = {h.rstrip("/") for h in _SERVER_PATH_HINTS}
    if segments & client_path_hints:
        return "client"
    if segments & server_path_hints:
        return "server"

    # Known framework hints
    if n in _CLIENT_HINTS or tail in _CLIENT_HINTS or n.startswith("@vue") \
            or n.startswith("@angular") or n.startswith("@mui") \
            or n.startswith("@chakra-ui") or n.startswith("@sveltejs"):
        return "client"
    if n in _SERVER_HINTS or tail in _SERVER_HINTS:
        return "server"

    # Ecosystem defaults
    if ecosystem == "npm" and tail in {"express", "koa", "fastify", "cors",
                                       "body-parser", "helmet", "passport",
                                       "sequelize", "typeorm", "prisma",
                                       "mongoose", "knex", "pg", "mysql2"}:
        return "server"
    if ecosystem == "PyPI" and n in {"django", "flask", "fastapi", "tornado",
                                     "starlette", "aiohttp", "celery",
                                     "sqlalchemy", "flask-login"}:
        return "server"

    return "dependency"


# ---------------------------------------------------------------------------
# Per-format parsers. Each returns list of (name, version, dev) for one file.
# ---------------------------------------------------------------------------

def _parse_package_json(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.debug("package.json parse failed %s: %s", path, exc)
        return out
    for key, dev in (("dependencies", False), ("peerDependencies", False),
                     ("optionalDependencies", False),
                     ("devDependencies", True)):
        block = data.get(key) or {}
        if not isinstance(block, dict):
            continue
        for name, ver in block.items():
            if isinstance(ver, str):
                v = _strip_semver_ops(ver)
                if name and v:
                    out.append((name, v, dev))
            # npm package.json alias: "name": "npm:other@1.2.3"
    return out


def _parse_package_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.debug("package-lock parse failed %s: %s", path, exc)
        return out
    # lockfileVersion 2/3 -> "packages" keyed by paths
    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, meta in packages.items():
            if not isinstance(meta, dict):
                continue
            # the root package has key "" -> skip
            if not key:
                continue
            name = key.split("node_modules/")[-1]
            ver = meta.get("version")
            dev = bool(meta.get("dev")) or bool(meta.get("optional"))
            if name and isinstance(ver, str) and ver:
                out.append((name, ver, dev))
        if out:
            return out
    # lockfileVersion 1 -> nested "dependencies"
    def _walk(deps: dict) -> None:
        for name, meta in (deps or {}).items():
            if not isinstance(meta, dict):
                continue
            ver = meta.get("version")
            if name and isinstance(ver, str) and ver:
                out.append((name, ver, bool(meta.get("dev"))))
            _walk(meta.get("dependencies"))
    _walk(data.get("dependencies"))
    return out


_YARN_BLOCK_RE = re.compile(r'^"?([^\s@]+(?:@[^:]+)?)@[^:]*:\s*$')
_YARN_VER_RE = re.compile(r'^\s*version\s+"([^"]+)"\s*$')


def _parse_yarn_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        logger.debug("yarn.lock parse failed %s: %s", path, exc)
        return out
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line and not line.startswith(" ") and line.endswith(":"):
            # header may list multiple specs separated by ", "
            specs = line[:-1].split(", ")
            names: List[str] = []
            for spec in specs:
                # spec like "express@^4.18.1" or "@scope/pkg@^1.0.0"
                m = re.match(r'^"?([^\s]+?)@[^@]+$', spec.strip().strip('"'))
                if m:
                    nm = m.group(1)
                    if nm.startswith("@"):
                        # @scope/pkg -> keep full scoped name
                        names.append(nm)
                    else:
                        names.append(nm)
            # find version within this block (indented lines until blank)
            j = i + 1
            ver = ""
            while j < n and (lines[j].startswith(" ") or lines[j] == ""):
                if lines[j] == "":
                    break
                vm = _YARN_VER_RE.match(lines[j])
                if vm and not ver:
                    ver = vm.group(1)
                j += 1
            if names and ver:
                for nm in names:
                    out.append((nm, ver, False))
            i = j
        else:
            i += 1
    return out


_REQ_LINE_RE = re.compile(
    r'^\s*([A-Za-z0-9_.\-]+\s*(?:\[[^\]]*\])?)\s*([=<>!~]=|=|~=|>=|<=|>|<)?\s*([0-9A-Za-z_.\-+*]+)?'
)


def _parse_requirements(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        logger.debug("requirements parse failed %s: %s", path, exc)
        return out
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or line.startswith("git+") \
                or line.startswith("http") or "://" in line:
            continue
        # strip environment markers and extras
        line = line.split(";")[0].strip()
        m = _REQ_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1).replace(" ", "").strip("[]")
        # extras like 'package[extra]' -> 'package'
        name = name.split("[")[0].strip()
        ver = m.group(3) or ""
        if ver and ("*" in ver or "x" in ver.lower()):
            ver = ""
        if name and ver:
            out.append((name, ver, False))
    return out


def _parse_pipfile_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.debug("Pipfile.lock parse failed %s: %s", path, exc)
        return out
    for section, dev in (("default", False), ("develop", True)):
        block = data.get(section) or {}
        if not isinstance(block, dict):
            continue
        for name, meta in block.items():
            if isinstance(meta, dict):
                ver = meta.get("version", "")
                if ver:
                    out.append((name, ver.lstrip("="), dev))
    return out


def _parse_pyproject(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    data = None
    if _HAS_TOMLLIB:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except Exception as exc:
            logger.debug("pyproject.toml parse failed %s: %s", path, exc)
            data = None
    if data is None:
        # regex fallback: only [tool.poetry.dependencies] + [project] deps
        return _parse_pyproject_regex(path)

    def _emit(specs: List[str], dev: bool) -> None:
        for spec in specs:
            if not isinstance(spec, str):
                continue
            m = re.match(r'^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*([=<>!~]=|=|~=|>=|<=|>|<)?\s*([0-9A-Za-z_.\-+*]+)?', spec)
            if not m:
                continue
            name = m.group(1)
            ver = m.group(3) or ""
            if ver and ("*" in ver or "x" in ver.lower()):
                ver = ""
            if name and ver:
                out.append((name, ver, dev))

    project = data.get("project") or {}
    if isinstance(project, dict):
        _emit(list(project.get("dependencies") or []), False)
        for group in ((project.get("optional-dependencies") or {}).values()):
            _emit(list(group), True)

    poetry = (data.get("tool") or {}).get("poetry") or {}
    if isinstance(poetry, dict):
        deps = poetry.get("dependencies") or {}
        if isinstance(deps, dict):
            for name, ver in deps.items():
                if name == "python":
                    continue
                if isinstance(ver, str):
                    v = _strip_semver_ops(ver.replace("^", "").replace("~", ""))
                    if name and v:
                        out.append((name, v, False))
                elif isinstance(ver, dict):
                    v = ver.get("version")
                    if v and name:
                        out.append((name, _strip_semver_ops(v), False))
        for key, dev in (("dev-dependencies", True), ("group", True)):
            block = poetry.get(key) or {}
            if isinstance(block, dict):
                for grp in block.values():
                    if isinstance(grp, dict):
                        for name, ver in (grp.get("dependencies") or {}).items():
                            if isinstance(ver, str):
                                v = _strip_semver_ops(ver)
                                if name and v:
                                    out.append((name, v, dev))
                            elif isinstance(ver, dict) and ver.get("version"):
                                out.append((name, _strip_semver_ops(ver["version"]), dev))
    return out


def _parse_pyproject_regex(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    # [project] dependencies = [ "name>=1.0", ... ]
    for m in re.finditer(r'"([A-Za-z0-9_.\-]+)\s*[=<>!~]=?\s*([0-9A-Za-z_.\-+*]+)"', text):
        name, ver = m.group(1), m.group(2)
        if "*" not in ver and "x" not in ver.lower():
            out.append((name, ver, False))
    return out


def _parse_poetry_lock(path: str) -> List[Tuple[str, str, bool]]:
    """Poetry's lock file -- the actually-resolved versions, as opposed to
    pyproject.toml's ">=" ranges (see _parse_pyproject). Without this parser
    registered (see _FILE_HANDLERS' priority-3 entry for it), a project
    pinned like `aiohttp = ">=3.8.4"` but actually resolved by poetry to a
    much newer, unaffected version would still get reported as "3.8.4" and
    checked against CVEs for a version nothing is actually running --
    exactly the false-positive/false-negative failure mode this file's own
    docstring says lockfiles exist to prevent.

    [[package]] blocks look like:
        [[package]]
        name = "aiohttp"
        version = "3.14.1"
        ...
        [package.extras]
        ...
    A dedicated (regex, not full TOML) reader keeps this robust across
    poetry.lock schema versions without needing poetry's own lock-format
    parser as a dependency, symmetric with _parse_pyproject_regex's fallback
    for the same reason.
    """
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        logger.debug("poetry.lock parse failed %s: %s", path, exc)
        return out

    dev_names: Set[str] = set()
    try:
        pyproject_path = os.path.join(os.path.dirname(path), "pyproject.toml")
        if _HAS_TOMLLIB and os.path.isfile(pyproject_path):
            with open(pyproject_path, "rb") as fh:
                pdata = tomllib.load(fh)
            groups = ((pdata.get("tool") or {}).get("poetry") or {}).get("group") or {}
            for grp in groups.values():
                if isinstance(grp, dict):
                    dev_names.update(_norm(n) for n in (grp.get("dependencies") or {}).keys())
            dev_names.update(_norm(n) for n in ((pdata.get("tool") or {}).get("poetry") or {})
                              .get("dev-dependencies", {}).keys())
    except Exception:
        pass  # best-effort dev/main split; default to "main" (dev=False) below

    for block in text.split("[[package]]")[1:]:
        name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
        ver_m = re.search(r'version\s*=\s*"([^"]+)"', block)
        if name_m and ver_m:
            name = name_m.group(1)
            out.append((name, ver_m.group(1), _norm(name) in dev_names))
    return out


_SETUP_PY_SPEC_RE = re.compile(r'''["']([A-Za-z0-9_.\-]+)\s*[=<>!~]=?\s*([0-9A-Za-z_.\-+*]+)["']''')


def _parse_setup_py(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for m in _SETUP_PY_SPEC_RE.finditer(text):
        name, ver = m.group(1), m.group(2)
        if "*" not in ver and "x" not in ver.lower():
            out.append((name, ver, False))
    return out


# --- Go ---

_GO_REQUIRE_RE = re.compile(r'^\s*([A-Za-z0-9_./\-]+)\s+(v[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.\-]*)')


def _parse_go_mod(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        logger.debug("go.mod parse failed %s: %s", path, exc)
        return out
    in_block = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block:
            m = _GO_REQUIRE_RE.match(line)
            if m:
                # indirect marker "// indirect" is fine, ignored
                out.append((m.group(1), m.group(2), False))
        elif line.startswith("require "):
            m = _GO_REQUIRE_RE.match(line[len("require "):])
            if m:
                out.append((m.group(1), m.group(2), False))
    return out


# --- Rust ---

_CARGO_DEP_RE = re.compile(
    r'^\s*([A-Za-z0-9_\-]+)\s*=\s*(?:"([^"]+)"|\{\s*[^}]*?version\s*=\s*"([^"]+)")'
)


def _parse_cargo_toml(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        logger.debug("Cargo.toml parse failed %s: %s", path, exc)
        return out
    section = ""
    for raw in lines:
        line = raw.rstrip()
        sec = re.match(r'^\s*\[([^\]]+)\]\s*$', line)
        if sec:
            section = sec.group(1).strip().lower()
            continue
        if not any(section == s for s in (
                "dependencies", "dev-dependencies", "build-dependencies")):
            continue
        dev = section != "dependencies"
        m = _CARGO_DEP_RE.match(line)
        if m:
            name = m.group(1)
            ver = m.group(2) or m.group(3) or ""
            if name and ver:
                out.append((name, ver, dev))
    return out


def _parse_cargo_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for block in re.findall(r'\[\[package\]\]\s*(.*?)(?=\n\[\[|\Z)', text, re.S):
        nm = re.search(r'^\s*name\s*=\s*"([^"]+)"', block, re.M)
        vr = re.search(r'^\s*version\s*=\s*"([^"]+)"', block, re.M)
        if nm and vr:
            out.append((nm.group(1), vr.group(1), False))
    return out


# --- Maven ---

def _parse_pom_xml(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as exc:
        logger.debug("pom.xml parse failed %s: %s", path, exc)
        return out
    for block in re.findall(r"<dependency>(.*?)</dependency>", text, re.S | re.I):
        gid = re.search(r"<groupId>(.*?)</groupId>", block, re.S | re.I)
        aid = re.search(r"<artifactId>(.*?)</artifactId>", block, re.S | re.I)
        ver = re.search(r"<version>(.*?)</version>", block, re.S | re.I)
        scope = re.search(r"<scope>(.*?)</scope>", block, re.S | re.I)
        if gid and aid and ver:
            g = gid.group(1).strip()
            a = aid.group(1).strip()
            v = ver.group(1).strip()
            if "${" in v:
                continue  # property placeholder, can't resolve
            dev = scope and scope.group(1).strip().lower() in ("test", "provided")
            out.append((f"{g}:{a}", v, dev))
    return out


_GRADLE_DEP_RE = re.compile(
    r'''(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testCompileOnly|debugImplementation|annotationProcessor)\s*[('"]+(?:['"])?([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+):([0-9A-Za-z_.\-]+)'''
)


def _parse_build_gradle(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for m in _GRADLE_DEP_RE.finditer(text):
        g, a, v = m.group(1), m.group(2), m.group(3)
        if v and "$" not in v:
            out.append((f"{g}:{a}", v, False))
    return out


# --- Ruby ---

_GEMFILE_RE = re.compile(r'''gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?''')


def _parse_gemfile(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for m in _GEMFILE_RE.finditer(text):
        name = m.group(1)
        ver = m.group(2) or ""
        ver = _strip_semver_ops(ver)
        if name and ver:
            out.append((name, ver, False))
        elif name:
            out.append((name, "", False))  # version resolved from lock
    return out


_GEMLOCK_RE = re.compile(r'^\s{4}([A-Za-z0-9_.\-]+)\s+\(([0-9A-Za-z_.\-]+)\)')


def _parse_gemfile_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return out
    in_specs = False
    for raw in lines:
        if raw.strip() == "specs:":
            in_specs = True
            continue
        if in_specs:
            if raw and not raw.startswith(" "):
                in_specs = False
                continue
            m = _GEMLOCK_RE.match(raw)
            if m:
                # version may be a set like "(1.2.3, 1.3.0)" -> take last
                vers = re.findall(r'[0-9A-Za-z_.\-]+', m.group(2))
                out.append((m.group(1), vers[-1] if vers else "", False))
    return out


# --- PHP (Packagist) ---

def _parse_composer_json(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception:
        return out
    for key, dev in (("require", False), ("require-dev", True)):
        block = data.get(key) or {}
        if isinstance(block, dict):
            for name, ver in block.items():
                if name.startswith("php") or name.startswith("ext-"):
                    continue
                v = _strip_semver_ops(str(ver))
                if name and v:
                    out.append((name, v, dev))
    return out


def _parse_composer_lock(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception:
        return out
    for key, dev in (("packages", False), ("packages-dev", True)):
        for pkg in (data.get(key) or []):
            if isinstance(pkg, dict):
                name = pkg.get("name")
                ver = pkg.get("version")
                if name and isinstance(ver, str):
                    out.append((name, ver, dev))
    return out


# --- NuGet ---

_CSProj_PKGREF_RE = re.compile(
    r'<PackageReference\s+Include\s*=\s*"([^"]+)"\s+Version\s*=\s*"([^"]+)"',
    re.I,
)
_PACKAGES_CONFIG_RE = re.compile(
    r'<package\s+id\s*=\s*"([^"]+)"\s+version\s*=\s*"([^"]+)"', re.I,
)


def _parse_csproj(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for m in _CSProj_PKGREF_RE.finditer(text):
        out.append((m.group(1), m.group(2), False))
    return out


def _parse_packages_config(path: str) -> List[Tuple[str, str, bool]]:
    out: List[Tuple[str, str, bool]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return out
    for m in _PACKAGES_CONFIG_RE.finditer(text):
        out.append((m.group(1), m.group(2), False))
    return out


# ---------------------------------------------------------------------------
# Registry + walker
# ---------------------------------------------------------------------------

# filename -> (parser, ecosystem, priority). Higher priority (lockfiles) wins
# when both a manifest and lockfile resolve the same package.
_FILE_HANDLERS: Dict[str, Tuple["callable", str, int]] = {
    "package.json": (_parse_package_json, "npm", 1),
    "package-lock.json": (_parse_package_lock, "npm", 3),
    "yarn.lock": (_parse_yarn_lock, "npm", 3),
    "requirements.txt": (_parse_requirements, "PyPI", 1),
    "pyproject.toml": (_parse_pyproject, "PyPI", 2),
    "setup.py": (_parse_setup_py, "PyPI", 1),
    "pipfile.lock": (_parse_pipfile_lock, "PyPI", 3),
    "poetry.lock": (_parse_poetry_lock, "PyPI", 3),
    "go.mod": (_parse_go_mod, "Go", 3),
    "cargo.toml": (_parse_cargo_toml, "crates.io", 1),
    "cargo.lock": (_parse_cargo_lock, "crates.io", 3),
    "pom.xml": (_parse_pom_xml, "Maven", 2),
    "build.gradle": (_parse_build_gradle, "Maven", 2),
    "gemfile": (_parse_gemfile, "RubyGems", 1),
    "gemfile.lock": (_parse_gemfile_lock, "RubyGems", 3),
    "composer.json": (_parse_composer_json, "Packagist", 1),
    "composer.lock": (_parse_composer_lock, "Packagist", 3),
    "packages.config": (_parse_packages_config, "NuGet", 2),
}
# .csproj handled by extension


def _should_ignore_dir(name: str, excluded: Set[str]) -> bool:
    if name in _DEFAULT_IGNORE_DIRS or name.startswith("."):
        return True
    return name.lower() in {e.lower().strip("/") for e in excluded}


def parse_dependencies(
    repo_dir: str,
    excluded_dirs: Optional[List[str]] = None,
    excluded_files: Optional[List[str]] = None,
) -> List[Dependency]:
    """Walk ``repo_dir`` and return a deduplicated list of dependencies.

    When both a manifest (e.g. package.json) and a lockfile (package-lock.json)
    declare the same package, the lockfile's exact version wins.
    """
    excluded = set(excluded_dirs or [])
    excl_files = {f.lower() for f in (excluded_files or [])}

    # key (ecosystem, name) -> (Dependency, priority)
    merged: Dict[Tuple[str, str], Tuple[Dependency, int]] = {}
    manifests_parsed = 0

    def _record(name: str, version: str, ecosystem: str, dev: bool,
                source_file: str, priority: int) -> None:
        if not name or not version:
            return
        key = (ecosystem, name)
        existing = merged.get(key)
        if existing is None or priority > existing[1]:
            if existing is not None:
                # merge source files
                srcs = list(dict.fromkeys(existing[0].source_files + [source_file]))
                dev = existing[0].dev or dev
            else:
                srcs = [source_file]
            dep = Dependency(
                name=name, version=version, ecosystem=ecosystem,
                dev=dev, source_files=srcs,
            )
            merged[key] = (dep, priority)
        else:
            # same/lower priority: keep version but record the extra source file
            dep = existing[0]
            if source_file not in dep.source_files:
                dep.source_files.append(source_file)

    for root, dirs, files in os.walk(repo_dir):
        # mutate dirs in place to prune ignored subtrees
        dirs[:] = [d for d in dirs if not _should_ignore_dir(d, excluded)]
        for fname in files:
            if fname.lower() in excl_files:
                continue
            lname = fname.lower()
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, repo_dir).replace("\\", "/")

            handler = _FILE_HANDLERS.get(lname)
            ext_handler = None
            if lname.endswith(".csproj"):
                ext_handler = (_parse_csproj, "NuGet", 2)

            if handler:
                parser, ecosystem, priority = handler
            elif ext_handler:
                parser, ecosystem, priority = ext_handler
            else:
                continue

            try:
                entries = parser(full)
            except Exception as exc:  # never let one bad file abort the scan
                logger.debug("parser %s failed on %s: %s", lname, rel, exc)
                continue
            manifests_parsed += 1
            for name, version, dev in entries:
                _record(name, version, ecosystem, dev, rel, priority)

    # Finalise: infer category for each dependency.
    deps: List[Dependency] = []
    for (ecosystem, name), (dep, _prio) in merged.items():
        dep.category = _infer_category(name, ecosystem, dep.source_files, dep.dev)
        deps.append(dep)

    logger.info("Parsed %d unique dependencies across %d manifests in %s",
                len(deps), manifests_parsed, repo_dir)
    return deps


# ---------------------------------------------------------------------------
# Usage-file discovery (only run for deps that actually have CVEs, to keep
# the scan cheap on big repos).
# ---------------------------------------------------------------------------

# Map ecosystem -> import/require tokens we look for in source files.
_IMPORT_PATTERNS: Dict[str, "re.Pattern"] = {
    "npm": re.compile(
        r"""(?:require\s*\(\s*['"]([^'"]+)['"]|import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"])"""
    ),
    "PyPI": re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_\.]+)", re.M),
    "Go": re.compile(r'"([A-Za-z0-9_./\-]+)"'),
    "crates.io": re.compile(r"\buse\s+([A-Za-z0-9_:]+)|extern\s+crate\s+([A-Za-z0-9_]+)"),
    "Maven": None,  # Java imports use class names, not group:artifact; skip
    "RubyGems": re.compile(r'\brequire\s+["\']?([^"\'\s;]+)'),
    "Packagist": re.compile(r"\buse\s+([A-Za-z0-9_\\]+)"),
    "NuGet": None,
}

# file extensions worth grepping per ecosystem
_SCAN_EXTS: Dict[str, Set[str]] = {
    "npm": {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"},
    "PyPI": {".py"},
    "Go": {".go"},
    "crates.io": {".rs"},
    "Maven": set(),
    "RubyGems": {".rb"},
    "Packagist": {".php"},
    "NuGet": set(),
}


def find_usage_files(repo_dir: str, deps: List[Dependency],
                     excluded_dirs: Optional[List[str]] = None,
                     max_per_dep: int = 8,
                     max_file_size: int = 512 * 1024) -> Dict[str, List[str]]:
    """Best-effort: for each dependency, find source files that import/require it.

    Returns a dict keyed by ``f"{ecosystem}:{name}"``. Only the dependencies
    passed in are searched for (caller should pass only vulnerable deps to keep
    this cheap). Grep is capped per dep and skips binary/huge files.
    """
    excluded = set(excluded_dirs or [])
    result: Dict[str, List[str]] = {f"{d.ecosystem}:{d.name}": [] for d in deps}

    # Pre-build per-ecosystem search tokens
    tokens: Dict[str, List[Dependency]] = {}
    for d in deps:
        tokens.setdefault(d.ecosystem, []).append(d)
    if not tokens:
        return result

    # Walk once
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if not _should_ignore_dir(d, excluded)]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            # which ecosystems care about this extension?
            for ecosystem, dep_list in tokens.items():
                if ext not in _SCAN_EXTS.get(ecosystem, set()):
                    continue
                full = os.path.join(root, fname)
                try:
                    if os.path.getsize(full) > max_file_size:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except Exception:
                    continue
                rel = os.path.relpath(full, repo_dir).replace("\\", "/")
                pat = _IMPORT_PATTERNS.get(ecosystem)
                if pat is None:
                    continue
                for m in pat.finditer(text):
                    captured = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
                    if not captured:
                        continue
                    cand = captured.strip()
                    # match against deps in this ecosystem
                    for dep in dep_list:
                        key = f"{ecosystem}:{dep.name}"
                        if len(result[key]) >= max_per_dep:
                            continue
                        if _matches_usage(dep.name, ecosystem, cand, rel):
                            if rel not in result[key]:
                                result[key].append(rel)
    return result


def _matches_usage(dep_name: str, ecosystem: str, imported: str,
                   file_rel: str) -> bool:
    """Decide whether an import token refers to ``dep_name``."""
    imp = imported.strip().strip("'\"")
    if ecosystem == "npm":
        # import 'express' or import 'react-dom/server' or '@scope/pkg'
        if imp == dep_name or imp.startswith(dep_name + "/"):
            return True
        # bare subpath import without name? skip
        return False
    if ecosystem == "PyPI":
        # import pkg / from pkg import x  -- match top-level module
        return imp.split(".")[0].lower().replace("_", "-") == dep_name.lower().replace("_", "-")
    if ecosystem == "Go":
        return imp == dep_name or imp.startswith(dep_name + "/")
    if ecosystem == "crates.io":
        # use pkg::... or extern crate pkg
        cand = (imported or "").replace("::", "").strip()
        return cand.lower() == dep_name.lower() or imp.lower() == dep_name.lower()
    if ecosystem == "RubyGems":
        return imp.lower() == dep_name.lower()
    if ecosystem == "Packagist":
        # use Vendor\Package -> Vendor/Package
        norm = imp.replace("\\", "/").lower()
        return norm == dep_name.lower() or norm.startswith(dep_name.lower() + "/")
    return False