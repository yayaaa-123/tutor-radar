from __future__ import annotations

import json
import hashlib
from typing import Any, Mapping


def candidate_key(chat_id: str | None, order_id: str | None) -> str:
    """Return an unambiguous stable key while preserving legacy unscoped rows."""
    order = str(order_id or "").strip()
    chat = str(chat_id or "").strip()
    if not chat:
        return order
    return json.dumps([chat, order], ensure_ascii=False, separators=(",", ":"))


def event_candidate_key(event: Mapping[str, Any]) -> str:
    return candidate_key(
        str(event.get("chat_id") or ""),
        str((event.get("order") or {}).get("order_id") or ""),
    )


def public_group_key(chat_id: str | None) -> str:
    return hashlib.sha256(str(chat_id or "legacy").encode("utf-8")).hexdigest()[:20]


def public_candidate_key(chat_id: str | None, order_id: str | None) -> str:
    order = str(order_id or "").strip()
    if not str(chat_id or "").strip():
        return order
    return hashlib.sha256(candidate_key(chat_id, order).encode("utf-8")).hexdigest()
