import os
import sys
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from api.logging_config import setup_logging

# Configure logging
setup_logging()
logger = logging.getLogger(__name__)

# Configure watchfiles logger to show file paths
watchfiles_logger = logging.getLogger("watchfiles.main")
watchfiles_logger.setLevel(logging.DEBUG)  # Enable DEBUG to see file paths

# Add the current directory to the path so we can import the api package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Apply watchfiles monkey patch BEFORE uvicorn import
is_development = os.environ.get("NODE_ENV") != "production"
if is_development:
    import watchfiles
    current_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(current_dir, "logs")
    
    original_watch = watchfiles.watch
    def patched_watch(*args, **kwargs):
        # Only watch the api directory but exclude logs subdirectory
        # Instead of watching the entire api directory, watch specific subdirectories
        api_subdirs = []
        for item in os.listdir(current_dir):
            item_path = os.path.join(current_dir, item)
            if os.path.isdir(item_path) and item != "logs":
                api_subdirs.append(item_path)
            elif os.path.isfile(item_path) and item.endswith(".py"):
                api_subdirs.append(item_path)
        
        return original_watch(*api_subdirs, **kwargs)
    watchfiles.watch = patched_watch

import uvicorn

# Warn about missing cloud provider keys ONLY when the configured default
# provider actually needs them. The local-first, zero-API-key path (Ollama)
# is the documented default, so warning "GOOGLE_API_KEY/OPENAI_API_KEY missing"
# on every startup of a pure-Ollama install is noise that implies setup is
# broken when it isn't.
try:
    from api.config import get_model_config, configs
    _default_provider = configs.get("generator_config", {}).get("default_provider", "ollama")
    _provider_needs_keys = {
        "openai": ["OPENAI_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
        "claude": [],  # api key optional (subscription token), warned at request time
        "google": ["GOOGLE_API_KEY"],
        "azure": ["AZURE_API_KEY"],
        "bedrock": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
        "dashscope": ["DASHSCOPE_API_KEY"],
        "litellm": ["LITELLM_API_KEY"],
    }
    _needed = _provider_needs_keys.get(_default_provider, [])
    _missing = [v for v in _needed if not os.environ.get(v)]
    if _missing:
        logger.warning(
            f"Default provider '{_default_provider}' is missing: {', '.join(_missing)}. "
            "Some functionality may not work correctly without these variables."
        )
except Exception:  # noqa: BLE001 - never block startup on a warning
    pass

# Configure Google Generative AI
import google.generativeai as genai
from api.config import GOOGLE_API_KEY

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    logger.warning("GOOGLE_API_KEY not configured")

if __name__ == "__main__":
    # Get port from environment variable or use default
    port = int(os.environ.get("PORT", 8001))

    # Import the app here to ensure environment variables are set first
    from api.api import app

    logger.info(f"Starting Streaming API on port {port}")

    # Bind loopback by default. The app exposes unauthenticated wiki-cache,
    # vuln-cache and filesystem-listing endpoints; binding 0.0.0.0 by default
    # would expose those to the LAN. Set HACKDEEPWIKI_HOST=0.0.0.0 explicitly
    # (e.g. for a containerized/remote deploy) to override.
    host = os.environ.get("HACKDEEPWIKI_HOST", "127.0.0.1")

    # Run the FastAPI app with uvicorn
    uvicorn.run(
        "api.api:app",
        host=host,
        port=port,
        reload=is_development,
        reload_excludes=["**/logs/*", "**/__pycache__/*", "**/*.pyc"] if is_development else None,
    )
