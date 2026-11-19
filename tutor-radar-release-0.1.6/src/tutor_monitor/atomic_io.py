from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from uuid import uuid4


Sleep = Callable[[float], None]


def _sharing_violation(error: OSError) -> bool:
    return (
        isinstance(error, PermissionError)
        or getattr(error, "winerror", None) in {5, 32, 33}
        or error.errno in {errno.EACCES, errno.EBUSY}
    )


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    attempts: int = 8,
    initial_backoff: float = 0.02,
    sleep: Sleep = time.sleep,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        last_error: OSError | None = None
        for attempt in range(max(1, attempts)):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                last_error = error
                if not _sharing_violation(error) or attempt + 1 >= max(1, attempts):
                    raise
                sleep(min(initial_backoff * (2**attempt), 0.25))
        if last_error is not None:
            raise last_error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(
    path: Path,
    content: str,
    *,
    attempts: int = 8,
    initial_backoff: float = 0.02,
    sleep: Sleep = time.sleep,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
    temporary.write_text(content, encoding="utf-8")
    try:
        for attempt in range(max(1, attempts)):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if not _sharing_violation(error) or attempt + 1 >= max(1, attempts):
                    raise
                sleep(min(initial_backoff * (2**attempt), 0.25))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
