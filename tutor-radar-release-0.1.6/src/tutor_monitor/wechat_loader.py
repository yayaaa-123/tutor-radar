from __future__ import annotations

from importlib import metadata, util
from pathlib import Path
from types import ModuleType


DIST_NAME = "wechatauto-replica"
EXPECTED_VERSION = "1.1.9"
_MODULE_NAME = "_tutor_monitor_upstream_wechat_db"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_TEXT_BYTES = 4 * 1024 * 1024


def upstream_status() -> dict[str, str | bool]:
    try:
        distribution = metadata.distribution(DIST_NAME)
    except metadata.PackageNotFoundError:
        return {"installed": False, "expected_version": EXPECTED_VERSION}
    db_path = Path(distribution.locate_file("wechatauto/db.py")).resolve()
    return {
        "installed": True,
        "version": distribution.version,
        "expected_version": EXPECTED_VERSION,
        "version_matches": distribution.version == EXPECTED_VERSION,
        "db_module": str(db_path),
    }


def load_readonly_db_module() -> ModuleType:
    """Load only upstream db.py, bypassing package UI/sending imports."""
    status = upstream_status()
    if not status.get("installed"):
        raise RuntimeError(f"未安装 {DIST_NAME} {EXPECTED_VERSION}")
    if not status.get("version_matches"):
        raise RuntimeError(
            f"上游版本不匹配：需要 {EXPECTED_VERSION}，实际为 {status.get('version')}"
        )
    db_path = Path(str(status["db_module"]))
    spec = util.spec_from_file_location(_MODULE_NAME, db_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载只读数据库模块：{db_path}")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _install_compressed_text_decoder(module)
    return module


def _install_compressed_text_decoder(module: ModuleType) -> None:
    """Extend upstream text decoding for WCDB Zstandard content type 4."""
    original = module.WeChatDB._friendly_content

    def decode(content: bytes, message_type: str) -> str:
        if isinstance(content, bytes) and content.startswith(ZSTD_MAGIC):
            try:
                import zstandard
            except ImportError:
                return original(content, message_type)
            try:
                unpacked = zstandard.ZstdDecompressor().decompress(
                    content, max_output_size=MAX_TEXT_BYTES
                )
                text = unpacked.decode("utf-8")
                return text.rstrip("\x00") or f"[{message_type}]"
            except (UnicodeDecodeError, zstandard.ZstdError):
                pass
        return original(content, message_type)

    module.WeChatDB._friendly_content = staticmethod(decode)
