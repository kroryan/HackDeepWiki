import os
import sys
import shutil
import hashlib
import subprocess
import urllib.request
import tarfile
import tempfile

# Make the api package importable so the opencode version pin lives in ONE
# place (api/code_agent/binary.py) -- the runtime lazy-download and this
# build-time download must agree.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def copy_dir(src, dest):
    if not os.path.exists(src):
        print(f"Warning: Source directory {src} does not exist. Skipping.")
        return
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f"Copied {src} -> {dest}")

def download_file(url, dest_path, expected_sha256=None):
    print(f"Downloading {url} to {dest_path}...")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    # Custom headers to prevent potential request blocks
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as out_file:
        shutil.copyfileobj(response, out_file)
    if expected_sha256:
        digest = hashlib.sha256()
        with open(dest_path, "rb") as downloaded:
            for chunk in iter(lambda: downloaded.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(
                f"SHA-256 mismatch for {os.path.basename(dest_path)}: "
                f"expected {expected_sha256}, got {actual}"
            )
    print("Download finished.")

def setup_node_binary(platform):
    bin_dir = os.path.abspath("bin")
    os.makedirs(bin_dir, exist_ok=True)
    dest_path = os.path.join(
        bin_dir,
        "node.exe" if platform == "windows" else "node",
    )
    
    if platform == "windows":
        node_url = "https://nodejs.org/dist/v24.18.0/win-x64/node.exe"
        download_file(
            node_url,
            dest_path,
            "9a4eb5f1c29c6a2e93852ead46b999e284a6a5ca8bab4d4e241d587d025a52de",
        )
    elif platform == "linux":
        node_url = "https://nodejs.org/dist/v24.18.0/node-v24.18.0-linux-x64.tar.xz"
        
        # Download tarball to a temp file
        with tempfile.NamedTemporaryFile(delete=False) as temp_tar:
            temp_tar_path = temp_tar.name
            
        try:
            download_file(
                node_url,
                temp_tar_path,
                "55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742",
            )
            print("Extracting Node.js binary from tarball...")
            with tarfile.open(temp_tar_path, "r:xz") as tar:
                # Find the bin/node file inside the tarball
                node_member = None
                for member in tar.getmembers():
                    if member.name.endswith("bin/node") and not member.isdir():
                        node_member = member
                        break
                        
                if node_member:
                    # Extract it
                    node_member.name = "node"  # Rename to node
                    tar.extract(node_member, path=bin_dir, filter="data")
                    print(f"Extracted Node.js to {os.path.join(bin_dir, 'node')}")
                    # Set executable permissions
                    os.chmod(os.path.join(bin_dir, "node"), 0o755)
                else:
                    print("Error: Could not find bin/node in Node.js tarball")
                    sys.exit(1)
        finally:
            if os.path.exists(temp_tar_path):
                os.remove(temp_tar_path)
    else:
        print(f"Unknown platform: {platform}")
        sys.exit(1)
    node_version = subprocess.run(
        [dest_path, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    if node_version != "v24.18.0":
        raise RuntimeError(f"Bundled Node reported {node_version!r}, expected 'v24.18.0'")
    print(f"Node {node_version} executable validated at {dest_path}")

def setup_opencode_binary(platform):
    """Download the pinned opencode CLI release (anomalyco/opencode) into
    bin/ so the AppImage/.exe ships with the code agent embedded. Mirrors
    setup_node_binary. Release builds are strict: an executable advertised
    with Code Editing mode must
    contain a checksum-verified, runnable agent."""
    from api.code_agent.binary import (
        GITHUB_REPO,
        OPENCODE_VERSION,
        _extract_binary,
        verify_archive_checksum,
    )

    bin_dir = os.path.abspath("bin")
    os.makedirs(bin_dir, exist_ok=True)

    if platform == "windows":
        asset = "opencode-windows-x64.zip"
        binary_name = "opencode.exe"
    else:
        asset = "opencode-linux-x64.tar.gz"
        binary_name = "opencode"
    url = f"https://github.com/{GITHUB_REPO}/releases/download/{OPENCODE_VERSION}/{asset}"
    dest_path = os.path.join(bin_dir, binary_name)

    with tempfile.NamedTemporaryFile(delete=False, suffix=asset[asset.index("."):]) as temp_file:
        temp_path = temp_file.name
    try:
        download_file(url, temp_path)
        print(f"Verifying SHA-256 for {asset}...")
        verify_archive_checksum(temp_path, asset, OPENCODE_VERSION)
        print(f"Extracting and executing {binary_name} validation...")
        installed = _extract_binary(temp_path, bin_dir, OPENCODE_VERSION)
        if installed != dest_path:
            raise RuntimeError(f"OpenCode was installed at unexpected path {installed}")
        print(f"opencode {OPENCODE_VERSION} bundled at {dest_path}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def setup_engraphis():
    """Require the exact Engraphis version locked by Poetry.

    Deliberately installed WITHOUT extras: the [server]/[mcp] extras drag in
    sentence-transformers -> torch (gigabytes) and an `mcp` pin this codebase
    avoids on purpose (see api/mcp_server.py). The core is numpy-only and the
    dashboard reuses the fastapi/uvicorn already bundled; recall runs fully
    offline on Engraphis's deterministic embedder.
    """
    import importlib.metadata
    from api.engraphis_version import ENGRAPHIS_VERSION

    try:
        version = importlib.metadata.version("engraphis")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            "Engraphis is missing from the build environment; run the locked "
            "Poetry install before preparing assets."
        ) from exc
    if version != ENGRAPHIS_VERSION:
        raise RuntimeError(
            f"Engraphis {version} is installed, but the build requires "
            f"exactly {ENGRAPHIS_VERSION}."
        )
    print(f"engraphis {version} matches the locked build contract.")


def setup_tiktoken_cache():
    print("Preparing offline tiktoken cache...")
    cache_dir = os.path.abspath("tiktoken_cache")
    os.makedirs(cache_dir, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = cache_dir
    
    try:
        import tiktoken
        # Trigger download and caching
        tiktoken.get_encoding("cl100k_base")
        print(f"Tiktoken cache successfully prepared at: {cache_dir}")
        print("Cached encodings:")
        for item in os.listdir(cache_dir):
            print(f" - {item}")
    except ImportError:
        print("Error: tiktoken is not installed in the current Python environment.")
        print("Please install dependencies (poetry install / pip install tiktoken) first.")
        sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/prepare_assets.py [windows|linux]")
        sys.exit(1)
        
    platform = sys.argv[1].lower()
    if platform not in ["windows", "linux"]:
        print(f"Error: Unsupported platform '{platform}'. Must be 'windows' or 'linux'.")
        sys.exit(1)
        
    print(f"Preparing build assets for platform: {platform}...")
    
    # 1. Copy Next.js frontend assets to the standalone directory
    copy_dir("public", os.path.join(".next", "standalone", "public"))
    copy_dir(
        os.path.join(".next", "static"), 
        os.path.join(".next", "standalone", ".next", "static")
    )
    
    # 2. Setup Node.js binary
    setup_node_binary(platform)

    # 2.5. Bundle the opencode coding agent (Code Editing mode)
    setup_opencode_binary(platform)

    # 2.7. Verify the locked Engraphis memory engine
    setup_engraphis()

    # 3. Setup tiktoken cache
    setup_tiktoken_cache()
    
    print("Build asset preparation completed successfully.")

if __name__ == "__main__":
    main()
