import os
import sys
import subprocess
import socket
import threading
import time
import webbrowser
import shutil
from pathlib import Path

# BASE_DIR is sys._MEIPASS if compiled with PyInstaller, else the project root directory
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add BASE_DIR to Python path to ensure 'api' module can be imported
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

def get_portable_base_dir() -> str:
    """Return the directory the user launched the app from — the folder that contains
    the .AppImage / .exe — so a portable ``DATABASE`` folder can live next to the
    executable and travel with it when the whole folder is copied/zipped.

    Resolution order:
      1. AppImage: the ``APPIMAGE`` env var holds the absolute path of the .AppImage
         file the user actually ran. ``sys.executable`` inside an AppImage points into
         the read-only squashfs mount (``/tmp/.mount_...``), which we must NOT use.
      2. Frozen PyInstaller build (Windows .exe / onefile): ``sys.executable`` is the
         launcher executable itself, so its directory is the install folder.
      3. Development mode: the project root (parent of this scripts/ dir).
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return os.path.dirname(os.path.abspath(appimage))
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False

def find_free_port(start_port):
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1

def start_node_server(node_bin, frontend_port, backend_port):
    print(f"Starting Next.js frontend on port {frontend_port}...")
    
    server_js = os.path.join(BASE_DIR, "server.js")
    if not os.path.exists(server_js):
        # In non-compiled development mode, check if we need to warn or find it
        dev_server_js = os.path.join(BASE_DIR, ".next", "standalone", "server.js")
        if os.path.exists(dev_server_js):
            server_js = dev_server_js
        else:
            print(f"Error: server.js not found (expected at {server_js} or {dev_server_js})")
            print("Please build the frontend first using 'npm run build' or 'yarn build'")
            return None
            
    node_cwd = os.path.dirname(server_js)
    
    env = os.environ.copy()
    env["PORT"] = str(frontend_port)
    env["HOSTNAME"] = "127.0.0.1"
    env["NODE_ENV"] = "production"
    env["SERVER_BASE_URL"] = f"http://127.0.0.1:{backend_port}"
    env["PYTHON_BACKEND_HOST"] = f"http://127.0.0.1:{backend_port}"
    
    # Hide terminal window on Windows if launched without a console
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
    proc = subprocess.Popen(
        [node_bin, server_js],
        cwd=node_cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        startupinfo=startupinfo
    )
    
    # Logging helper threads
    def log_stream(stream, prefix):
        for line in stream:
            print(f"[{prefix}] {line.strip()}")
            
    threading.Thread(target=log_stream, args=(proc.stdout, "Next.js"), daemon=True).start()
    threading.Thread(target=log_stream, args=(proc.stderr, "Next.js Error"), daemon=True).start()
    
    return proc

def setup_persistent_config_and_logs(args):
    home_dir = os.path.expanduser("~")

    # Portable DATABASE folder: live next to the .AppImage / .exe so the entire
    # install (executable + DATABASE) is self-contained and can be zipped/moved as
    # one unit. Holds config, logs, wiki cache, cloned repos, embeddings, and
    # adalflow's internal caches/dbs. Falls back to ~/.hackdeepwiki if the folder
    # next to the executable is not writable (e.g. read-only location).
    database_dir = os.path.join(get_portable_base_dir(), "DATABASE")
    use_portable = _is_writable_dir(database_dir)
    if use_portable:
        config_dir = os.path.join(database_dir, "config")
        logs_dir = os.path.join(database_dir, "logs")
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        # Point the app's data root (wikicache/repos/faiss) at DATABASE. The app's
        # get_data_root() honors HACKDEEPWIKI_DATA_DIR first and verifies writability.
        os.environ["HACKDEEPWIKI_DATA_DIR"] = database_dir
        # Migrate wikis produced by older (non-portable) builds into DATABASE so they
        # are still detected. adalflow's hardcoded ~/.adalflow root is redirected to
        # DATABASE by api.data_root at import time, so no patch is needed here.
        try:
            from api.data_root import migrate_legacy_wikicache
            migrate_legacy_wikicache(database_dir)
        except Exception as e:
            print(f"Warning: legacy wiki cache migration skipped: {e}")
        print(f"Portable DATABASE directory: {database_dir}")
    else:
        hackdeepwiki_dir = os.path.join(home_dir, ".hackdeepwiki")
        config_dir = os.path.join(hackdeepwiki_dir, "config")
        logs_dir = os.path.join(hackdeepwiki_dir, "logs")
        os.makedirs(config_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        print(f"Warning: portable DATABASE dir '{database_dir}' is not writable; "
              f"falling back to {config_dir}")

    # Set config environment variables
    os.environ["HACKDEEPWIKI_CONFIG_DIR"] = config_dir
    os.environ["LOG_FILE_PATH"] = os.path.join(logs_dir, "application.log")

    # Set TIKTOKEN_CACHE_DIR to the bundled cache if it exists in BASE_DIR
    bundled_tiktoken_cache = os.path.join(BASE_DIR, "tiktoken_cache")
    if os.path.exists(bundled_tiktoken_cache):
        os.environ["TIKTOKEN_CACHE_DIR"] = bundled_tiktoken_cache
        
    # Load environment variables from args
    if args.github_token:
        os.environ["GITHUB_TOKEN"] = args.github_token
    if args.embed_batch_size:
        os.environ["OLLAMA_EMBED_BATCH_SIZE"] = str(args.embed_batch_size)
    if args.ollama_timeout:
        os.environ["OLLAMA_REQUEST_TIMEOUT"] = str(args.ollama_timeout)

    default_config_src = os.path.join(BASE_DIR, "api", "config")
    
    # Try dynamic Ollama discovery first
    # Note: hackdeepwiki_config.render() raises SystemExit (not Exception) on
    # connection errors, so we catch BaseException to handle both gracefully.
    try:
        # Try both import paths: bundled (scripts package) and dev mode (direct)
        try:
            from scripts.hackdeepwiki_config import render as _render
        except ImportError:
            import importlib.util
            _spec_path = os.path.join(BASE_DIR, "scripts", "hackdeepwiki_config.py")
            _spec = importlib.util.spec_from_file_location("hackdeepwiki_config", _spec_path)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _render = _mod.render
        render = _render
        print(f"Checking Ollama status at {args.ollama_endpoint}...")
        model, embed_model = render(
            Path(default_config_src),
            Path(config_dir),
            args.ollama_endpoint,
            args.model,
            args.embed_model
        )
        print(f"Successfully configured Ollama: default model '{model}', embedding model '{embed_model}'")
        os.environ["HACKDEEPWIKI_EMBEDDER_TYPE"] = "ollama"
        os.environ["OLLAMA_HOST"] = args.ollama_endpoint
        if model:
            os.environ["OLLAMA_MODEL"] = model
        if embed_model:
            os.environ["OLLAMA_EMBED_MODEL"] = embed_model
    except (Exception, SystemExit) as e:
        print(f"Ollama auto-configuration skipped / not available: {e}")
        print("Falling back to default configuration templates.")
        if os.path.exists(default_config_src):
            for item in os.listdir(default_config_src):
                src_file = os.path.join(default_config_src, item)
                dest_file = os.path.join(config_dir, item)
                if os.path.isfile(src_file) and not os.path.exists(dest_file):
                    print(f"Copying default config: {item} -> {config_dir}")
                    shutil.copy2(src_file, dest_file)

        
    print(f"Persistent config directory: {config_dir}")
    print(f"Persistent log file: {os.environ['LOG_FILE_PATH']}")

def run_fastapi_server(backend_port):
    print(f"Starting FastAPI backend on port {backend_port}...")
    os.environ["PORT"] = str(backend_port)
    os.environ["NODE_ENV"] = "production"
    
    import uvicorn
    # Import the app inside the thread to make sure config environment variables are set
    from api.api import app
    import google.generativeai as genai
    from api.config import GOOGLE_API_KEY
    
    if GOOGLE_API_KEY:
        genai.configure(api_key=GOOGLE_API_KEY)
        
    uvicorn.run(app, host="127.0.0.1", port=backend_port)

def is_port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

def main():
    import argparse
    parser = argparse.ArgumentParser(description="HackDeepWiki Standalone Runner")
    parser.add_argument("--ollama-endpoint", default=os.environ.get("OLLAMA_ENDPOINT", "http://127.0.0.1:11434"),
                        help="Ollama API endpoint URL")
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", ""),
                        help="Ollama completion model to use")
    parser.add_argument("--embed-model", dest="embed_model", default=os.environ.get("OLLAMA_EMBED_MODEL", ""),
                        help="Ollama embedding model to use")
    parser.add_argument("--embed-batch-size", dest="embed_batch_size", type=int,
                        default=int(os.environ.get("OLLAMA_EMBED_BATCH_SIZE", "32")),
                        help="Batch size for Ollama embeddings")
    parser.add_argument("--ollama-timeout", dest="ollama_timeout", type=int,
                        default=int(os.environ.get("OLLAMA_REQUEST_TIMEOUT", "1800")),
                        help="Timeout (seconds) for Ollama requests")
    parser.add_argument("--github-token", dest="github_token", default=os.environ.get("GITHUB_TOKEN", ""),
                        help="GitHub personal access token")
    parser.add_argument("--api-port", dest="api_port", type=int, default=None,
                        help="Backend API port (default: auto-detect from 8001)")
    parser.add_argument("--port", type=int, default=None,
                        help="Frontend port (default: auto-detect from 3000)")
    
    args, unknown = parser.parse_known_args()
    
    print("=" * 60)
    print("                HACKDEEPWIKI STANDALONE RUNNER")
    print("=" * 60)
    
    # Initialize dirs & paths
    setup_persistent_config_and_logs(args)
    
    # Find free ports
    backend_port = args.api_port if args.api_port is not None else int(os.environ.get("HACKDEEPWIKI_API_PORT", find_free_port(8001)))
    frontend_port = args.port if args.port is not None else int(os.environ.get("PORT", find_free_port(3000)))
    
    # Locate Node.js executable
    node_name = "node.exe" if sys.platform == "win32" else "node"
    node_bin = os.path.join(BASE_DIR, "bin", node_name)
    
    if not os.path.exists(node_bin):
        # Fall back to system Node.js
        node_bin = shutil.which(node_name)
        if not node_bin:
            print("Error: Node.js executable not found. Please install Node.js or bundle it with this package.")
            sys.exit(1)
            
    print(f"Using Node.js: {node_bin}")
    
    # Start Node.js Next.js Server
    node_process = start_node_server(node_bin, frontend_port, backend_port)
    if not node_process:
        print("Failed to start Next.js frontend.")
        sys.exit(1)
        
    # Start FastAPI Python backend thread
    backend_thread = threading.Thread(
        target=run_fastapi_server, 
        args=(backend_port,), 
        daemon=True
    )
    backend_thread.start()
    
    # Wait for both services to be active
    print("Initializing servers...")
    retries = 40
    servers_started = False
    while retries > 0:
        if is_port_open(frontend_port) and is_port_open(backend_port):
            servers_started = True
            break
        time.sleep(0.5)
        retries -= 1
        
    if not servers_started:
        print("Error: Servers failed to start within the timeout period.")
        node_process.terminate()
        sys.exit(1)
        
    # Open default browser
    url = f"http://127.0.0.1:{frontend_port}"
    print(f"Opening browser at: {url}")
    webbrowser.open(url)
    
    print("\nHackDeepWiki is running successfully!")
    print("Press Ctrl+C in this terminal window to stop the application.")
    print("=" * 60)
    
    # Keep the main thread running and monitor processes
    try:
        while True:
            if node_process.poll() is not None:
                print("Next.js server exited unexpectedly.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping HackDeepWiki...")
    finally:
        if node_process:
            node_process.terminate()
            try:
                node_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                node_process.kill()
        print("Goodbye!")

if __name__ == "__main__":
    main()
