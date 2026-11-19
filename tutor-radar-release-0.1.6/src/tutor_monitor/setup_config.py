import json
from pathlib import Path
import tomllib

from .atomic_io import atomic_write_text, atomic_write_json
from .parser import SUBJECTS


def validate_profile(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid_profile")
    gender = payload.get("gender")
    subjects = payload.get("subjects")
    weekend = payload.get("weekend_only")
    if gender not in ("male", "female") or not isinstance(weekend, bool):
        raise ValueError("invalid_profile")
    if not isinstance(subjects, list) or not subjects or len(subjects) > len(SUBJECTS):
        raise ValueError("invalid_subjects")
    if any(subject not in SUBJECTS for subject in subjects):
        raise ValueError("invalid_subjects")

    result = {"gender": gender, "subjects": list(dict.fromkeys(subjects)), "weekend_only": weekend}
    for name in ("school", "year", "education", "origin", "db_dir"):
        raw = payload.get(name, "")
        if not isinstance(raw, str) or len(raw) > 512 or (name in {"education", "origin"} and not raw.strip()):
            raise ValueError("invalid_profile")
        result[name] = raw.strip()
    return result


def read_profile_override(path: Path) -> dict | None:
    try:
        return validate_profile(json.loads(path.with_name("profile.local.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def current_profile(path: Path) -> dict | None:
    try:
        with path.open("rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        return None
    override = read_profile_override(path)
    if override:
        return override
    profile = config.get("profile", {})
    capability = config.get("scoring", {}).get("capability", {})
    return {
        **{key: profile.get(key, "") for key in ("gender", "school", "year")},
        "subjects": profile.get("subjects", []),
        "weekend_only": profile.get("weekend_only", False),
        "education": capability.get("education", ""),
        "origin": config.get("map", {}).get("origin_address", ""),
    }


def save_profile(path: Path, payload: object) -> None:
    profile = validate_profile(payload)
    if path.is_file():
        profile.pop("db_dir", None)
        atomic_write_json(path.with_name("profile.local.json"), profile)
        return

    def value(name: str) -> str:
        return json.dumps(profile[name], ensure_ascii=False)

    config = f"""
[wechat]
db_dir = {value("db_dir")}
groups = []
poll_interval_seconds = 5.0
backfill_messages = 50

[profile]
gender = {value("gender")}
school = {value("school")}
year = {value("year")}
subjects = {value("subjects")}
preferred_subjects = []
weekend_only = {value("weekend_only")}

[scoring.capability]
education = {value("education")}
strength_subjects = {value("subjects")}
preferred_subjects = []
experience = []
weekend_only = {value("weekend_only")}

[storage]
database = "data/private/monitor.db"
health_file = "data/private/health.json"
events_file = "data/private/orders.jsonl"

[map]
enabled = false
origin_address = {value("origin")}
origin_city = "深圳市"
request_interval_seconds = 1.0

[model]
enabled = false
"""
    tomllib.loads(config)
    atomic_write_text(path, config)
