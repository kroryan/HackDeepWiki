# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

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
    'faiss'
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='freedeepwiki',
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
)
