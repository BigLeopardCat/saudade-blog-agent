"""Centralised logging configuration."""

import logging
import sys
from config import settings


def setup_logging() -> None:
    """Configure the root logger with consistent formatting and level."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(name)-24s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on reload
    if not root.handlers:
        root.addHandler(handler)
    else:
        root.handlers = [handler]
