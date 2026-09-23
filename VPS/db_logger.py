import os
import threading
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.getenv("LOG_DIR", "."))
LOG_PATH = LOG_DIR / "Logs.txt"
_lock = threading.Lock()


def log_change(user_id: int, action: str, row_id: int, status: str = "OK") -> None:
    action = action.upper()
    status = status.upper()
    if action not in ("INSERT", "UPDATE", "DELETE"):
        return
    if status not in ("OK", "ERROR"):
        status = "ERROR"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} - {user_id} - {action} - {row_id} - {status}\n"

    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)