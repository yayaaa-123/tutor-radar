from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import sqlite3

from .parser import parse_orders
from .filtering import filter_order, load_profile
from .probe import (
    environment_report,
    backfill_orders,
    list_sessions,
    listen,
    raw_message_metadata,
    recent_messages,
)
from .service import TutorMonitorService, load_config
from .maps import AmapCommuteEnricher
from .score_backfill import append_reparsed_candidate, backfill_candidate_scores
from .model_backfill import reanalyze_incomplete_candidates


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _parse_command(path: str | None) -> int:
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    try:
        result = parse_orders(raw)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps([order.to_dict() for order in result], ensure_ascii=False, indent=2))
    return 0


def _show_orders(database: str, limit: int) -> int:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT parsed_json FROM orders ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        connection.close()
    seen: set[str] = set()
    output = []
    fields = (
        "order_id", "address", "subjects", "student_text", "student_gender",
        "current_level", "requirements_text", "schedule_text", "frequency_per_week",
        "duration_hours", "latest_end_time", "hourly_rate_min", "hourly_rate_max",
        "trial_time", "warnings",
    )
    for (raw,) in rows:
        order = json.loads(raw)
        if order["order_id"] in seen:
            continue
        seen.add(order["order_id"])
        output.append({field: order.get(field) for field in fields})
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _filter_file(path: str, output: str | None, config: str) -> int:
    messages = json.loads(Path(path).read_text(encoding="utf-8"))
    profile = load_profile(config)
    decisions = []
    parse_errors = []
    for message in messages:
        content = message.get("content") or ""
        try:
            orders = parse_orders(content)
        except ValueError as exc:
            parse_errors.append({"local_id": message.get("local_id"), "error": str(exc)})
            continue
        for order in orders:
            item = filter_order(order, profile).to_dict()
            item["source_local_id"] = message.get("local_id")
            decisions.append(item)
    result = {
        "summary": {
            "messages": len(messages),
            "orders": len(decisions),
            "matched": sum(item["status"] == "matched" for item in decisions),
            "review": sum(item["status"] == "review" for item in decisions),
            "rejected": sum(item["status"] == "rejected" for item in decisions),
            "unrecognized_messages": len(parse_errors),
        },
        "candidates": [item for item in decisions if item["status"] != "rejected"],
        "parse_errors": parse_errors,
    }
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    _configure_utf8_console()
    parser = argparse.ArgumentParser(prog="tutor-monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    parse_parser = commands.add_parser("parse", help="Parse one tutor order from a file or stdin")
    parse_parser.add_argument("path", nargs="?", help="UTF-8 text file; omit to read stdin")
    show_parser = commands.add_parser("show-orders", help="Show parsed local orders")
    show_parser.add_argument("--database", default="data/private/monitor.db")
    show_parser.add_argument("--limit", type=int, default=20)
    filter_parser = commands.add_parser("filter-file", help="Filter exported messages with the local tutor profile")
    filter_parser.add_argument("path", help="UTF-8 recent-message JSON file")
    filter_parser.add_argument("--output", help="Write filtered candidates as UTF-8 JSON")
    filter_parser.add_argument("--config", default="config.toml")
    probe_parser = commands.add_parser("probe", help="Read-only WeChat compatibility probe")
    probe_commands = probe_parser.add_subparsers(dest="probe_command", required=True)
    probe_commands.add_parser("env", help="Safe environment check; does not read process memory")

    sessions_parser = probe_commands.add_parser("sessions", help="List recent WeChat sessions")
    sessions_parser.add_argument("--limit", type=int, default=30)
    sessions_parser.add_argument("--db-dir")
    sessions_parser.add_argument("--name-contains", help="Only return chats whose name contains this text")
    sessions_parser.add_argument("--allow-process-read", action="store_true")

    recent_parser = probe_commands.add_parser("recent", help="Read recent messages from one chat")
    recent_parser.add_argument("chat_id")
    recent_parser.add_argument("--limit", type=int, default=10)
    recent_parser.add_argument("--show-content", action="store_true")
    recent_parser.add_argument("--output", help="Write the result as UTF-8 JSON")
    recent_parser.add_argument("--db-dir")
    recent_parser.add_argument("--allow-process-read", action="store_true")

    raw_parser = probe_commands.add_parser("raw-meta", help="Inspect message byte metadata only")
    raw_parser.add_argument("chat_id")
    raw_parser.add_argument("local_ids", nargs="+", type=int)
    raw_parser.add_argument("--db-dir")
    raw_parser.add_argument("--allow-process-read", action="store_true")

    backfill_parser = probe_commands.add_parser("backfill", help="Parse recent bundled order messages")
    backfill_parser.add_argument("chat_id")
    backfill_parser.add_argument("--limit", type=int, default=100)
    backfill_parser.add_argument("--db-dir")
    backfill_parser.add_argument("--allow-process-read", action="store_true")

    listen_parser = probe_commands.add_parser("listen", help="Listen to one chat and persist orders")
    listen_parser.add_argument("chat_id")
    listen_parser.add_argument("--seconds", type=int, default=60)
    listen_parser.add_argument("--interval", type=float, default=1.0)
    listen_parser.add_argument("--show-content", action="store_true")
    listen_parser.add_argument("--db-dir")
    listen_parser.add_argument("--allow-process-read", action="store_true")

    run_parser = commands.add_parser("run", help="Run the configured read-only monitor")
    run_parser.add_argument("--config", default="config.toml")
    run_parser.add_argument("--seconds", type=int, default=0, help="0 runs until stopped")
    run_parser.add_argument("--allow-process-read", action="store_true")
    map_parser = commands.add_parser("map-check", help="Geocode an address and calculate public-transit commute")
    map_parser.add_argument("address")
    map_parser.add_argument("--config", default="config.toml")
    score_parser = commands.add_parser("score-backfill", help="Calculate map and fixed scores for saved candidates")
    score_parser.add_argument("--config", default="config.toml")
    score_parser.add_argument("--output", default="data/private/candidate-enrichments.json")
    score_parser.add_argument("--force", action="store_true")
    repair_parser = commands.add_parser("repair-candidate", help="Append a reparsed correction for one saved candidate")
    repair_parser.add_argument("order_id")
    repair_parser.add_argument("--chat-id", help="Disambiguate the source group when IDs collide")
    repair_parser.add_argument("--config", default="config.toml")
    repair_parser.add_argument("--output", default="data/private/candidate-enrichments.json")
    model_reanalyze_parser = commands.add_parser(
        "model-reanalyze",
        help="Re-recognize active candidates with incomplete or unusable structured fields",
    )
    model_reanalyze_parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    if args.command == "parse":
        return _parse_command(args.path)
    if args.command == "show-orders":
        return _show_orders(args.database, args.limit)
    if args.command == "filter-file":
        return _filter_file(args.path, args.output, args.config)
    if args.command == "probe":
        try:
            if args.probe_command == "env":
                result = environment_report()
            elif args.probe_command == "sessions":
                result = list_sessions(
                    args.allow_process_read,
                    args.limit,
                    args.db_dir,
                    args.name_contains,
                )
            elif args.probe_command == "recent":
                result = recent_messages(
                    args.allow_process_read,
                    args.chat_id,
                    args.limit,
                    args.show_content,
                    args.db_dir,
                )
                if args.output:
                    output_path = Path(args.output)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            elif args.probe_command == "raw-meta":
                result = raw_message_metadata(
                    args.allow_process_read, args.chat_id, args.local_ids, args.db_dir
                )
            elif args.probe_command == "backfill":
                result = backfill_orders(
                    args.allow_process_read, args.chat_id, args.limit, args.db_dir
                )
            elif args.probe_command == "listen":
                result = listen(
                    args.allow_process_read,
                    args.chat_id,
                    args.seconds,
                    args.interval,
                    args.show_content,
                    args.db_dir,
                )
            else:
                return 1
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        try:
            service = TutorMonitorService(
                load_config(args.config), allow_process_read=args.allow_process_read
            )
            result = service.run(duration_seconds=args.seconds)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "map-check":
        config = load_config(args.config)
        enricher = AmapCommuteEnricher(config.map_settings)
        try:
            result = enricher.enrich(args.address)
        finally:
            enricher.close()
        print(json.dumps({"ok": result.get("status") == "ok", "result": result}, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 2
    if args.command == "score-backfill":
        result = backfill_candidate_scores(args.config, args.output, args.force)
        print(json.dumps({"ok": result["map_failures"] == 0, "result": result}, ensure_ascii=False, indent=2))
        return 0 if result["map_failures"] == 0 else 2
    if args.command == "repair-candidate":
        config = load_config(args.config)
        result = append_reparsed_candidate(config, args.order_id, args.output, args.chat_id)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "model-reanalyze":
        config = load_config(args.config)
        result = reanalyze_incomplete_candidates(config)
        print(json.dumps({"ok": result["errors"] == 0, "result": result}, ensure_ascii=False, indent=2))
        return 0 if result["errors"] == 0 else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
