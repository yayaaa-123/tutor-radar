from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS messages (
    chat_id TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    sort_seq INTEGER NOT NULL,
    sender_id TEXT,
    create_time INTEGER,
    message_type TEXT,
    content TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, local_id),
    UNIQUE (chat_id, sort_seq)
);
CREATE TABLE IF NOT EXISTS orders (
    chat_id TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    parse_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, local_id, order_id)
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class MonitorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            rows = conn.execute("SELECT DISTINCT chat_id, order_id FROM orders").fetchall()
            conn.executemany(
                "INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')",
                [(f"seen_order:{row['chat_id']}:{row['order_id']}",) for row in rows],
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add_message(self, chat_id: str, message: dict[str, Any]) -> bool:
        content = message.get("content")
        if not isinstance(content, str):
            return False
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO messages
                   (chat_id, local_id, sort_seq, sender_id, create_time, message_type, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    int(message["local_id"]),
                    int(message["sort_seq"]),
                    message.get("sender_id"),
                    message.get("create_time"),
                    message.get("type"),
                    content,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def add_order(
        self,
        chat_id: str,
        local_id: int,
        order_id: str,
        parsed: dict[str, Any],
        parse_error: str | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO orders
                   (chat_id, local_id, order_id, parsed_json, parse_error)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, local_id, order_id, json.dumps(parsed, ensure_ascii=False), parse_error),
            )
            conn.commit()

    def get_watermark(self, chat_id: str) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key=?", (f"watermark:{chat_id}",)
            ).fetchone()
        return int(row["value"]) if row else None

    def set_watermark(self, chat_id: str, value: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO state(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (f"watermark:{chat_id}", str(value)),
            )
            conn.commit()

    def get_message_create_time(self, chat_id: str, sort_seq: int) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT create_time FROM messages WHERE chat_id = ? AND sort_seq = ?",
                (chat_id, sort_seq),
            ).fetchone()
        if row is None or row["create_time"] is None:
            return None
        return int(row["create_time"])

    def claim_order(self, chat_id: str, order_id: str) -> bool:
        """Return True only the first time an order ID is seen in a chat."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')",
                (f"seen_order:{chat_id}:{order_id}",),
            )
            conn.commit()
            return cursor.rowcount == 1

    def claim_candidate(self, chat_id: str, order_id: str) -> bool:
        """Return True only the first time an order passes the hard filter."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')",
                (f"seen_candidate:{chat_id}:{order_id}",),
            )
            conn.commit()
            return cursor.rowcount == 1

    def prune_orders_before(self, cutoff_epoch: int) -> dict[str, Any]:
        """Delete expired message/order payloads while preserving watermarks."""
        with closing(self._connect()) as conn:
            stale_rows = conn.execute(
                """SELECT o.chat_id, o.local_id, o.order_id
                   FROM orders o JOIN messages m
                     ON m.chat_id = o.chat_id AND m.local_id = o.local_id
                   WHERE COALESCE(m.create_time, 0) > 0 AND m.create_time < ?""",
                (cutoff_epoch,),
            ).fetchall()
            stale_messages = conn.execute(
                """SELECT chat_id, local_id FROM messages
                   WHERE COALESCE(create_time, 0) > 0 AND create_time < ?""",
                (cutoff_epoch,),
            ).fetchall()
            conn.execute(
                """DELETE FROM orders WHERE EXISTS (
                       SELECT 1 FROM messages m
                       WHERE m.chat_id = orders.chat_id AND m.local_id = orders.local_id
                         AND COALESCE(m.create_time, 0) > 0 AND m.create_time < ?
                   )""",
                (cutoff_epoch,),
            )
            conn.execute(
                "DELETE FROM messages WHERE COALESCE(create_time, 0) > 0 AND create_time < ?",
                (cutoff_epoch,),
            )
            released: list[tuple[str, str]] = []
            for row in stale_rows:
                remaining = conn.execute(
                    "SELECT 1 FROM orders WHERE chat_id = ? AND order_id = ? LIMIT 1",
                    (row["chat_id"], row["order_id"]),
                ).fetchone()
                key = (str(row["chat_id"]), str(row["order_id"]))
                if remaining is None and key not in released:
                    released.append(key)
                    conn.execute(
                        "DELETE FROM state WHERE key IN (?, ?)",
                        (f"seen_order:{key[0]}:{key[1]}", f"seen_candidate:{key[0]}:{key[1]}"),
                    )
            conn.commit()
        return {
            "messages": [(str(row["chat_id"]), int(row["local_id"])) for row in stale_messages],
            "orders_deleted": len(stale_rows),
            "claims_released": len(released),
        }
