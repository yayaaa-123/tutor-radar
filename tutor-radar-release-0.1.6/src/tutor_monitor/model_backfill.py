from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .dashboard_api import build_dashboard_payload
from .filtering import filter_order
from .model_pipeline import OllamaClient, analyze_order, audit_order, recognize_order
from .parser import TutorOrder, parse_orders
from .score_backfill import repair_candidate_scores
from .scoring import score_order


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def _append_event(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def reanalyze_incomplete_candidates(config: Any) -> dict[str, Any]:
    dashboard = build_dashboard_payload()
    active_ids = {str(item["order"]["order_id"]) for item in dashboard["candidates"]}
    latest_events: dict[str, dict[str, Any]] = {}
    for line in config.events_file.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "candidate":
            continue
        order_id = str((event.get("order") or {}).get("order_id") or "")
        if order_id in active_ids:
            latest_events[order_id] = event

    model_file = config.model_settings.results_file
    model_results = _read_json(model_file, {"version": 1, "orders": {}})
    model_orders = model_results.setdefault("orders", {})
    order_fields = {item.name for item in fields(TutorOrder)}
    selected: list[tuple[TutorOrder, dict[str, Any]]] = []
    for order_id, event in latest_events.items():
        order = TutorOrder(**{
            key: value for key, value in (event.get("order") or {}).items()
            if key in order_fields
        })
        recognition = (model_orders.get(order_id) or {}).get("recognition") or {}
        if audit_order(order).complete and recognition.get("status") != "information_missing":
            continue
        try:
            reparsed_orders = parse_orders(order.raw_text)
            matching = next((item for item in reparsed_orders if item.order_id == order.order_id), None)
            if matching is None and len(reparsed_orders) == 1:
                matching = reparsed_orders[0]
            if matching is not None:
                matching.order_id = order.order_id
                order = matching
        except ValueError:
            pass
        selected.append((order, event))
    client = OllamaClient(config.model_settings)
    completed: list[tuple[TutorOrder, dict[str, Any], dict[str, Any]]] = []
    missing = 0
    errors = 0
    try:
        for index, (order, source_event) in enumerate(selected, 1):
            if audit_order(order).complete:
                enriched = order
                recognition = {
                    "role": "deterministic_reparse",
                    "status": "complete",
                    "original_issues": [],
                    "accepted_fields": [],
                    "missing_fields": [],
                }
            else:
                try:
                    enriched, recognition = recognize_order(client, order)
                except Exception as exc:
                    enriched = order
                    recognition = {
                        "role": "recognition",
                        "status": "error",
                        "error": type(exc).__name__,
                        "missing_fields": list(audit_order(order).issues),
                    }
                    errors += 1
            model_orders.setdefault(order.order_id, {})["recognition"] = recognition
            model_orders[order.order_id].pop("analysis", None)
            model_results["updated_at"] = _now()
            atomic_write_json(model_file, model_results)

            if recognition.get("status") != "complete":
                missing += 1
                _append_event(config.events_file, {
                    "event": "information_missing",
                    "captured_at": _now(),
                    "chat_id": source_event.get("chat_id"),
                    "chat_name": source_event.get("chat_name"),
                    "local_id": source_event.get("local_id"),
                    "order": enriched.to_dict(),
                    "recognition": recognition,
                })
                print(json.dumps({"progress": index, "total": len(selected), "order_id": order.order_id, "status": "information_missing"}, ensure_ascii=False), flush=True)
                continue

            decision = filter_order(enriched, config.profile)
            commute = {
                "provider": "amap",
                "mode": "public_transit",
                "origin_address": config.map_settings.origin_address,
                "destination_address": enriched.address,
                "status": "pending",
            }
            corrected = {
                **source_event,
                "captured_at": _now(),
                "correction_of": source_event.get("captured_at"),
                "order": enriched.to_dict(),
                "filter": {
                    "status": decision.status,
                    "priority": decision.priority,
                    "reasons": decision.reasons,
                },
                "commute": commute,
                "score": score_order(enriched, commute, config.capability_profile),
                "recognition": recognition,
            }
            _append_event(config.events_file, corrected)
            completed.append((enriched, corrected, recognition))
            print(json.dumps({"progress": index, "total": len(selected), "order_id": order.order_id, "status": "recognized"}, ensure_ascii=False), flush=True)
    finally:
        client.close()

    enrichment_file = config.events_file.parent / "candidate-enrichments.json"
    enrichments = _read_json(enrichment_file, {"version": 1, "candidates": {}})
    for order, _, _ in completed:
        enrichments.setdefault("candidates", {}).pop(order.order_id, None)
    atomic_write_json(enrichment_file, enrichments)
    if completed:
        repair_candidate_scores(config, enrichment_file, retry_only=True)
        enrichments = _read_json(enrichment_file, {"candidates": {}}).get("candidates", {})

    client = OllamaClient(config.model_settings)
    analyzed = 0
    try:
        for order, event, _ in completed:
            overlay = enrichments.get(order.order_id) or {}
            commute, score = overlay.get("commute") or {}, overlay.get("score")
            if commute.get("status") != "ok" or not isinstance(score, dict):
                continue
            context = {
                "order": {key: value for key, value in order.to_dict().items() if key != "raw_text"},
                "user_capability": {
                    "gender": config.profile.gender,
                    "school": config.profile.school,
                    "year": config.profile.year,
                    "education": config.capability_profile.education,
                    "strength_subjects": config.capability_profile.strength_subjects,
                    "preferred_subjects": config.capability_profile.preferred_subjects,
                    "experience": [
                        {"level": level, "subjects": subjects}
                        for level, subjects in config.capability_profile.experience
                    ],
                    "weekend_only": config.capability_profile.weekend_only,
                },
                "hard_filter": event.get("filter"),
                "commute": commute,
                "fixed_score": score,
            }
            try:
                analysis = analyze_order(client, order, context)
            except Exception as exc:
                analysis = {"role": "analysis", "status": "error", "error": type(exc).__name__}
                errors += 1
            model_orders.setdefault(order.order_id, {})["analysis"] = analysis
            model_results["updated_at"] = _now()
            atomic_write_json(model_file, model_results)
            analyzed += analysis.get("status") == "complete"
    finally:
        client.close()

    return {
        "selected": len(selected),
        "recognized": len(completed),
        "information_missing": missing,
        "analyzed": analyzed,
        "errors": errors,
    }
