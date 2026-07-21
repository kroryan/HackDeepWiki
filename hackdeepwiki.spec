# -*- mode: python ; coding: utf-8 -*-
import sys
import os
import importlib.util
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

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
    "fastapi", "uvicorn", "pydantic", "adalflow", "google.generativeai",
    "tiktoken", "tiktoken_ext", "websockets", "azure.identity", "azure.core",
    "boto3", "botocore", "requests", "jinja2", "aiohttp", "langid", "numpy",
    "openai", "ollama", "faiss", "libzim",
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

datas = [
    # Package the frontend files (Next.js standalone output)
    ('.next/standalone/server.js', '.'),
    ('.next/standalone/node_modules', 'node_modules'),
    ('.next/standalone/.next', '.next'),  # Contains required-server-files.json and server files
    ('public', 'public'),
    ('.next/static', '.next/static'),
    # Package python api package
    ('api', 'api'),
    # Package scripts package (includes hackdeepwiki_config.py for runtime Ollama discovery)
    ('scripts', 'scripts'),
]

# Package the Node binary if present
if os.path.exists(node_source_path):
    datas.append((node_source_path, 'bin'))
else:
    print(f"Warning: Node.js executable not found at {node_source_path}. Make sure it is downloaded before running PyInstaller.")

# Package the tiktoken cache if present
if os.path.exists(tiktoken_cache_source):
    datas.append((tiktoken_cache_source, 'tiktoken_cache'))
else:
    print(f"Warning: tiktoken cache not found at {tiktoken_cache_source}.")

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
    'google',
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
    'libzim'
]

hidden_imports = []

for pkg in packages_to_collect:
    try:
        submodules = collect_submodules(pkg)
        hidden_imports.extend(submodules)
    except Exception as e:
        print(f"Warning: Could not collect submodules for {pkg}: {e}")

# Collect data files if needed
for pkg in ['adalflow', 'langid']:
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
