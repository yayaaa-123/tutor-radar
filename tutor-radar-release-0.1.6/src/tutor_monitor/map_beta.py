from dataclasses import replace
import json
from pathlib import Path
import re

from .atomic_io import atomic_write_json
from .maps import MapSettings


from .runtime_paths import PROJECT_ROOT

BETA_MAP_FILE = PROJECT_ROOT / "data/private/amap-beta.json"
FIELDS = ("web_key", "js_key", "security_code")


def validate_map_beta(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != set(FIELDS):
        raise ValueError("invalid_map_config")
    credentials = {}
    for field in FIELDS:
        value = payload[field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value.strip()):
            raise ValueError("invalid_map_config")
        credentials[field] = value.strip()
    return credentials


def read_map_beta(path: Path = BETA_MAP_FILE) -> dict[str, str] | None:
    try:
        return validate_map_beta(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return None


def save_map_beta(credentials: dict[str, str], path: Path = BETA_MAP_FILE) -> None:
    atomic_write_json(path, validate_map_beta(credentials))


def apply_map_beta(settings: MapSettings) -> MapSettings:
    credentials = read_map_beta()
    return replace(settings, enabled=True, imported_web_key=credentials["web_key"]) if credentials else settings
