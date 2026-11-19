from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json


@dataclass(frozen=True, slots=True)
class SelectedGroup:
    chat_id: str
    chat_name: str


def _group_value(item: Any, field: str) -> str:
    if isinstance(item, Mapping):
        return str(item.get(field) or "").strip()
    return str(getattr(item, field, "") or "").strip()


def _normalize_groups(items: Iterable[Any]) -> tuple[SelectedGroup, ...]:
    groups: list[SelectedGroup] = []
    seen: set[str] = set()
    for item in items:
        chat_id = _group_value(item, "chat_id")
        chat_name = _group_value(item, "chat_name") or chat_id
        if not chat_id.endswith("@chatroom"):
            raise ValueError("监控目标只能是微信群")
        if chat_id in seen:
            continue
        seen.add(chat_id)
        groups.append(SelectedGroup(chat_id=chat_id, chat_name=chat_name))
    if len(groups) > 30:
        raise ValueError("最多同时监控 30 个微信群")
    return tuple(groups)


def load_selected_groups(
    path: Path,
    default_groups: Iterable[Any] = (),
) -> tuple[SelectedGroup, ...]:
    if not path.exists():
        return _normalize_groups(default_groups)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("群聊选择文件不可读") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise ValueError("群聊选择文件格式无效")
    return _normalize_groups(payload["groups"])


def save_selected_groups(path: Path, groups: Iterable[Any]) -> tuple[SelectedGroup, ...]:
    normalized = _normalize_groups(groups)
    atomic_write_json(
        path,
        {
            "version": 1,
            "groups": [
                {"chat_id": group.chat_id, "chat_name": group.chat_name}
                for group in normalized
            ],
        },
    )
    return normalized
