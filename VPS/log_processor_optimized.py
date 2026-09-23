import os
from pathlib import Path

ACTIONS = ("INSERT", "UPDATE", "DELETE")
STATUSES = ("OK", "ERROR")

LOG_DIR = Path(os.getenv("LOG_DIR", "."))
IN_FILE = LOG_DIR / "Logs.txt"
OUT_FILE = LOG_DIR / "Result.txt"


def _empty_counters() -> dict[str, int]:
    return {"INSERT": 0, "UPDATE": 0, "DELETE": 0}


def count_optimized(path: Path = IN_FILE) -> dict[str, dict[int, dict[str, int]]]:
    stats: dict[str, dict[int, dict[str, int]]] = {"OK": {}, "ERROR": {}}

    if not path.exists():
        return stats

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split(" - ")]
            if len(parts) != 5:
                continue

            _date, user_id_raw, action, _row_id, status = parts
            action = action.upper()
            status = status.upper()

            if action not in ACTIONS or status not in STATUSES:
                continue

            try:
                user_id = int(user_id_raw)
            except ValueError:
                continue

            counters = stats[status].setdefault(user_id, _empty_counters())
            counters[action] += 1

    return stats


def format_block(title: str, stats: dict[int, dict[str, int]]) -> list[str]:
    lines = [f"=== {title} ==="]
    for uid in sorted(stats):
        s = stats[uid]
        lines.append(
            f"User {uid}: created={s['INSERT']}, "
            f"updated={s['UPDATE']}, deleted={s['DELETE']}"
        )
    return lines


def process_logs_optimized() -> tuple[list[str], list[str]]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not IN_FILE.exists():
        IN_FILE.touch()

    log_lines = IN_FILE.read_text(encoding="utf-8").splitlines()

    stats = count_optimized(IN_FILE)

    result_lines: list[str] = []
    result_lines += format_block("SUCCESS", stats["OK"])
    result_lines.append("")
    result_lines += format_block("ERROR", stats["ERROR"])

    OUT_FILE.write_text(
        "\n".join(result_lines) + "\n",
        encoding="utf-8",
    )
    return log_lines, result_lines