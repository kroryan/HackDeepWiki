# -*- mode: python ; coding: utf-8 -*-
import sys
import os
import importlib.util
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Make the LOCAL ./scripts and ./api packages win over anything in
# site-packages for every import this spec performs (collect_submodules,
# _REQUIRED_IMPORTS). This matters since Engraphis: its pip install ships a
# top-level `scripts` package, and in CI `pyinstaller hackdeepwiki.spec` runs
# as a console script (cwd NOT on sys.path), so without this insert
# collect_submodules('scripts') would import and freeze ENGRAPHIS's scripts
# package instead of HackDeepWiki's. SPECPATH is the directory of this file.
sys.path.insert(0, SPECPATH)

block_cipher = None

# ---------------------------------------------------------------------------
# Fail fast if a required runtime dependency is missing from the build env.
# PyInstaller's collect_submodules() silently skips packages that aren't
# installed, which produces a bundle that *builds green* but crashes at
# startup with ModuleNotFoundError (e.g. libzim). Aborting here turns that
# silent failure into a loud, actionable build error — for both local and
# CI builds. Keep this list in sync with `packages_to_collect` below.
# ---------------------------------------------------------------------------
_REQUIRED_IMPORTS = [
    "fastapi", "uvicorn", "pydantic", "adalflow", "google.genai",
    "tiktoken", "tiktoken_ext", "websockets", "azure.identity", "azure.core",
    "boto3", "botocore", "requests", "jinja2", "aiohttp", "langid", "numpy",
    "openai", "ollama", "faiss", "libzim",
    "mwparserfromhell", "playwright", "bs4", "markdownify", "neo4j",
    "httpx",  # api/code_agent (embedded opencode agent)
    "engraphis",  # Engraphis memory + embedded dashboard (api/engraphis_integration.py);
                  # installed fresh from upstream by scripts/prepare_assets.py on every build
    "python_multipart",  # required by the Engraphis dashboard's Form/Upload routes
]
_missing = [m for m in _REQUIRED_IMPORTS if importlib.util.find_spec(m) is None]
if _missing:
    raise SystemExit(
        "\n[build aborted] missing required dependencies: "
        + ", ".join(_missing)
        + "\nPyInstaller would silently skip them and ship a broken bundle."
        + "\nInstall them first (e.g. `poetry install --only main` in api/,"
        " or `pip install <pkg>`).\n"
    )

# Identify current OS
is_win = sys.platform.startswith('win')

# Define target paths
node_bin_name = 'node.exe' if is_win else 'node'
node_source_path = os.path.abspath(os.path.join('bin', node_bin_name))
tiktoken_cache_source = os.path.abspath('tiktoken_cache')

# The api/ and scripts/ trees are shipped as data, but ONLY their source
# files. A developer's working copy accumulates build leftovers inside api/
# (dist/ and build/ from running PyInstaller in there, logs/, __pycache__)
# that a clean CI checkout never has — bundling the raw directory made a
# local AppImage ~750 MB heavier than the CI artifact built from the same
# commit. Filtering here keeps local and CI builds byte-identical in content
# regardless of working-tree state.
_TREE_EXCLUDED_DIRS = {'dist', 'build', 'logs', '__pycache__', '.pytest_cache',
                       '.mypy_cache', '.ruff_cache', '.claude',
                       # api/.venv is the build environment itself (poetry
                       # installs it in-project, locally AND in CI) -- bundling
                       # it shipped ~460 MB of site-packages as dead weight
                       # inside the frozen app's api/ source tree.
                       '.venv', '.venv311', '.git'}
_TREE_EXCLUDED_EXTS = ('.pyc', '.pyo', '.log')


def _source_tree(src_root, dest_root):
    entries = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in _TREE_EXCLUDED_DIRS]
        for filename in filenames:
            if filename.endswith(_TREE_EXCLUDED_EXTS):
                continue
            full = os.path.join(dirpath, filename)
            rel_dir = os.path.relpath(dirpath, src_root)
            dest = dest_root if rel_dir == '.' else os.path.join(dest_root, rel_dir)
            entries.append((full, dest))
    return entries


datas = [
    # Package the frontend files (Next.js standalone output)
    ('.next/standalone/server.js', '.'),
    ('.next/standalone/node_modules', 'node_modules'),
    ('.next/standalone/.next', '.next'),  # Contains required-server-files.json and server files
    ('public', 'public'),
    ('.next/static', '.next/static'),
    ('build/components.json', 'build'),
]
# Package the python api package and the scripts package (includes
# hackdeepwiki_config.py for runtime Ollama discovery), sources only.
datas.extend(_source_tree('api', 'api'))
datas.extend(_source_tree('scripts', 'scripts'))
if os.path.exists('build/build-info.json'):
    datas.append(('build/build-info.json', 'build'))

# Package the Node binary if present
if os.path.exists(node_source_path):
    datas.append((node_source_path, 'bin'))
else:
    raise SystemExit(
        f"[build aborted] Node.js executable not found at {node_source_path}. "
        "Run scripts/prepare_assets.py first."
    )

# Package the checksum-verified OpenCode agent. Release builds must never
# advertise Code Editing mode while silently omitting its executable.
opencode_bin_name = 'opencode.exe' if is_win else 'opencode'
opencode_source_path = os.path.abspath(os.path.join('bin', opencode_bin_name))
if os.path.exists(opencode_source_path):
    datas.append((opencode_source_path, 'bin'))
else:
    raise SystemExit(
        f"[build aborted] OpenCode executable not found at {opencode_source_path}. "
        "Run scripts/prepare_assets.py first."
    )

# Package the tiktoken cache if present
if os.path.exists(tiktoken_cache_source):
    datas.append((tiktoken_cache_source, 'tiktoken_cache'))
else:
    raise SystemExit(
        f"[build aborted] tiktoken cache not found at {tiktoken_cache_source}. "
        "Run scripts/prepare_assets.py first."
    )

# Collect all hidden submodules of dynamic libraries
packages_to_collect = [
    'api',
    'api.vuln_scanner',
    'api.vuln_common',
    'api.web_crawler',
    'api.web_vuln_scanner',
    'neo4j',
    'scripts',
    'fastapi',
    'uvicorn',
    'pydantic',
    'adalflow',
    'google.genai',
    'tiktoken',
    'tiktoken_ext',
    'websockets',
    'azure',
    'boto3',
    'botocore',
    'requests',
    'jinja2',
    'aiohttp',
    'langid',
    'numpy',
    'openai',
    'ollama',
    'faiss',
    'libzim',
    'mwparserfromhell',
    'playwright',
    'bs4',
    'markdownify',
    # Engraphis memory engine (core is numpy-only; the dashboard reuses the
    # fastapi/uvicorn already bundled). NOTE: engraphis also installs a
    # top-level `scripts` package into site-packages -- the local ./scripts
    # dir shadows it because builds run from the project root, and we
    # deliberately never collect engraphis's copy.
    'engraphis',
    'python_multipart',
]

hidden_imports = []


def _runtime_submodule(name):
    """Keep package discovery out of vendor test/benchmark trees.

    Several runtime libraries ship their entire upstream test suite in the
    wheel.  Freezing those modules can pull pytest and large optional stacks
    (pandas, pyarrow, scipy, matplotlib) into an otherwise clean release.
    """
    parts = name.split('.')
    return not any(
        part in {'test', 'tests', 'testing', 'benchmarks'}
        or part == 'conftest'
        or part.startswith('test_')
        for part in parts
    )


for pkg in packages_to_collect:
    try:
        submodules = collect_submodules(pkg, filter=_runtime_submodule)
        hidden_imports.extend(submodules)
    except Exception as e:
        print(f"Warning: Could not collect submodules for {pkg}: {e}")

# Collect data files if needed. engraphis ships its whole dashboard SPA as
# package data (engraphis/static: index.html + dashboard.js/css + vendored
# d3/marked/DOMPurify) -- without these the embedded memory dashboard 404s.
for pkg in ['adalflow', 'langid', 'playwright', 'engraphis']:
    try:
        datas.extend(collect_data_files(pkg))
    except Exception as e:
        print(f"Warning: Could not collect data files for {pkg}: {e}")

a = Analysis(
    ['scripts/launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if is_win:
    # Windows keeps onefile: users download one .exe and double-click it, and
    # onefile's per-run extraction to %TEMP% is fast enough there not to matter.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='hackdeepwiki',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon='icono.ico',
    )
else:
    # Linux uses onedir: the AppImage already wraps everything into a single
    # file for the user, so there is no UX benefit to onefile here — and
    # onefile's bootloader re-extracts the whole bundle (Node.js binary, FAISS,
    # adalflow, the Next.js standalone build, ...) into a fresh /tmp/_MEIxxxxxx
    # on EVERY launch, with no way to cache that across runs. onedir extracts
    # once at build time; sys._MEIPASS then points straight at the on-disk
    # folder, so every launch skips extraction entirely.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='hackdeepwiki',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='hackdeepwiki',
    )
