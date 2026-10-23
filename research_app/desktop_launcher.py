from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def app_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS"))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "research_app" / "app.py"


def prepare_environment() -> Path:
    base = app_path().parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    return base


def find_free_port(start: int = 8501, end: int = 8600) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用的本地端口（8501-8600）。")


def _open_when_ready(url: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.3)


def main() -> None:
    prepare_environment()
    script = app_path()
    if not script.exists():
        raise FileNotFoundError(f"找不到应用入口：{script}")
    port = find_free_port()
    url = f"http://localhost:{port}"
    threading.Thread(
        target=_open_when_ready,
        args=(url, port),
        daemon=True,
    ).start()

    from streamlit.web import bootstrap

    options = {
        "server_port": port,
        "server_address": "localhost",
        "server_headless": True,
        "server_fileWatcherType": "none",
        "browser_serverPort": port,
        "browser_serverAddress": "localhost",
        "browser_gatherUsageStats": False,
        "global_developmentMode": False,
    }
    bootstrap.load_config_options(options)
    bootstrap.run(str(script), False, [], options)


if __name__ == "__main__":
    main()
