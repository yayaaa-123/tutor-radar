from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import struct
import time
from typing import Any

from .parser import parse_orders
from .storage import MonitorStore
from .wechat_loader import load_readonly_db_module, upstream_status


from .runtime_paths import PROJECT_ROOT
PRIVATE_DIR = PROJECT_ROOT / "data" / "private"
WECHAT_CACHE = PRIVATE_DIR / "wechat_cache"
MONITOR_DB = PRIVATE_DIR / "probe.db"


def environment_report() -> dict[str, Any]:
    processes: list[dict[str, Any]] = []
    try:
        import psutil

        for process in psutil.process_iter(["pid", "name", "exe"]):
            if (process.info.get("name") or "").lower() in {"weixin.exe", "wechat.exe"}:
                processes.append(process.info)
    except Exception as exc:
        processes.append({"inspection_error": repr(exc)})

    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_bits": struct.calcsize("P") * 8,
        "wechat_processes": processes,
        "upstream": upstream_status(),
    }
    try:
        module = load_readonly_db_module()
        report["auto_detected_db_dir"] = module.auto_detect_db_dir()
        report["accounts"] = module.list_accounts(report["auto_detected_db_dir"])
    except Exception as exc:
        report["readonly_module_error"] = repr(exc)
    return report


def open_database(allow_process_read: bool, db_dir: str | None = None):
    if not allow_process_read:
        raise RuntimeError("此操作会只读扫描微信进程内存；请显式添加 --allow-process-read")
    module = load_readonly_db_module()
    WECHAT_CACHE.mkdir(parents=True, exist_ok=True)
    return module.WeChatDB(db_dir=db_dir or None, workdir=str(WECHAT_CACHE))


def list_sessions(
    allow_process_read: bool,
    limit: int,
    db_dir: str | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    db = open_database(allow_process_read, db_dir)
    output = []
    for session in db.get_sessions(limit=limit):
        chat_id = session["username"]
        name = db.get_nickname(chat_id)
        if name_contains and name_contains.casefold() not in name.casefold():
            continue
        output.append(
            {
                "chat_id": chat_id,
                "name": name,
                "unread": session.get("unread"),
                "last_time": session.get("last_time"),
                "last_sender": session.get("last_sender"),
            }
        )
    return output


def recent_messages(
    allow_process_read: bool,
    chat_id: str,
    limit: int,
    show_content: bool,
    db_dir: str | None = None,
) -> list[dict[str, Any]]:
    db = open_database(allow_process_read, db_dir)
    messages = db.get_messages(chat_id, limit=limit)
    result = []
    for message in messages:
        item = {
            "local_id": message.get("local_id"),
            "sort_seq": message.get("sort_seq"),
            "type": message.get("type"),
            "sender_id": message.get("sender_id"),
            "create_time": message.get("create_time"),
            "content_length": len(message.get("content") or ""),
            "looks_like_order": "订单" in (message.get("content") or ""),
        }
        if show_content:
            item["content"] = message.get("content")
        result.append(item)
    return result


def raw_message_metadata(
    allow_process_read: bool,
    chat_id: str,
    local_ids: list[int],
    db_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Return byte-format diagnostics without returning message bodies."""
    db = open_database(allow_process_read, db_dir)
    found = db._msg_conn(chat_id)
    if not found:
        return []
    conn, table = found
    try:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        ct_column = "WCDB_CT_message_content" if "WCDB_CT_message_content" in columns else None
        select_ct = f", {ct_column} AS compression_type" if ct_column else ", NULL AS compression_type"
        placeholders = ",".join("?" for _ in local_ids)
        rows = conn.execute(
            f"""SELECT local_id, typeof(message_content) AS storage_type,
                       length(message_content) AS byte_length,
                       hex(substr(message_content, 1, 32)) AS prefix_hex
                       {select_ct}
                FROM {table} WHERE local_id IN ({placeholders})
                ORDER BY local_id DESC""",
            tuple(local_ids),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def listen(
    allow_process_read: bool,
    chat_id: str,
    seconds: int,
    interval: float,
    show_content: bool,
    db_dir: str | None = None,
) -> dict[str, Any]:
    db = open_database(allow_process_read, db_dir)
    store = MonitorStore(MONITOR_DB)
    watermark = store.get_watermark(chat_id)
    if watermark is None:
        latest = db.get_messages(chat_id, limit=1)
        watermark = int(latest[0]["sort_seq"]) if latest else 0
        store.set_watermark(chat_id, watermark)

    deadline = time.monotonic() + seconds
    received = stored = orders = errors = 0
    while time.monotonic() < deadline:
        messages = db.get_new_messages(chat_id, since_seq=watermark, limit=200)
        for message in messages:
            received += 1
            watermark = max(watermark, int(message["sort_seq"]))
            content = message.get("content") or ""
            if "订单" in content and store.add_message(chat_id, message):
                stored += 1
                try:
                    parsed_orders = parse_orders(content)
                except ValueError:
                    errors += 1
                else:
                    for order in parsed_orders:
                        store.add_order(
                            chat_id,
                            int(message["local_id"]),
                            order.order_id,
                            order.to_dict(),
                        )
                        orders += 1
                        event = {
                            "event": "order",
                            "order_id": order.order_id,
                            "subjects": order.subjects,
                            "district": order.district,
                            "hourly_rate": [order.hourly_rate_min, order.hourly_rate_max],
                        }
                        if show_content:
                            event["content"] = order.raw_text
                        print(json.dumps(event, ensure_ascii=False), flush=True)
            store.set_watermark(chat_id, watermark)
        time.sleep(interval)

    return {
        "chat_id": chat_id,
        "seconds": seconds,
        "received": received,
        "stored": stored,
        "orders": orders,
        "parse_errors": errors,
        "watermark": watermark,
        "database": str(MONITOR_DB),
    }


def backfill_orders(
    allow_process_read: bool,
    chat_id: str,
    limit: int,
    db_dir: str | None = None,
) -> dict[str, Any]:
    db = open_database(allow_process_read, db_dir)
    store = MonitorStore(MONITOR_DB)
    messages = list(reversed(db.get_messages(chat_id, limit=limit)))
    message_count = order_count = parse_errors = 0
    summaries: list[dict[str, Any]] = []
    max_watermark = store.get_watermark(chat_id) or 0
    for message in messages:
        max_watermark = max(max_watermark, int(message["sort_seq"]))
        content = message.get("content") or ""
        if "订单" not in content:
            continue
        if not store.add_message(chat_id, message):
            continue
        message_count += 1
        try:
            extracted = parse_orders(content)
        except ValueError:
            parse_errors += 1
            continue
        for order in extracted:
            store.add_order(chat_id, int(message["local_id"]), order.order_id, order.to_dict())
            order_count += 1
            summaries.append(
                {
                    "order_id": order.order_id,
                    "district": order.district,
                    "subjects": order.subjects,
                    "student": order.student_text,
                    "hourly_rate": [order.hourly_rate_min, order.hourly_rate_max],
                }
            )
    store.set_watermark(chat_id, max_watermark)
    return {
        "scanned_messages": len(messages),
        "order_messages_saved": message_count,
        "orders_extracted": order_count,
        "parse_errors": parse_errors,
        "watermark": max_watermark,
        "orders": summaries,
        "database": str(MONITOR_DB),
    }
