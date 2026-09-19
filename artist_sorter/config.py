from __future__ import annotations

import json
import sys
from pathlib import Path


def app_dir() -> Path:
    """Return the portable data directory: the folder containing the EXE."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    return app_dir() / "config.json"


def managed_db_path() -> Path:
    return app_dir() / "rawdata-korean.db"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(data: dict) -> None:
    config_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
