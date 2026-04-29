"""
utils/logger.py
Centralised logging configuration for SDM.
Call setup_logging() once from main.py before importing any other module.
All other modules obtain their logger via: logging.getLogger(__name__)
"""

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging(log_level: str = "INFO", log_file: str = "sdm.log") -> None:
    """
    Configure the root logger with:
      - A RotatingFileHandler writing to `log_file` (5 MB max, 3 backups)
      - A StreamHandler writing to stderr

    Both handlers use the same level. The root logger is set to DEBUG so
    child loggers inherit the configured level correctly.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # handlers control actual filtering

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — never fills disk, survives long runs
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(fmt)

    # Console handler — useful during development and CLI mode
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(fmt)

    # Avoid duplicate handlers if setup_logging is called more than once
    if root_logger.handlers:
        root_logger.handlers.clear()

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger(__name__).debug("Logging initialised at level %s", log_level)
