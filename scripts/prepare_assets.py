import os
import sys
import shutil
import urllib.request
import tarfile
import tempfile

def copy_dir(src, dest):
    if not os.path.exists(src):
        print(f"Warning: Source directory {src} does not exist. Skipping.")
        return
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f"Copied {src} -> {dest}")

def download_file(url, dest_path):
    print(f"Downloading {url} to {dest_path}...")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    # Custom headers to prevent potential request blocks
    req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as out_file:
        shutil.copyfileobj(response, out_file)
    print("Download finished.")

def setup_node_binary(platform):
    bin_dir = os.path.abspath("bin")
    os.makedirs(bin_dir, exist_ok=True)
    
    if platform == "windows":
        node_url = "https://nodejs.org/dist/v24.18.0/win-x64/node.exe"
        dest_path = os.path.join(bin_dir, "node.exe")
        download_file(node_url, dest_path)
    elif platform == "linux":
        node_url = "https://nodejs.org/dist/v24.18.0/node-v24.18.0-linux-x64.tar.xz"
        
        # Download tarball to a temp file
        with tempfile.NamedTemporaryFile(delete=False) as temp_tar:
            temp_tar_path = temp_tar.name
            
        try:
            download_file(node_url, temp_tar_path)
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
                    node_member.name = "node" # Rename to node
                    tar.extract(node_member, path=bin_dir)
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
    
    # 3. Setup tiktoken cache
    setup_tiktoken_cache()
    
    print("Build asset preparation completed successfully.")

if __name__ == "__main__":
    main()
