"""Persistent user preferences: home location, favourite stores, search radius."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_RADIUS_KM = 25.0


def default_path() -> Path:
    override = os.environ.get("KAUFDA_MCP_PREFS")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "kaufda-mcp" / "preferences.json"


class PreferenceStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def stores(self) -> list[str]:
        return list(self.load().get("stores") or [])

    def radius_km(self) -> float:
        value = self.load().get("max_distance_km")
        return float(value) if value is not None else DEFAULT_RADIUS_KM
