import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(root: Path) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    level_name = os.getenv("PAGEINDEX_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    app_handler = RotatingFileHandler(log_dir / "server8765.out.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    app_handler.setLevel(level)
    app_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(log_dir / "server8765.err.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(app_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)
