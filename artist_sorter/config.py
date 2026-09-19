from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "ArtistSorter"


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


def managed_db_path() -> Path:
    return app_data_dir() / "rawdata-korean.db"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(data: dict) -> None:
    config_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
