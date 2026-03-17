"""
Reusable Logging Setup
Extracted from MiroFish-Offline — standalone, zero-dependency module.

Provides dual-output logging (file + console) with:
  - Rotating file handler (10MB, 5 backups)
  - Date-based log filenames
  - UTF-8 safety on Windows
  - Per-module loggers with function/line tracking

Usage:
    from tools.logger import setup_logger, get_logger

    # Once at startup:
    setup_logger("myapp")

    # Anywhere else:
    logger = get_logger("myapp.module")
    logger.info("Hello %s", "world")
"""

import os
import sys
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler


DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")


def _ensure_utf8_stdout():
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logger(
    name: str = "app",
    level: int = logging.DEBUG,
    log_dir: str = DEFAULT_LOG_DIR,
) -> logging.Logger:
    """
    Configure a logger with file + console handlers.

    Args:
        name:    Logger name (use dotted names for hierarchy).
        level:   Minimum log level for the file handler.
        log_dir: Directory for log files.
    """
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    detailed = logging.Formatter(
        "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    simple = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # File handler — detailed, rotating
    fh = RotatingFileHandler(
        os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d") + ".log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(detailed)

    # Console handler — concise, INFO+
    _ensure_utf8_stdout()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(simple)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def get_logger(name: str = "app") -> logging.Logger:
    """Get or create a named logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
