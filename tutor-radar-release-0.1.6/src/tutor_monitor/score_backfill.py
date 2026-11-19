from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .dashboard_api import build_dashboard_payload
from .filtering import filter_order
from .identity import candidate_key
from .maps import AmapCommuteEnricher
from .parser import TutorOrder, parse_orders
from .scoring import SCORING_VERSION, score_order


from .runtime_paths import PROJECT_ROOT
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "private" / "candidate-enrichments.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def repair_candidate_scores(
    config: Any,
    output_path: str | Path = DEFAULT_OUTPUT,
    force: bool = False,
    retry_only: bool = False,
) -> dict[str, Any]:
    output = Path(output_path)
    try:
        stored = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = {"version": 1, "candidates": {}}
    overlays = stored.get("candidates") or {}
    payload = build_dashboard_payload(enrichments_file=output, _include_private=True)
    candidates = payload["candidates"] + payload["rejected_candidates"]
    order_id_counts: dict[str, int] = {}
    for candidate in candidates:
        order_id = str(candidate["order"].get("order_id") or "")
        order_id_counts[order_id] = order_id_counts.get(order_id, 0) + 1
    order_fields = {item.name for item in fields(TutorOrder)}
    enricher = AmapCommuteEnricher(config.map_settings)
    updated = 0
    skipped = 0
    failed = 0
    try:
        for candidate in candidates:
            order_id = str(candidate["order"].get("order_id") or "")
            scoped_key = str(
                candidate.get("_storage_key")
                or candidate_key(candidate.get("_chat_id"), order_id)
            )
            legacy_existing = overlays.get(order_id) if order_id_counts.get(order_id) == 1 else None
            existing = overlays.get(scoped_key) or legacy_existing or {}
            if scoped_key not in overlays and legacy_existing:
                overlays[scoped_key] = legacy_existing
            current_commute = existing.get("commute") or candidate.get("commute") or {}
            retryable = current_commute.get("status") == "pending" or (
                current_commute.get("status") == "error"
                and str(current_commute.get("error") or "").startswith("network_")
            )
            if retry_only and not retryable:
                skipped += 1
                continue
            if (
                not force
                and not retryable
                and existing.get("score", {}).get("version") == SCORING_VERSION
            ):
                skipped += 1
                continue
            order_data = {
                key: value for key, value in candidate["order"].items() if key in order_fields
            }
            order = TutorOrder(**order_data)
            commute = enricher.enrich(order.address)
            score = score_order(order, commute, config.capability_profile)
            overlays[scoped_key] = {
                "commute": commute,
                "score": score,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
            updated += 1
            if commute.get("status") != "ok":
                failed += 1
            # Publish each completed candidate immediately. A full repair can
            # take minutes under the shared Amap rate limit; waiting for the
            # whole batch makes the map appear empty and loses partial work if
            # the process exits midway.
            _atomic_write(output, {
                "version": 1,
                "scoring_version": SCORING_VERSION,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "candidates": overlays,
            })
    finally:
        enricher.close()
    result = {
        "version": 1,
        "scoring_version": SCORING_VERSION,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "candidates": overlays,
    }
    _atomic_write(output, result)
    return {
        "candidates_seen": len(candidates),
        "updated": updated,
        "skipped": skipped,
        "map_failures": failed,
        "output": str(output),
    }


def backfill_candidate_scores(
    config_path: str | Path = "config.toml",
    output_path: str | Path = DEFAULT_OUTPUT,
    force: bool = False,
) -> dict[str, Any]:
    from .service import load_config

    return repair_candidate_scores(load_config(config_path), output_path, force)


def append_reparsed_candidate(
    config: Any,
    order_id: str,
    output_path: str | Path = DEFAULT_OUTPUT,
    chat_id: str | None = None,
) -> dict[str, Any]:
    matching_events: list[dict[str, Any]] = []
    try:
        lines = config.events_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("候选事件文件不可读") from exc
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "candidate":
            continue
        if str((event.get("order") or {}).get("order_id") or "") != order_id:
            continue
        if chat_id is not None and str(event.get("chat_id") or "") != chat_id:
            continue
        matching_events.append(event)
    matching_chats = {str(event.get("chat_id") or "") for event in matching_events}
    if chat_id is None and len(matching_chats) > 1:
        raise ValueError("多个群存在相同订单编号，必须指定群聊")
    source_event = matching_events[0] if matching_events else None
    if source_event is None:
        raise ValueError(f"未找到候选：{order_id}")
    raw_text = str((source_event.get("order") or {}).get("raw_text") or "")
    reparsed = parse_orders(raw_text)
    matching = next((order for order in reparsed if order.order_id == order_id), None)
    if matching is None:
        if len(reparsed) != 1:
            raise ValueError("原消息包含多个订单，无法安全确定需要更正的候选")
        matching = reparsed[0]
    matching.order_id = order_id
    decision = filter_order(matching, config.profile)
    commute = {
        "provider": "amap",
        "mode": "public_transit",
        "origin_address": config.map_settings.origin_address,
        "destination_address": matching.address,
        "status": "pending",
    }
    corrected_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    corrected_event = {
        **source_event,
        "captured_at": corrected_at,
        "correction_of": source_event.get("captured_at"),
        "order": matching.to_dict(),
        "filter": {
            "status": decision.status,
            "priority": decision.priority,
            "reasons": decision.reasons,
        },
        "commute": commute,
        "score": score_order(matching, commute, config.capability_profile),
    }
    with config.events_file.open("a", encoding="utf-8") as file:
        file.write(json.dumps(corrected_event, ensure_ascii=False) + "\n")

    output = Path(output_path)
    try:
        stored = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stored = {"version": 1, "candidates": {}}
    overlays = stored.setdefault("candidates", {})
    overlay_key = candidate_key(str(source_event.get("chat_id") or ""), order_id)
    overlays.pop(overlay_key, None)
    if not source_event.get("chat_id"):
        overlays.pop(order_id, None)
    atomic_write_json(output, stored)
    return {
        "order_id": order_id,
        "address": matching.address,
        "pay_text": matching.pay_text,
        "status": decision.status,
        "corrected_at": corrected_at,
    }
