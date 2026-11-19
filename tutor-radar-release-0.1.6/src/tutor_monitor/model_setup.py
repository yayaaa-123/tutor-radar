import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock, Thread

import requests

from .atomic_io import atomic_write_json
from .runtime_paths import PROJECT_ROOT
from . import ollama_runtime


MODEL_NAME = "qwen3.5:4b"
MODEL_READY = PROJECT_ROOT / "data/private/local-model.json"
_lock = Lock()
_status = {"status": "idle", "percent": 0, "directory": ""}


def configured_directory() -> str:
    try:
        value = json.loads(MODEL_READY.read_text(encoding="utf-8"))
        if value.get("model") == MODEL_NAME and isinstance(value.get("directory"), str):
            return str(ollama_runtime.resolve_directory(value["directory"]))
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def model_status():
    with _lock:
        install = Path(os.environ.get("TUTOR_RADAR_INSTALL_DIR", str(PROJECT_ROOT)))
        return {**_status, "suggested_directory": configured_directory() or str(install / "models")}


def _set(status: str, percent: int = 0) -> None:
    with _lock:
        _status.update(status=status, percent=percent)


def _prepare(directory: Path, download: bool = False) -> None:
    try:
        executable = ollama_runtime.find_ollama()
        if not executable:
            _set("ollama_required")
            return
        directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=directory):
            pass
        ollama_runtime.start(executable, directory)
        with requests.Session() as session:
            session.trust_env = False
            base = ollama_runtime.ENDPOINT
            try:
                response = session.get(f"{base}/api/tags", timeout=(3, 15), allow_redirects=False)
                response.raise_for_status()
                installed = {item.get("name") for item in response.json().get("models", [])}
            except requests.RequestException:
                _set("error")
                return
            if MODEL_NAME not in installed:
                if not download:
                    _set("download_required")
                    return
                if not ollama_runtime.owns_listener():
                    raise OSError("managed_ollama_unavailable")
                _set("working")
                with session.post(f"{base}/api/pull", json={"model": MODEL_NAME, "stream": True},
                                  stream=True, timeout=(5, 120), allow_redirects=False) as response:
                    response.raise_for_status()
                    completed = False
                    for line in response.iter_lines():
                        if not line:
                            continue
                        item = json.loads(line)
                        if item.get("error"):
                            raise ValueError("pull_failed")
                        if item.get("status") == "success":
                            completed = True
                        elif item.get("total"):
                            _set("working", min(99, int(100 * item.get("completed", 0) / item["total"])))
                    if not completed:
                        raise ValueError("incomplete_pull")
        atomic_write_json(MODEL_READY, {"model": MODEL_NAME, "directory": str(directory)})
        _set("ready", 100)
    except (requests.RequestException, OSError, ValueError, RuntimeError):
        _set("error")


def start_model_setup(directory: str, *, download: bool = False) -> bool:
    resolved = ollama_runtime.resolve_directory(directory)
    with _lock:
        if _status["status"] in {"checking", "working"}:
            return False
        if download and (_status["status"] != "download_required" or _status["directory"] != str(resolved)):
            return False
        _status.update(status="checking", percent=0, directory=str(resolved))
        Thread(target=_prepare, args=(resolved, download), daemon=True).start()
        return True
