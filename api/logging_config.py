import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path


class IgnoreLogChangeDetectedFilter(logging.Filter):
    def filter(self, record: logging.LogRecord):
        return "Detected file change in" not in record.getMessage()


class IgnoreEngraphisCloudWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord):
        # We explicitly disable managed cloud; this warning is expected but noisy.
        return "managed cloud operation failed" not in record.getMessage()


def setup_logging(format: str = None):
    """
    Configure logging for the application with log rotation.

    Environment variables:
        LOG_LEVEL: Log level (default: INFO)
        LOG_FILE_PATH: Path to log file (default: <data root>/logs/application.log)
        LOG_MAX_SIZE: Max size in MB before rotating (default: 10MB)
        LOG_BACKUP_COUNT: Number of backup files to keep (default: 5)

    Ensures log directory exists, prevents path traversal, and configures
    both rotating file and console handlers.
    """
    # Determine log directory and default file path
    try:
        from api.data_root import get_data_root
        log_dir = Path(get_data_root()) / "logs"
    except Exception:
        log_dir = Path(tempfile.gettempdir()) / "hackdeepwiki-logs"
    default_log_file = log_dir / "application.log"

    # Get log level from environment
    log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Get log file path
    log_file_path = Path(os.environ.get("LOG_FILE_PATH", str(default_log_file)))

    # Secure path check: must be inside logs/ directory (bypassed if
    # LOG_FILE_PATH is explicitly set in environment).
    log_dir_resolved = log_dir.resolve()
    resolved_path = log_file_path.resolve()
    if "LOG_FILE_PATH" not in os.environ and not str(resolved_path).startswith(str(log_dir_resolved) + os.sep):
        raise ValueError(f"LOG_FILE_PATH '{log_file_path}' is outside the trusted log directory '{log_dir_resolved}'")

    # Get max log file size (default: 10MB)
    try:
        max_mb = int(os.environ.get("LOG_MAX_SIZE", 10))  # 10MB default
        max_bytes = max_mb * 1024 * 1024
    except (TypeError, ValueError):
        max_bytes = 10 * 1024 * 1024  # fallback to 10MB on error

    # Get backup count (default: 5)
    try:
        backup_count = int(os.environ.get("LOG_BACKUP_COUNT", 5))
    except ValueError:
        backup_count = 5

    # Configure format
    log_format = format or "%(asctime)s - %(levelname)s - %(name)s - %(filename)s:%(lineno)d - %(message)s"

    # Create handlers
    console_handler = logging.StreamHandler()
    handlers: list[logging.Handler] = [console_handler]
    try:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            resolved_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handlers.insert(0, file_handler)
    except OSError as exc:
        # Logging must never make the API unstartable. This also recovers
        # cleanly from older Docker/root runs that left api/logs owned by a
        # different user.
        file_handler = None
        print(
            f"Warning: cannot open log file '{resolved_path}': {exc}. "
            "Continuing with console logging.",
            file=os.sys.stderr,
        )

    # Set format for both handlers
    formatter = logging.Formatter(log_format)
    if file_handler is not None:
        file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Add filter to suppress "Detected file change" messages
    if file_handler is not None:
        file_handler.addFilter(IgnoreLogChangeDetectedFilter())
        file_handler.addFilter(IgnoreEngraphisCloudWarningFilter())
    console_handler.addFilter(IgnoreLogChangeDetectedFilter())
    console_handler.addFilter(IgnoreEngraphisCloudWarningFilter())

    # Apply logging configuration
    logging.basicConfig(level=log_level, handlers=handlers, force=True)

    # Log configuration info
    logger = logging.getLogger(__name__)
    logger.debug(
        f"Logging configured: level={log_level_str}, "
        f"file={resolved_path}, max_size={max_bytes} bytes, "
        f"backup_count={backup_count}"
    )
