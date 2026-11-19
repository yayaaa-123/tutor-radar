from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event, Lock, Thread
import time
import tomllib
from typing import Any

from .atomic_io import atomic_write_json, atomic_write_text
from .filtering import TutorProfile, filter_order, load_profile
from .group_selection import load_selected_groups
from .identity import candidate_key, event_candidate_key, public_candidate_key
from .maps import MapSettings
from .map_beta import apply_map_beta
from .model_setup import configured_directory
from .ollama_runtime import ENDPOINT as MANAGED_OLLAMA_ENDPOINT
from .model_pipeline import ModelPipeline, ModelSettings, ModelTask, audit_order
from .notifier import show_windows_notification
from .parser import extract_order_blocks, looks_like_order_block, parse_order
from .probe import open_database
from .scoring import CapabilityProfile, load_capability_profile, score_order
from .score_backfill import repair_candidate_scores
from .storage import MonitorStore
from .setup_config import read_profile_override


from .runtime_paths import PROJECT_ROOT


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True, slots=True)
class GroupConfig:
    chat_id: str
    chat_name: str


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    db_dir: str
    chat_id: str
    chat_name: str
    groups: tuple[GroupConfig, ...]
    group_selection_file: Path
    poll_interval: float
    backfill_messages: int
    startup_max_catchup_hours: int
    order_retention_hours: int
    retention_cleanup_interval_seconds: int
    database: Path
    health_file: Path
    events_file: Path
    profile: TutorProfile
    notifications_enabled: bool
    notification_seconds: int
    map_settings: MapSettings
    capability_profile: CapabilityProfile
    model_settings: ModelSettings
    config_path: Path | None = None


def apply_personal_conditions(config: ServiceConfig) -> ServiceConfig:
    if config.config_path is None:
        return config
    saved = read_profile_override(config.config_path)
    if not saved:
        return config
    subjects = tuple(saved["subjects"])
    return replace(
        config,
        profile=replace(config.profile, gender=saved["gender"], school=saved["school"],
                        year=saved["year"], subjects=subjects, weekend_only=saved["weekend_only"]),
        capability_profile=replace(config.capability_profile, education=saved["education"],
                                   strength_subjects=subjects, weekend_only=saved["weekend_only"]),
        map_settings=replace(config.map_settings, origin_address=saved["origin"]),
    )


def load_config(path: str | Path) -> ServiceConfig:
    config_path = Path(path)
    with config_path.open("rb") as file:
        data = tomllib.load(file)
    wechat = data["wechat"]
    storage = data["storage"]
    notifications = data.get("notifications", {})
    map_data = data.get("map", {})
    analysis = data.get("analysis", {})
    model_data = data.get("model", {})
    managed_model = bool(configured_directory())
    retention = data.get("retention", {})
    interval = float(wechat.get("poll_interval_seconds", 1.0))
    if interval < 0.5:
        raise ValueError("poll_interval_seconds 不能小于 0.5")
    raw_groups = wechat.get("groups") or []
    if raw_groups:
        groups = tuple(
            GroupConfig(str(item["chat_id"]), str(item.get("chat_name", item["chat_id"])))
            for item in raw_groups
        )
    elif "chat_id" in wechat:
        groups = (GroupConfig(str(wechat["chat_id"]), str(wechat.get("chat_name", wechat["chat_id"]))),)
    else:
        groups = ()
    if any(not group.chat_id.strip() for group in groups):
        raise ValueError("微信群 chat_id 不能为空")
    if len({group.chat_id for group in groups}) != len(groups):
        raise ValueError("微信群 chat_id 不能重复")
    return apply_personal_conditions(ServiceConfig(
        config_path=config_path,
        db_dir=str(wechat["db_dir"]),
        chat_id=groups[0].chat_id if groups else "",
        chat_name=groups[0].chat_name if groups else "",
        groups=groups,
        group_selection_file=_resolve_project_path(
            str(wechat.get("selection_file", "data/private/monitored-groups.json"))
        ),
        poll_interval=interval,
        backfill_messages=int(wechat.get("backfill_messages", 100)),
        startup_max_catchup_hours=max(
            0, int(wechat.get("startup_max_catchup_hours", 48))
        ),
        order_retention_hours=max(1, int(retention.get("order_hours", 48))),
        retention_cleanup_interval_seconds=max(
            30, int(retention.get("cleanup_interval_seconds", 300))
        ),
        database=_resolve_project_path(str(storage["database"])),
        health_file=_resolve_project_path(str(storage["health_file"])),
        events_file=_resolve_project_path(str(storage["events_file"])),
        profile=load_profile(config_path),
        notifications_enabled=bool(notifications.get("enabled", True)),
        notification_seconds=int(notifications.get("display_seconds", 8)),
        map_settings=MapSettings(
            enabled=bool(map_data.get("enabled", analysis.get("commute_enabled", False))),
            key_file=_resolve_project_path(str(map_data.get("key_file", "data/private/amap.key"))),
            cache_db=_resolve_project_path(str(map_data.get("cache_db", "data/private/map-cache.db"))),
            origin_address=str(map_data.get("origin_address", analysis.get("commute_origin", "深圳技术大学地铁站"))),
            origin_city=str(map_data.get("origin_city", "深圳市")),
            strategy=int(map_data.get("strategy", 8)),
            alternative_routes=int(map_data.get("alternative_routes", 3)),
            departure_time=str(map_data.get("departure_time", "09:00")),
            request_timeout_seconds=float(map_data.get("request_timeout_seconds", 6.0)),
            geocode_ttl_days=int(map_data.get("geocode_ttl_days", 30)),
            route_ttl_hours=int(map_data.get("route_ttl_hours", 12)),
            request_attempts=int(map_data.get("request_attempts", 3)),
            retry_backoff_seconds=float(map_data.get("retry_backoff_seconds", 1.0)),
            request_interval_seconds=float(map_data.get("request_interval_seconds", 0.0)),
            error_log=_resolve_project_path(
                str(map_data.get("error_log", "data/private/map-errors.jsonl"))
            ),
            js_key_file=_resolve_project_path(
                str(map_data.get("js_key_file", "data/private/amap-js.key"))
            ),
            js_security_code_file=_resolve_project_path(
                str(map_data.get("js_security_code_file", "data/private/amap-js-security-code.txt"))
            ),
        ),
        capability_profile=load_capability_profile(config_path),
        model_settings=ModelSettings(
            enabled=managed_model or bool(model_data.get("enabled", False)),
            provider=str(model_data.get("provider", "ollama")),
            name=str(model_data.get("name", "qwen3.5:4b")),
            endpoint=MANAGED_OLLAMA_ENDPOINT if managed_model else str(model_data.get("endpoint", "http://127.0.0.1:11434")),
            temperature=float(model_data.get("temperature", 0.1)),
            keep_alive=str(model_data.get("keep_alive", "10m")),
            request_timeout_seconds=float(model_data.get("request_timeout_seconds", 90.0)),
            queue_size=int(model_data.get("queue_size", 100)),
            results_file=_resolve_project_path(
                str(model_data.get("results_file", "data/private/model-results.json"))
            ),
        ),
    ))


class TutorMonitorService:
    def __init__(self, config: ServiceConfig, allow_process_read: bool):
        self.config = config
        self.db = open_database(allow_process_read, config.db_dir)
        self.store = MonitorStore(config.database)
        self.started_at = self._now()
        self._next_retention_cleanup = 0.0
        self.last_message_at: str | None = None
        self.last_order_at: str | None = None
        self.messages_seen = 0
        self.orders_extracted = 0
        self.candidates_found = 0
        self.orders_rejected = 0
        self.parse_errors = 0
        self.consecutive_errors = 0
        self._repair_stop = Event()
        self._repair_wake = Event()
        self._repair_thread: Thread | None = None
        self._event_lock = Lock()
        self._cache_lock = Lock()
        self._analysis_queued: set[str] = set()
        selected_groups = tuple(
            GroupConfig(group.chat_id, group.chat_name)
            for group in load_selected_groups(config.group_selection_file, config.groups)
        )
        self._active_groups = {group.chat_id: group for group in selected_groups}
        self._group_stats = {
            group.chat_id: {
                "chat_id": group.chat_id, "chat_name": group.chat_name,
                "monitored": True,
                "last_message_at": None, "last_order_at": None,
                "messages_seen": 0, "orders_extracted": 0,
                "candidates_found": 0, "orders_rejected": 0,
                "consecutive_errors": 0, "error": None,
            }
            for group in selected_groups
        }
        self.model_pipeline: ModelPipeline | None = None
        if config.model_settings.enabled:
            config.model_settings.validate()
            self.model_pipeline = ModelPipeline(config.model_settings)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def _write_health(self, status: str, error: str | None = None) -> None:
        primary = next(iter(getattr(self, "_active_groups", {}).values()), None)
        payload = {
            "status": status,
            "chat_id": primary.chat_id if primary else None,
            "chat_name": primary.chat_name if primary else None,
            "groups": list(getattr(self, "_group_stats", {}).values()),
            "started_at": self.started_at,
            "checked_at": self._now(),
            "last_message_at": self.last_message_at,
            "last_order_at": self.last_order_at,
            "messages_seen": self.messages_seen,
            "orders_extracted": self.orders_extracted,
            "candidates_found": self.candidates_found,
            "orders_rejected": self.orders_rejected,
            "parse_errors": self.parse_errors,
            "consecutive_errors": self.consecutive_errors,
            "error": error,
        }
        try:
            atomic_write_json(self.config.health_file, payload)
        except OSError as exc:
            diagnostic = {
                "at": self._now(),
                "event": "health_write_failed",
                "error_type": type(exc).__name__,
                "errno": exc.errno,
                "winerror": getattr(exc, "winerror", None),
            }
            log_path = self.config.health_file.with_name("health-write-errors.jsonl")
            try:
                with log_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _append_event(self, payload: dict[str, Any]) -> None:
        self.config.events_file.parent.mkdir(parents=True, exist_ok=True)
        with self._event_lock:
            with self.config.events_file.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _event_timestamp(event: dict[str, Any]) -> int:
        try:
            return int(datetime.fromisoformat(str(event.get("captured_at") or "")).timestamp())
        except (TypeError, ValueError):
            return 0

    def _cleanup_retention(self, *, force: bool = False) -> dict[str, int]:
        if not hasattr(self.store, "prune_orders_before"):
            return {"events_deleted": 0, "orders_deleted": 0}
        now = time.monotonic()
        if not force and now < getattr(self, "_next_retention_cleanup", 0.0):
            return {"events_deleted": 0, "orders_deleted": 0}
        interval = int(getattr(self.config, "retention_cleanup_interval_seconds", 300))
        retention_hours = int(getattr(self.config, "order_retention_hours", 48))
        self._next_retention_cleanup = now + interval
        cutoff = int(time.time()) - retention_hours * 3600
        removed = self.store.prune_orders_before(cutoff)
        stale_messages = set(removed["messages"])
        deleted_events = 0
        surviving_keys: set[str] = set()
        surviving_order_ids: set[str] = set()
        with self._event_lock:
            try:
                lines = self.config.events_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            kept: list[str] = []
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    deleted_events += 1
                    continue
                event_message = (
                    str(event.get("chat_id") or ""), int(event.get("local_id") or 0)
                )
                captured_at = self._event_timestamp(event)
                if event_message in stale_messages or not captured_at or captured_at < cutoff:
                    deleted_events += 1
                    continue
                kept.append(json.dumps(event, ensure_ascii=False))
                if event.get("event") == "candidate":
                    scoped_key = event_candidate_key(event)
                    order_id = str((event.get("order") or {}).get("order_id") or "")
                    if scoped_key:
                        surviving_keys.add(scoped_key)
                    if order_id:
                        surviving_order_ids.add(order_id)
            if deleted_events:
                content = "\n".join(kept)
                atomic_write_text(self.config.events_file, content + ("\n" if content else ""))
        with self._cache_lock:
            self._prune_candidate_cache(
                self.config.events_file.parent / "candidate-enrichments.json",
                "candidates", surviving_keys, surviving_order_ids,
            )
        if self.model_pipeline is not None:
            with self.model_pipeline._results_lock:
                self._prune_candidate_cache(
                    self.config.model_settings.results_file,
                    "orders", surviving_keys, surviving_order_ids,
                )
        return {
            "events_deleted": deleted_events,
            "orders_deleted": int(removed["orders_deleted"]),
        }

    @staticmethod
    def _prune_candidate_cache(
        path: Path, field: str, surviving_keys: set[str], surviving_order_ids: set[str],
    ) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        mapping = payload.get(field)
        if not isinstance(mapping, dict):
            return
        kept = {
            key: value for key, value in mapping.items()
            if key in surviving_keys or key in surviving_order_ids
        }
        if len(kept) != len(mapping):
            payload[field] = kept
            atomic_write_json(path, payload)

    def _repair_commutes(self) -> None:
        while not self._repair_stop.is_set():
            self._repair_wake.clear()
            try:
                map_settings = apply_map_beta(self.config.map_settings)
                if map_settings.enabled:
                    with self._cache_lock:
                        repair_candidate_scores(replace(self.config, map_settings=map_settings), retry_only=True)
                    self._queue_ready_analyses()
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "commute_repair_error", "error": type(exc).__name__},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            self._repair_wake.wait(60.0)

    def _start_commute_repair(self) -> None:
        if self._repair_thread is not None:
            return
        self._repair_thread = Thread(
            target=self._repair_commutes,
            name="commute-repair",
            daemon=True,
        )
        self._repair_thread.start()

    def _process_message(self, group: GroupConfig, message: dict[str, Any], emit: bool) -> int:
        stats = self._group_stats[group.chat_id]
        self.messages_seen += 1
        stats["messages_seen"] += 1
        self.last_message_at = self._now()
        stats["last_message_at"] = self.last_message_at
        content = message.get("content") or ""
        if not content.strip():
            return 0
        orders = []
        for block in extract_order_blocks(content):
            try:
                orders.append(parse_order(block))
            except ValueError:
                if looks_like_order_block(block):
                    orders.append(parse_order(block, require_complete=False))
        if not orders:
            return 0
        if not self.store.add_message(group.chat_id, message):
            return 0

        candidate_count = 0
        candidate_events: list[dict[str, Any]] = []
        for order in orders:
            self.store.add_order(
                group.chat_id,
                int(message["local_id"]),
                order.order_id,
                order.to_dict(),
            )
            self.orders_extracted += 1
            stats["orders_extracted"] += 1
            audit = audit_order(order)
            if not audit.complete and self.model_pipeline is None:
                self.store.add_order(
                    group.chat_id,
                    int(message["local_id"]),
                    order.order_id,
                    order.to_dict(),
                    "information_missing",
                )
                continue
            if self.model_pipeline is not None and not audit.complete:
                local_id = int(message["local_id"])

                def recognition_finished(
                    enriched: Any,
                    recognition: dict[str, Any],
                    local_id: int = local_id,
                    emit_result: bool = emit,
                    result_group: GroupConfig = group,
                ) -> None:
                    assert self.model_pipeline is not None
                    result_key = candidate_key(result_group.chat_id, enriched.order_id)
                    self.model_pipeline.write_result(result_key, "recognition", recognition)
                    self.store.add_order(
                        result_group.chat_id,
                        local_id,
                        enriched.order_id,
                        enriched.to_dict(),
                        None if recognition.get("status") == "complete" else "information_missing",
                    )
                    if recognition.get("status") != "complete":
                        self._append_event({
                            "event": "information_missing",
                            "captured_at": self._now(),
                            "chat_id": result_group.chat_id,
                            "chat_name": result_group.chat_name,
                            "local_id": local_id,
                            "order": enriched.to_dict(),
                            "recognition": recognition,
                        })
                    self._finalize_order(result_group, enriched, local_id, emit_result, recognition)

                queued = self.model_pipeline.submit(ModelTask(
                    kind="recognize",
                    order=order,
                    context={},
                    callback=recognition_finished,
                ))
                if not queued:
                    missing = {
                        "role": "recognition",
                        "status": "information_missing",
                        "missing_fields": list(audit.issues),
                        "error": "queue_full",
                    }
                    self.model_pipeline.write_result(candidate_key(group.chat_id, order.order_id), "recognition", missing)
                    self.store.add_order(
                        group.chat_id,
                        local_id,
                        order.order_id,
                        order.to_dict(),
                        "information_missing",
                    )
                    self._append_event({
                        "event": "information_missing",
                        "captured_at": self._now(),
                        "chat_id": group.chat_id,
                        "chat_name": group.chat_name,
                        "local_id": local_id,
                        "order": order.to_dict(),
                        "recognition": missing,
                    })
                    self._finalize_order(group, order, local_id, emit, missing)
                continue
            candidate_event = self._finalize_order(group, order, int(message["local_id"]), emit)
            if candidate_event is not None:
                candidate_count += 1
                candidate_events.append(candidate_event)
        if candidate_count:
            self.last_order_at = self._now()
            stats["last_order_at"] = self.last_order_at
            self._repair_wake.set()
            if emit and self.config.notifications_enabled:
                show_windows_notification(candidate_events, self.config.notification_seconds)
        return candidate_count

    def _finalize_order(
        self,
        group: GroupConfig,
        order: Any,
        local_id: int,
        emit: bool,
        recognition: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        decision = filter_order(order, self.config.profile)
        if decision.status == "rejected":
            self.orders_rejected += 1
            self._group_stats[group.chat_id]["orders_rejected"] += 1
            return None
        if not self.store.claim_candidate(group.chat_id, order.order_id):
            return None
        self.candidates_found += 1
        self._group_stats[group.chat_id]["candidates_found"] += 1
        commute = {
            "provider": "amap",
            "mode": "public_transit",
            "origin_address": self.config.map_settings.origin_address,
            "destination_address": order.address,
            "status": "pending",
        }
        score = score_order(order, commute, self.config.capability_profile)
        event = {
            "event": "candidate",
            "captured_at": self._now(),
            "chat_id": group.chat_id,
            "chat_name": group.chat_name,
            "local_id": local_id,
            "order": order.to_dict(),
            "filter": {
                "status": decision.status,
                "priority": decision.priority,
                "reasons": decision.reasons,
            },
            "commute": commute,
            "score": score,
        }
        if recognition is not None:
            event["recognition"] = recognition
        self._append_event(event)
        if emit:
            print(
                json.dumps(
                    {
                        "event": "candidate",
                        "order_id": order.order_id,
                        "district": order.district,
                        "subjects": order.subjects,
                        "hourly_rate": [order.hourly_rate_min, order.hourly_rate_max],
                        "status": decision.status,
                        "priority": decision.priority,
                        "score": score["total_score"],
                        "recommendation": score["recommendation"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        self.last_order_at = self._now()
        self._group_stats[group.chat_id]["last_order_at"] = self.last_order_at
        self._repair_wake.set()
        if recognition is not None and emit and self.config.notifications_enabled:
            show_windows_notification([event], self.config.notification_seconds)
        return event

    def _refresh_group_selection(self, watermarks: dict[str, int]) -> None:
        selected = tuple(
            GroupConfig(group.chat_id, group.chat_name)
            for group in load_selected_groups(
                self.config.group_selection_file,
                self.config.groups,
            )
        )
        selected_ids = {group.chat_id for group in selected}
        for stats in self._group_stats.values():
            stats["monitored"] = stats["chat_id"] in selected_ids
        for group in selected:
            stats = self._group_stats.setdefault(
                group.chat_id,
                {
                    "chat_id": group.chat_id, "chat_name": group.chat_name,
                    "monitored": True,
                    "last_message_at": None, "last_order_at": None,
                    "messages_seen": 0, "orders_extracted": 0,
                    "candidates_found": 0, "orders_rejected": 0,
                    "consecutive_errors": 0, "error": None,
                },
            )
            stats["chat_name"] = group.chat_name
            stats["monitored"] = True
            if group.chat_id not in watermarks:
                try:
                    watermarks[group.chat_id] = self.bootstrap(group)
                except Exception as exc:
                    stats["consecutive_errors"] = int(stats["consecutive_errors"]) + 1
                    stats["error"] = repr(exc)
                    watermarks[group.chat_id] = self.store.get_watermark(group.chat_id) or 0
        self._active_groups = {group.chat_id: group for group in selected}

    def _queue_ready_analyses(self) -> None:
        if self.model_pipeline is None:
            return
        try:
            overlays = json.loads(
                (self.config.events_file.parent / "candidate-enrichments.json").read_text(encoding="utf-8")
            ).get("candidates", {})
        except (OSError, json.JSONDecodeError):
            return
        try:
            model_results = json.loads(
                self.config.model_settings.results_file.read_text(encoding="utf-8")
            ).get("orders", {})
        except (OSError, json.JSONDecodeError):
            model_results = {}
        try:
            rejection_payload = json.loads(
                (self.config.events_file.parent / "manual-rejections.json").read_text(encoding="utf-8")
            )
            manual_rejections = set(rejection_payload.get("candidate_keys", []))
            legacy_rejections = set(rejection_payload.get("order_ids", []))
        except (OSError, json.JSONDecodeError):
            manual_rejections = set()
            legacy_rejections = set()
        events: dict[str, dict[str, Any]] = {}
        try:
            lines = self.config.events_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "candidate":
                key = event_candidate_key(event)
                if key:
                    events[key] = event
        order_id_counts: dict[str, int] = {}
        for event in events.values():
            order_id = str((event.get("order") or {}).get("order_id") or "")
            order_id_counts[order_id] = order_id_counts.get(order_id, 0) + 1
        from dataclasses import fields
        from .parser import TutorOrder
        order_fields = {item.name for item in fields(TutorOrder)}
        for scoped_key, event in events.items():
            order_id = str((event.get("order") or {}).get("order_id") or "")
            public_key = public_candidate_key(event.get("chat_id"), order_id)
            allow_legacy = order_id_counts.get(order_id) == 1
            if public_key in manual_rejections or (allow_legacy and order_id in legacy_rejections):
                continue
            legacy_result = model_results.get(order_id) if allow_legacy else None
            analysis_state = (model_results.get(scoped_key) or legacy_result or {}).get("analysis") or {}
            if analysis_state.get("status") == "complete" or scoped_key in self._analysis_queued:
                continue
            legacy_overlay = overlays.get(order_id) if allow_legacy else None
            overlay = overlays.get(scoped_key) or legacy_overlay or {}
            commute = overlay.get("commute") or {}
            score = overlay.get("score")
            if commute.get("status") != "ok" or not isinstance(score, dict):
                continue
            order_data = {key: value for key, value in (event.get("order") or {}).items() if key in order_fields}
            order = TutorOrder(**order_data)

            def analysis_finished(
                _: Any,
                analysis: dict[str, Any],
                result_key: str = scoped_key,
            ) -> None:
                assert self.model_pipeline is not None
                self.model_pipeline.write_result(result_key, "analysis", analysis)

            context = {
                "order": {key: value for key, value in order.to_dict().items() if key != "raw_text"},
                "user_capability": {
                    "gender": self.config.profile.gender,
                    "school": self.config.profile.school,
                    "year": self.config.profile.year,
                    "education": self.config.capability_profile.education,
                    "strength_subjects": self.config.capability_profile.strength_subjects,
                    "preferred_subjects": self.config.capability_profile.preferred_subjects,
                    "experience": [
                        {"level": level, "subjects": subjects}
                        for level, subjects in self.config.capability_profile.experience
                    ],
                    "weekend_only": self.config.capability_profile.weekend_only,
                },
                "hard_filter": event.get("filter"),
                "commute": commute,
                "fixed_score": score,
            }
            if self.model_pipeline.submit(ModelTask("analyze", order, context, analysis_finished)):
                self._analysis_queued.add(scoped_key)

    def bootstrap(self, group: GroupConfig) -> int:
        watermark = self.store.get_watermark(group.chat_id)
        watermark_time = (
            self.store.get_message_create_time(group.chat_id, watermark)
            if watermark is not None
            else None
        )
        max_age_seconds = self.config.startup_max_catchup_hours * 3600
        watermark_is_fresh = (
            watermark is not None
            and (
                max_age_seconds <= 0
                or watermark_time is None
                or watermark_time >= int(time.time()) - max_age_seconds
            )
        )
        if watermark_is_fresh:
            return watermark
        messages = list(
            reversed(
                self.db.get_messages(
                    group.chat_id, limit=self.config.backfill_messages
                )
            )
        )
        watermark = 0
        for message in messages:
            watermark = max(watermark, int(message["sort_seq"]))
            if not self._message_is_stale(message):
                self._process_message(group, message, emit=False)
        self.store.set_watermark(group.chat_id, watermark)
        return watermark

    def _message_is_stale(self, message: dict[str, Any]) -> bool:
        timestamp = int(message.get("create_time") or 0)
        retention_hours = int(getattr(getattr(self, "config", None), "order_retention_hours", 0))
        cutoff = int(time.time()) - retention_hours * 3600 if retention_hours > 0 else int(
            getattr(self, "_monitor_cutoff_timestamp", 0)
        )
        return bool(
            cutoff
            and timestamp
            and timestamp < cutoff
        )

    def _poll_group(self, group: GroupConfig, watermark: int) -> int:
        """Read through stale sequence gaps without parsing them as live orders."""
        pages = 0
        while pages < 25:
            messages = self.db.get_new_messages(
                group.chat_id, since_seq=watermark, limit=200
            )
            if not messages:
                break
            pages += 1
            for message in messages:
                watermark = max(watermark, int(message["sort_seq"]))
                if not self._message_is_stale(message):
                    self._process_message(group, message, emit=True)
                self.store.set_watermark(group.chat_id, watermark)
            if len(messages) < 200:
                break
        return watermark

    def _enable_ready_model(self) -> None:
        if self.model_pipeline is not None or not configured_directory():
            return
        settings = replace(self.config.model_settings, enabled=True, endpoint=MANAGED_OLLAMA_ENDPOINT)
        settings.validate()
        pipeline = ModelPipeline(settings)
        pipeline.start()
        self.config = replace(self.config, model_settings=settings)
        self.model_pipeline = pipeline
        self._repair_wake.set()

    def run(self, duration_seconds: int = 0) -> dict[str, Any]:
        self._cleanup_retention(force=True)
        if self.model_pipeline is not None:
            self.model_pipeline.start()
        watermarks: dict[str, int] = {}
        for group in self._active_groups.values():
            try:
                watermarks[group.chat_id] = self.bootstrap(group)
            except Exception as exc:
                stats = self._group_stats[group.chat_id]
                stats["consecutive_errors"] = 1
                stats["error"] = repr(exc)
                watermarks[group.chat_id] = self.store.get_watermark(group.chat_id) or 0
        deadline = time.monotonic() + duration_seconds if duration_seconds > 0 else None
        initial_stats = [
            self._group_stats[group.chat_id]
            for group in self._active_groups.values()
        ]
        initial_errors = [str(stats["error"]) for stats in initial_stats if stats["error"]]
        self.consecutive_errors = max(
            (int(stats["consecutive_errors"]) for stats in initial_stats),
            default=0,
        )
        initial_status = "degraded" if initial_errors else "running" if self._active_groups else "idle"
        self._write_health(initial_status, "; ".join(initial_errors) or None)
        self._start_commute_repair()
        try:
            while deadline is None or time.monotonic() < deadline:
                try:
                    self.config = apply_personal_conditions(self.config)
                    self._enable_ready_model()
                    self._cleanup_retention()
                    selection_error: str | None = None
                    try:
                        self._refresh_group_selection(watermarks)
                    except Exception as exc:
                        selection_error = repr(exc)
                    for group in self._active_groups.values():
                        stats = self._group_stats[group.chat_id]
                        try:
                            watermarks[group.chat_id] = self._poll_group(
                                group, watermarks[group.chat_id]
                            )
                            stats["consecutive_errors"] = 0
                            stats["error"] = None
                        except Exception as exc:
                            stats["consecutive_errors"] += 1
                            stats["error"] = repr(exc)
                    active_stats = [
                        self._group_stats[group.chat_id]
                        for group in self._active_groups.values()
                    ]
                    self.consecutive_errors = max(
                        (int(stats["consecutive_errors"]) for stats in active_stats),
                        default=0,
                    )
                    errors = [str(stats["error"]) for stats in active_stats if stats["error"]]
                    if selection_error:
                        errors.append(selection_error)
                    status = "degraded" if errors else "running" if self._active_groups else "idle"
                    self._write_health(status, "; ".join(errors) or None)
                except Exception as exc:
                    self.consecutive_errors += 1
                    self._write_health("degraded", repr(exc))
                time.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._repair_stop.set()
            self._repair_wake.set()
            if self.model_pipeline is not None:
                self.model_pipeline.stop()
            self._write_health("stopped")
        return {
            "watermarks": watermarks,
            "messages_seen": self.messages_seen,
            "orders_extracted": self.orders_extracted,
            "candidates_found": self.candidates_found,
            "orders_rejected": self.orders_rejected,
            "parse_errors": self.parse_errors,
            "health_file": str(self.config.health_file),
            "events_file": str(self.config.events_file),
        }
