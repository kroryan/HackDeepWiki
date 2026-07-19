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
    freedeepwiki_dir = os.path.join(home_dir, ".freedeepwiki")
    config_dir = os.path.join(freedeepwiki_dir, "config")
    logs_dir = os.path.join(freedeepwiki_dir, "logs")
    
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    
    # Set config environment variables
    os.environ["FREEDEPWIKI_CONFIG_DIR"] = config_dir
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
    # Note: freedeepwiki_config.render() raises SystemExit (not Exception) on
    # connection errors, so we catch BaseException to handle both gracefully.
    try:
        # Try both import paths: bundled (scripts package) and dev mode (direct)
        try:
            from scripts.freedeepwiki_config import render as _render
        except ImportError:
            import importlib.util
            _spec_path = os.path.join(BASE_DIR, "scripts", "freedeepwiki_config.py")
            _spec = importlib.util.spec_from_file_location("freedeepwiki_config", _spec_path)
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
        os.environ["FREEDEPWIKI_EMBEDDER_TYPE"] = "ollama"
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
    parser = argparse.ArgumentParser(description="FreeDeepWiki Standalone Runner")
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
    print("                FREEDEPWIKI STANDALONE RUNNER")
    print("=" * 60)
    
    # Initialize dirs & paths
    setup_persistent_config_and_logs(args)
    
    # Find free ports
    backend_port = args.api_port if args.api_port is not None else int(os.environ.get("FREEDEPWIKI_API_PORT", find_free_port(8001)))
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
    
    print("\nFreeDeepWiki is running successfully!")
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
        print("\nStopping FreeDeepWiki...")
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
