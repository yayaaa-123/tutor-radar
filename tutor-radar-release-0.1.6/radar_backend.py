"""Entry point for the frozen Windows backend."""
import sys
import time

from tutor_monitor.dashboard_api import main as serve_api
from tutor_monitor.runtime_paths import PROJECT_ROOT
from tutor_monitor.service import TutorMonitorService, load_config


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "self-test":
        from tutor_monitor.wechat_loader import load_readonly_db_module

        load_readonly_db_module()
        return 0
    if mode == "api":
        sys.argv = [sys.argv[0]]
        return serve_api()
    if mode == "monitor":
        path = PROJECT_ROOT / "config.toml"
        while True:
            try:
                if path.is_file():
                    TutorMonitorService(load_config(path), allow_process_read=True).run()
            except Exception as exc:
                print(type(exc).__name__, flush=True)
            time.sleep(5)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
