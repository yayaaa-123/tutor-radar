from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from dataclasses import replace
from tempfile import TemporaryDirectory
from threading import Lock
import time
import tomllib
from typing import Any
from urllib.parse import parse_qs, urlparse

from .maps import AmapCommuteEnricher, MapSettings
from .map_beta import apply_map_beta, read_map_beta, save_map_beta, validate_map_beta
from .identity import event_candidate_key, public_candidate_key, public_group_key
from .group_selection import SelectedGroup, load_selected_groups, save_selected_groups
from .probe import list_sessions
from .setup_config import current_profile, save_profile
from .model_setup import configured_directory, model_status, start_model_setup


from .runtime_paths import PROJECT_ROOT
HEALTH_FILE = PROJECT_ROOT / "data" / "private" / "health.json"
EVENTS_FILE = PROJECT_ROOT / "data" / "private" / "orders.jsonl"
PLAYBACK_FILE = PROJECT_ROOT / "data" / "private" / "recent-50-filtered.json"
LEGACY_SAVED_FILE = PROJECT_ROOT / "data" / "private" / "recent-20-filtered.json"
REJECTIONS_FILE = PROJECT_ROOT / "data" / "private" / "manual-rejections.json"
ENRICHMENTS_FILE = PROJECT_ROOT / "data" / "private" / "candidate-enrichments.json"
MODEL_RESULTS_FILE = PROJECT_ROOT / "data" / "private" / "model-results.json"
CONFIG_FILE = PROJECT_ROOT / "config.toml"
try:
    with CONFIG_FILE.open("rb") as _retention_config_file:
        ORDER_RETENTION_HOURS = max(
            1, int(tomllib.load(_retention_config_file).get("retention", {}).get("order_hours", 48))
        )
except (OSError, ValueError, tomllib.TOMLDecodeError):
    ORDER_RETENTION_HOURS = 48
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
}
REJECTION_LOCK = Lock()
GROUP_SELECTION_LOCK = Lock()
GROUP_DISCOVERY_CACHE: dict[str, SelectedGroup] = {}
AMAP_STYLE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
def _safe_group_name(chat_id: str, value: Any) -> str:
    name = str(value or "").strip()
    if not name or name == chat_id or "@chatroom" in name:
        return f"未命名微信群 · {public_group_key(chat_id)[:6]}"
    return name


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _read_private_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_map_client_config(config_file: Path = CONFIG_FILE) -> dict[str, Any]:
    try:
        with config_file.open("rb") as file:
            map_data = tomllib.load(file).get("map", {})
    except (OSError, tomllib.TOMLDecodeError):
        map_data = {}
    js_key_file = _project_path(str(map_data.get("js_key_file", "data/private/amap-js.key")))
    security_file = _project_path(
        str(map_data.get("js_security_code_file", "data/private/amap-js-security-code.txt"))
    )
    style_file = _project_path(
        str(map_data.get("js_style_id_file", "data/private/amap-js-style-id.txt"))
    )
    js_key = _read_private_text(js_key_file)
    security_code = _read_private_text(security_file)
    imported = read_map_beta()
    if imported:
        js_key = imported["js_key"]
        security_code = imported["security_code"]
    style_id = _read_private_text(style_file)
    bundled_basemap = os.environ.get("TUTOR_RADAR_BASEMAP")
    if bundled_basemap:
        try:
            bundled = json.loads(Path(bundled_basemap).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            bundled = None
        fields = ("js_key", "security_code", "style_id")
        if isinstance(bundled, dict) and all(
            isinstance(bundled.get(field), str) and AMAP_STYLE_ID_RE.fullmatch(bundled[field])
            for field in fields
        ):
            js_key, security_code, style_id = (bundled[field] for field in fields)
    if not AMAP_STYLE_ID_RE.fullmatch(style_id):
        style_id = ""
    configured = bool(js_key and security_code)
    return {
        "configured": configured,
        "provider": "amap",
        "js_key": js_key if configured else None,
        "security_code": security_code if configured else None,
        "style_id": style_id or None,
        "message": None if configured else "缺少高德 JS API Key 或安全密钥",
    }


def build_group_management_payload(
    config_file: Path = CONFIG_FILE,
    *,
    limit: int = 300,
) -> dict[str, Any]:
    from .service import load_config

    config = load_config(config_file)
    selected = load_selected_groups(config.group_selection_file, config.groups)
    discovered: list[dict[str, Any]] = []
    error: str | None = None
    try:
        discovered = list_sessions(True, max(30, min(limit, 500)), config.db_dir)
    except Exception:
        error = "暂时无法读取微信会话，请确认微信已登录后重试"

    by_id: dict[str, dict[str, Any]] = {
        group.chat_id: {
            "chat_id": group.chat_id,
            "chat_name": _safe_group_name(group.chat_id, group.chat_name),
            "monitored": True,
            "unread": None,
            "last_time": None,
        }
        for group in selected
    }
    for session in discovered:
        chat_id = str(session.get("chat_id") or "").strip()
        if not chat_id.endswith("@chatroom"):
            continue
        row = by_id.setdefault(
            chat_id,
            {
                "chat_id": chat_id,
                "chat_name": _safe_group_name(chat_id, session.get("name")),
                "monitored": False,
                "unread": session.get("unread"),
                "last_time": session.get("last_time"),
            },
        )
        row["chat_name"] = _safe_group_name(chat_id, session.get("name") or row["chat_name"])
        row["unread"] = session.get("unread")
        row["last_time"] = session.get("last_time")

    def session_time(item: dict[str, Any]) -> int:
        try:
            return int(item.get("last_time") or 0)
        except (TypeError, ValueError):
            return 0

    rows = list(by_id.values())
    rows.sort(key=lambda item: (not bool(item["monitored"]), -session_time(item)))
    with GROUP_SELECTION_LOCK:
        GROUP_DISCOVERY_CACHE.clear()
        GROUP_DISCOVERY_CACHE.update({
            public_group_key(row["chat_id"]): SelectedGroup(row["chat_id"], row["chat_name"])
            for row in rows
        })
    return {
        "groups": [
            {
                "group_key": public_group_key(row["chat_id"]),
                "chat_name": row["chat_name"],
                "monitored": bool(row["monitored"]),
                "unread": row["unread"],
                "last_time": row["last_time"],
            }
            for row in rows
        ],
        "error": error,
    }


def save_group_selection_by_keys(
    group_keys: list[str],
    config_file: Path = CONFIG_FILE,
) -> tuple[SelectedGroup, ...]:
    from .service import load_config

    if len(group_keys) > 30 or len(set(group_keys)) != len(group_keys):
        raise ValueError("群聊选择无效")
    config = load_config(config_file)
    current = load_selected_groups(config.group_selection_file, config.groups)
    current_by_key = {public_group_key(group.chat_id): group for group in current}
    with GROUP_SELECTION_LOCK:
        available = {**GROUP_DISCOVERY_CACHE, **current_by_key}
        try:
            selected = [available[key] for key in group_keys]
        except KeyError as exc:
            raise ValueError("群聊列表已过期，请刷新后重试") from exc
        return save_selected_groups(config.group_selection_file, selected)


def _safe_health(raw: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "status", "chat_name", "started_at", "checked_at", "last_message_at",
        "last_order_at", "messages_seen", "orders_extracted", "candidates_found",
        "orders_rejected", "parse_errors", "consecutive_errors", "error",
    )
    return {field: raw.get(field) for field in fields}


def _safe_recognition(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    accepted = raw.get("accepted_fields") if isinstance(raw.get("accepted_fields"), list) else []
    return {
        "role": raw.get("role"),
        "status": raw.get("status"),
        "accepted_fields": [
            {"name": item.get("name"), "value": item.get("value")}
            for item in accepted if isinstance(item, dict)
        ],
        "missing_fields": list(raw.get("missing_fields") or []),
    }


def _safe_candidate(event: dict[str, Any], source: str) -> dict[str, Any]:
    order = event.get("order") or {}
    decision = event.get("filter") or {}
    order_fields = (
        "order_id", "address", "district", "subjects", "subject_text", "student_text",
        "current_level", "requirements_text", "schedule_text", "frequency_per_week",
        "duration_hours", "pay_text", "hourly_rate_min", "hourly_rate_max",
        "lesson_pay_min", "lesson_pay_max", "trial_time", "raw_text",
    )
    location_reason_tokens = ("区域优先级", "区域不考虑", "区域未设偏好", "区域无法确定", "仅高薪考虑")
    reasons = [
        str(reason) for reason in (decision.get("reasons") or [])
        if not any(token in str(reason) for token in location_reason_tokens)
    ]
    subjects = set(order.get("subjects") or [])
    priority = sum(2 if subject in {"数学", "物理"} else 1 for subject in subjects & {"数学", "物理", "化学"})
    status = decision.get("status", "review")
    if status == "review" and not any(
        token in reason for reason in reasons for token in ("待确认", "需确认", "不明确")
    ):
        status = "matched"
    return {
        "captured_at": event.get("captured_at"),
        "source": source,
        "chat_id": public_group_key(event.get("chat_id")),
        "chat_name": str(event.get("chat_name") or "历史记录"),
        "candidate_key": public_candidate_key(
            str(event.get("chat_id") or ""), str(order.get("order_id") or "")
        ),
        "_storage_key": event_candidate_key(event),
        "_chat_id": str(event.get("chat_id") or ""),
        "order": {field: order.get(field) for field in order_fields},
        "filter": {
            "status": status,
            "priority": priority,
            "reasons": reasons,
        },
        "commute": event.get("commute"),
        "score": event.get("score"),
        "recognition": _safe_recognition(event.get("recognition")),
        "model_analysis": None,
    }


def _live_candidates(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "candidate":
            continue
        captured_at = str(event.get("captured_at") or "")
        if captured_at:
            try:
                if datetime.fromisoformat(captured_at).timestamp() < time.time() - ORDER_RETENTION_HOURS * 3600:
                    continue
            except ValueError:
                pass
        scoped_key = event_candidate_key(event)
        if not scoped_key or scoped_key in seen:
            continue
        seen.add(scoped_key)
        if str((event.get("filter") or {}).get("status") or "") == "rejected":
            continue
        output.append(_safe_candidate(event, "live"))
    return output


def _live_event_ids(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    output: set[str] = set()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "candidate":
            continue
        captured_at = str(event.get("captured_at") or "")
        if captured_at:
            try:
                if datetime.fromisoformat(captured_at).timestamp() < time.time() - ORDER_RETENTION_HOURS * 3600:
                    continue
            except ValueError:
                pass
        scoped_key = event_candidate_key(event)
        if scoped_key:
            output.add(scoped_key)
    return output


def _information_missing_ids(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    resolved: set[str] = set()
    missing: set[str] = set()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        captured_at = str(event.get("captured_at") or "")
        if captured_at:
            try:
                if datetime.fromisoformat(captured_at).timestamp() < time.time() - ORDER_RETENTION_HOURS * 3600:
                    continue
            except ValueError:
                pass
        scoped_key = event_candidate_key(event)
        if not scoped_key or scoped_key in resolved or scoped_key in missing:
            continue
        if event.get("event") == "information_missing":
            missing.add(scoped_key)
        elif event.get("event") == "candidate":
            resolved.add(scoped_key)
    return missing


def _set_manual_rejection(scoped_key: str, rejected: bool, path: Path = REJECTIONS_FILE) -> None:
    with REJECTION_LOCK:
        payload = _read_json(path, {"order_ids": []})
        candidate_keys = {str(value) for value in payload.get("candidate_keys") or []}
        if rejected:
            candidate_keys.add(scoped_key)
        else:
            candidate_keys.discard(scoped_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                "candidate_keys": sorted(candidate_keys),
                "order_ids": list(payload.get("order_ids") or []),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def build_dashboard_payload(
    health_file: Path = HEALTH_FILE,
    events_file: Path = EVENTS_FILE,
    playback_file: Path = PLAYBACK_FILE,
    saved_file: Path = LEGACY_SAVED_FILE,
    rejections_file: Path = REJECTIONS_FILE,
    enrichments_file: Path | None = None,
    model_results_file: Path | None = None,
    _include_private: bool = False,
) -> dict[str, Any]:
    raw_health = _read_json(health_file, {})
    health = _safe_health(raw_health)
    live = _live_candidates(events_file)
    # Compatibility snapshots have no trustworthy source timestamp. Including
    # them would violate the rolling 48-hour product boundary.
    playback = {"summary": {}, "candidates": []}
    history: list[dict[str, Any]] = []

    live_summary = {
        "messages": int(health.get("messages_seen") or 0),
        "orders": int(health.get("orders_extracted") or 0),
        "candidates": int(health.get("candidates_found") or 0),
        "rejected": int(health.get("orders_rejected") or 0),
    }
    replay_raw = playback.get("summary") or {}
    replay_summary = {
        "messages": int(replay_raw.get("messages") or 0),
        "orders": int(replay_raw.get("orders") or 0),
        "candidates": sum(item["source"] == "playback" for item in history),
        "rejected": int(replay_raw.get("rejected") or 0),
    }
    rejection_payload = _read_json(rejections_file, {"candidate_keys": [], "order_ids": []})
    rejected_keys = set(rejection_payload.get("candidate_keys") or [])
    legacy_rejected_ids = set(rejection_payload.get("order_ids") or [])
    candidates = live + history
    order_id_counts: dict[str, int] = {}
    for candidate in candidates:
        order_id = str(candidate["order"].get("order_id") or "")
        order_id_counts[order_id] = order_id_counts.get(order_id, 0) + 1

    def scoped_or_unambiguous_legacy(mapping: dict[str, Any], candidate: dict[str, Any]) -> Any:
        scoped_key = candidate["_storage_key"]
        if scoped_key in mapping:
            return mapping[scoped_key]
        order_id = str(candidate["order"].get("order_id") or "")
        return mapping.get(order_id) if order_id_counts.get(order_id) == 1 else None
    overlay_path = enrichments_file or events_file.parent / ENRICHMENTS_FILE.name
    overlays = (_read_json(overlay_path, {"candidates": {}}).get("candidates") or {})
    for candidate in candidates:
        overlay = scoped_or_unambiguous_legacy(overlays, candidate) or {}
        if overlay.get("commute") is not None:
            candidate["commute"] = overlay["commute"]
        if overlay.get("score") is not None:
            candidate["score"] = overlay["score"]
    model_path = model_results_file or events_file.parent / MODEL_RESULTS_FILE.name
    model_results = (_read_json(model_path, {"orders": {}}).get("orders") or {})
    for candidate in candidates:
        result = scoped_or_unambiguous_legacy(model_results, candidate) or {}
        if result.get("recognition") is not None:
            candidate["recognition"] = _safe_recognition(result["recognition"])
        if result.get("analysis") is not None:
            candidate["model_analysis"] = result["analysis"]
    visible_candidates = [
        item for item in candidates
        if item["candidate_key"] not in rejected_keys
        and not (
            order_id_counts.get(str(item["order"].get("order_id") or "")) == 1
            and str(item["order"].get("order_id") or "") in legacy_rejected_ids
        )
    ]
    rejected_candidates = [
        item for item in candidates
        if item["candidate_key"] in rejected_keys
        or (
            order_id_counts.get(str(item["order"].get("order_id") or "")) == 1
            and str(item["order"].get("order_id") or "") in legacy_rejected_ids
        )
    ]
    information_missing_ids = _information_missing_ids(events_file)
    group_rows: dict[str, dict[str, Any]] = {}
    for group in raw_health.get("groups") or []:
        chat_id = str(group.get("chat_id") or "")
        if chat_id:
            group_rows[chat_id] = dict(group)
    for candidate in candidates:
        chat_id = str(candidate.get("_chat_id") or "")
        if chat_id and chat_id not in group_rows:
            group_rows[chat_id] = {
                "chat_id": chat_id,
                "chat_name": candidate.get("chat_name") or "历史群聊",
                "monitored": False,
            }
    result = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "health": health,
        "live_summary": live_summary,
        "playback_summary": replay_summary,
        "candidates": visible_candidates,
        "rejected_candidates": rejected_candidates,
        "information_missing_count": sum(
            item["_storage_key"] in information_missing_ids
            for item in visible_candidates
        ),
        "groups": [
            {
                "chat_id": public_group_key(group.get("chat_id")),
                "chat_name": str(group.get("chat_name") or group.get("chat_id") or "未命名群"),
                "monitored": bool(group.get("monitored", True)),
                "status": (
                    "stopped" if not group.get("monitored", True)
                    else "degraded" if group.get("consecutive_errors")
                    else str(raw_health.get("status") or "running")
                ),
                "messages": int(group.get("messages_seen") or 0),
                "orders": int(group.get("orders_extracted") or 0),
                "candidates_found": int(group.get("candidates_found") or 0),
                "orders_rejected": int(group.get("orders_rejected") or 0),
                "candidates": sum(item["_chat_id"] == str(group.get("chat_id") or "") for item in visible_candidates),
                "rejected": sum(item["_chat_id"] == str(group.get("chat_id") or "") for item in rejected_candidates),
                "information_missing": sum(
                    item["_chat_id"] == str(group.get("chat_id") or "")
                    and item["_storage_key"] in information_missing_ids
                    for item in visible_candidates
                ),
            }
            for group in group_rows.values()
            if group.get("chat_id") or group.get("chat_name")
        ],
    }
    if not _include_private:
        for candidate in visible_candidates + rejected_candidates:
            candidate.pop("_storage_key", None)
            candidate.pop("_chat_id", None)
    return result


def build_candidate_route(
    scoped_key: str,
    map_settings: MapSettings | None = None,
    dashboard_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dashboard_payload or build_dashboard_payload()
    candidate = next(
        (
            item
            for item in payload.get("candidates") or []
            if str(item.get("candidate_key") or "") == scoped_key
            or (not item.get("chat_id") and str((item.get("order") or {}).get("order_id") or "") == scoped_key)
        ),
        None,
    )
    if candidate is None:
        return {"status": "not_found", "error": "candidate_not_found"}
    address = str((candidate.get("order") or {}).get("address") or "").strip()
    if map_settings is None:
        from .service import load_config

        map_settings = load_config(CONFIG_FILE).map_settings
    enricher = AmapCommuteEnricher(apply_map_beta(map_settings))
    try:
        result = enricher.route_detail(address)
    finally:
        enricher.close()
    return {"candidate_key": scoped_key, "order_id": candidate["order"]["order_id"], **result}


def import_map_beta(payload: object) -> dict[str, Any]:
    from .service import load_config

    credentials = validate_map_beta(payload)
    settings = load_config(CONFIG_FILE).map_settings
    with TemporaryDirectory() as directory:
        enricher = AmapCommuteEnricher(replace(
            settings, enabled=True, imported_web_key=credentials["web_key"],
            cache_db=Path(directory) / "map-test.db", error_log=None, request_attempts=1,
            request_interval_seconds=max(1.0, settings.request_interval_seconds),
            origin_city="深圳市",
        ))
        try:
            route = enricher.enrich_between("深圳市民中心", "深圳大剧院")
        finally:
            enricher.close()
    if route.get("status") != "ok":
        return {"ok": False, "error": "transit_check_failed"}
    save_map_beta(credentials)
    return {"ok": True, "transit_minutes": route["route"]["duration_minutes"]}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TutorDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/setup":
            self._json({"configured": CONFIG_FILE.is_file(), "profile": current_profile(CONFIG_FILE)})
        elif path == "/api/setup/model":
            self._json(model_status())
        elif path == "/api/health":
            self._json({"health": build_dashboard_payload()["health"]})
        elif path in {"/api/candidates", "/api/dashboard"}:
            self._json(build_dashboard_payload())
        elif path == "/api/map/config":
            self._json(build_map_client_config())
        elif path == "/api/groups/available":
            try:
                requested_limit = int((parse_qs(parsed.query).get("limit") or ["300"])[0])
                self._json(build_group_management_payload(limit=requested_limit))
            except (TypeError, ValueError):
                self._json({"error": "invalid group query"}, status=400)
        elif path == "/api/route":
            scoped_key = str((parse_qs(parsed.query).get("candidate_key") or [""])[0]).strip()
            if not scoped_key or len(scoped_key) > 500:
                self._json({"status": "invalid", "error": "invalid_candidate_key"}, status=400)
                return
            result = build_candidate_route(scoped_key)
            self._json(result, status=404 if result.get("status") == "not_found" else 200)
        else:
            self._json({"error": "not found"}, status=404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in {"/api/candidates/reject", "/api/groups/selection", "/api/map/import-beta", "/api/setup", "/api/setup/model"}:
            self._json({"error": "not found"}, status=404)
            return
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            self._json({"error": "origin not allowed"}, status=403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 1 or length > 65536:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/setup/model":
                action = body.get("action", "check") if isinstance(body, dict) else None
                if action == "install":
                    os.startfile("https://ollama.com/download/windows")
                    self._json({"ok": True})
                    return
                if action == "browse":
                    script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object Windows.Forms.FolderBrowserDialog; if ($d.ShowDialog() -eq 'OK') { [Console]::OutputEncoding=[Text.Encoding]::UTF8; [Console]::Write($d.SelectedPath) }; $d.Dispose()"
                    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                                            capture_output=True, encoding="utf-8", timeout=120,
                                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
                    self._json({"directory": result.stdout.strip()})
                    return
                if action not in ("check", "confirm_download"):
                    self._json({"error": "invalid_action"}, status=400)
                    return
                directory = body.get("directory")
                if not isinstance(directory, str):
                    raise ValueError("invalid_directory")
                started = start_model_setup(directory, download=action == "confirm_download")
                self._json({"ok": started, "directory": model_status()["directory"]}, status=202 if started else 409)
                return
            if path == "/api/setup":
                save_profile(CONFIG_FILE, body)
                self._json({"ok": True})
                return
            if path == "/api/map/import-beta":
                try:
                    result = import_map_beta(body)
                except (OSError, ValueError):
                    self._json({"ok": False, "error": "invalid_or_unavailable_map_config"}, status=400)
                    return
                self._json(result, status=200 if result["ok"] else 422)
                return
            if path == "/api/groups/selection":
                group_keys = body.get("group_keys")
                if not isinstance(group_keys, list) or any(
                    not isinstance(value, str) or not value or len(value) > 100
                    for value in group_keys
                ):
                    raise ValueError("invalid groups")
                selected = save_group_selection_by_keys(group_keys)
            else:
                scoped_key = str(body.get("candidate_key") or body.get("order_id") or "").strip()
                action = str(body.get("action") or "reject")
                if not scoped_key or len(scoped_key) > 500 or action not in {"reject", "restore"}:
                    raise ValueError("invalid request")
                _set_manual_rejection(scoped_key, action == "reject")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError, subprocess.SubprocessError):
            self._json({"error": "invalid request"}, status=400)
            return
        if path == "/api/groups/selection":
            self._json({
                "ok": True,
                "groups": [
                    {"group_key": public_group_key(group.chat_id), "chat_name": group.chat_name}
                    for group in selected
                ],
            })
        else:
            self._json({"ok": True, "action": action, "candidate_key": scoped_key})

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the local tutor dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("dashboard API 只允许绑定到本机回环地址")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    directory = configured_directory()
    if directory:
        start_model_setup(directory)
    print(f"Tutor dashboard API: http://{args.host}:{args.port}/api/dashboard", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
