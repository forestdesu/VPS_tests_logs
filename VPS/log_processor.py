import os
from pathlib import Path

ACTIONS = ("INSERT", "UPDATE", "DELETE")
STATUSES = ("OK", "ERROR")

LOG_DIR = Path(os.getenv("LOG_DIR", "."))
IN_FILE = LOG_DIR / "Logs.txt"
OUT_FILE = LOG_DIR / "Result.txt"


def parse_log(path: Path = IN_FILE) -> list[tuple[int, str, str]]:
    if not path.exists():
        return []

    records: list[tuple[int, str, str]] = []
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
            if action not in ACTIONS:
                continue
            if status not in STATUSES:
                continue
            try:
                user_id = int(user_id_raw)
            except ValueError:
                continue
            records.append((user_id, action, status))
    return records


def collect_user_ids(records: list[tuple[int, str, str]]) -> list[int]:
    unique_ids: list[int] = []
    for uid, _, _ in records:
        found = False
        for existing in unique_ids:
            if existing == uid:
                found = True
                break
        if not found:
            unique_ids.append(uid)
    return unique_ids


def count_naive(
    records: list[tuple[int, str, str]],
    user_ids: list[int],
    status_filter: str,
) -> dict[int, dict[str, int]]:

    stats: dict[int, dict[str, int]] = {}
    for uid in user_ids:
        counters = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}
        for record_uid, action, status in records:
            if record_uid == uid and status == status_filter:
                counters[action] += 1
        stats[uid] = counters
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


def process_logs() -> tuple[list[str], list[str]]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not IN_FILE.exists():
        IN_FILE.touch()

    log_lines = IN_FILE.read_text(encoding="utf-8").splitlines()

    records = parse_log(IN_FILE)
    ids = collect_user_ids(records)

    stats_ok = count_naive(records, ids, "OK")
    stats_err = count_naive(records, ids, "ERROR")

    result_lines: list[str] = []
    result_lines += format_block("SUCCESS", stats_ok)
    result_lines.append("")
    result_lines += format_block("ERROR", stats_err)

    OUT_FILE.write_text(
        "\n".join(result_lines) + "\n",
        encoding="utf-8",
    )
    return log_lines, result_lines