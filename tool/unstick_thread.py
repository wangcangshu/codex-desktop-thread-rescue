import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ABORTED_TEXT = (
    "<turn_aborted>\n"
    "The user interrupted the previous turn on purpose. Any running unified exec "
    "processes may still be running in the background. If any tools/commands were "
    "aborted, they may have partially executed.\n"
    "</turn_aborted>"
)


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def normalize_path(value: str) -> str:
    value = value or ""
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return value.lower().replace("/", "\\")


def connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def select_latest_thread(conn: sqlite3.Connection, workspace_filter: str) -> dict | None:
    rows = conn.execute(
        """
        select *
        from threads
        where archived = 0
        order by coalesce(updated_at_ms, updated_at * 1000) desc
        """
    ).fetchall()
    if not rows:
        return None

    normalized_filter = normalize_path(workspace_filter)
    for row in rows:
        cwd = normalize_path(row["cwd"] or "")
        if normalized_filter and normalized_filter in cwd:
            return dict(row)

    if normalized_filter:
        return None

    return dict(rows[0])


def resolve_thread(conn: sqlite3.Connection, thread_id: str | None, title_contains: str | None):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if thread_id:
        row = cur.execute("select * from threads where id = ?", (thread_id,)).fetchone()
        if not row:
            raise SystemExit(f"thread not found: {thread_id}")
        return dict(row)

    if not title_contains:
        raise SystemExit("either --thread-id or --title-contains is required")

    rows = cur.execute(
        """
        select *
        from threads
        where title like ?
        order by coalesce(updated_at_ms, updated_at * 1000) desc
        limit 5
        """,
        (f"%{title_contains}%",),
    ).fetchall()

    if not rows:
        raise SystemExit(f"no thread matched title fragment: {title_contains}")

    return dict(rows[0])


def parse_rollout(rollout_path: Path):
    parsed = []
    with rollout_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for idx, line in enumerate(handle, 1):
            text = line.rstrip("\n")
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid jsonl at line {idx}: {exc}") from exc
            parsed.append((idx, obj))
    return parsed


def find_open_turns(parsed):
    latest_state_event = None
    open_turns = {}
    for idx, obj in parsed:
        if obj.get("type") != "event_msg":
            continue

        payload = obj.get("payload") or {}
        payload_type = payload.get("type")
        if payload_type not in {"task_started", "turn_aborted", "task_complete"}:
            continue

        latest_state_event = {
            "payload_type": payload_type,
            "turn_id": payload.get("turn_id"),
            "line": idx,
            "started_at": payload.get("started_at"),
            "timestamp": obj.get("timestamp"),
        }

        turn_id = payload.get("turn_id")
        if not turn_id:
            continue

        if payload_type == "task_started":
            open_turns[turn_id] = {
                "turn_id": turn_id,
                "line": idx,
                "started_at": payload.get("started_at"),
                "timestamp": obj.get("timestamp"),
            }
        elif payload_type in {"turn_aborted", "task_complete"}:
            open_turns.pop(turn_id, None)

    if not latest_state_event or latest_state_event["payload_type"] != "task_started":
        latest_open_turn = None
    else:
        latest_open_turn = {
            "turn_id": latest_state_event["turn_id"],
            "line": latest_state_event["line"],
            "started_at": latest_state_event.get("started_at"),
            "timestamp": latest_state_event.get("timestamp"),
        }

    open_turn_list = sorted(open_turns.values(), key=lambda item: item["line"])
    return open_turn_list, latest_open_turn


def inspect_thread(thread: dict, rollout_path: Path) -> dict:
    parsed = parse_rollout(rollout_path)
    open_turns, latest_open_turn = find_open_turns(parsed)
    stat = rollout_path.stat()
    return {
        "thread_id": thread["id"],
        "title": thread.get("title", ""),
        "rollout_path": str(rollout_path),
        "rollout_size_bytes": stat.st_size,
        "rollout_mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "status": "orphan_task_started" if open_turns else "no_open_turn",
        "open_turn": latest_open_turn,
        "open_turns": open_turns,
        "thread": thread,
    }


def backup_files(backup_dir: Path, rollout_path: Path, state_db: Path):
    backup_dir.mkdir(parents=True, exist_ok=True)
    rollout_backup = backup_dir / rollout_path.name
    state_backup = backup_dir / state_db.name
    shutil.copy2(rollout_path, rollout_backup)
    shutil.copy2(state_db, state_backup)
    return rollout_backup, state_backup


def append_abort_events(rollout_path: Path, turn_id: str, started_at: int | None):
    now_s = int(time.time())
    now_ms = int(time.time() * 1000)
    now_iso = utc_now_iso()

    duration_ms = 0
    if isinstance(started_at, int):
        duration_ms = max(0, now_ms - (started_at * 1000))

    response_item = {
        "timestamp": now_iso,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": ABORTED_TEXT}],
        },
    }
    event_msg = {
        "timestamp": now_iso,
        "type": "event_msg",
        "payload": {
            "type": "turn_aborted",
            "turn_id": turn_id,
            "reason": "interrupted",
            "completed_at": now_s,
            "duration_ms": duration_ms,
        },
    }

    with rollout_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(response_item, ensure_ascii=False) + "\n")
        handle.write(json.dumps(event_msg, ensure_ascii=False) + "\n")

    return {
        "timestamp": now_iso,
        "completed_at": now_s,
        "completed_at_ms": now_ms,
        "duration_ms": duration_ms,
    }


def append_abort_events_many(rollout_path: Path, open_turns: list[dict]):
    repairs = []
    with rollout_path.open("a", encoding="utf-8", newline="\n") as handle:
        for turn in open_turns:
            now_s = int(time.time())
            now_ms = int(time.time() * 1000)
            now_iso = utc_now_iso()

            duration_ms = 0
            started_at = turn.get("started_at")
            if isinstance(started_at, int):
                duration_ms = max(0, now_ms - (started_at * 1000))

            response_item = {
                "timestamp": now_iso,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": ABORTED_TEXT}],
                },
            }
            event_msg = {
                "timestamp": now_iso,
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "turn_id": turn["turn_id"],
                    "reason": "interrupted",
                    "completed_at": now_s,
                    "duration_ms": duration_ms,
                },
            }

            handle.write(json.dumps(response_item, ensure_ascii=False) + "\n")
            handle.write(json.dumps(event_msg, ensure_ascii=False) + "\n")
            repairs.append(
                {
                    "turn_id": turn["turn_id"],
                    "timestamp": now_iso,
                    "completed_at": now_s,
                    "completed_at_ms": now_ms,
                    "duration_ms": duration_ms,
                }
            )

    return repairs


def update_thread_timestamp(state_db: Path, thread_id: str, completed_at: int, completed_at_ms: int):
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            """
            update threads
            set updated_at = ?, updated_at_ms = ?
            where id = ?
            """,
            (completed_at, completed_at_ms, thread_id),
        )
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Recover a Codex thread whose rollout ends with an orphan task_started."
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--thread-id")
    parser.add_argument("--title-contains")
    parser.add_argument("--workspace-filter", default="")
    parser.add_argument("--all-open-turns", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        raise SystemExit(f"missing state db: {state_db}")

    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path.cwd() / "reports"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_sqlite(state_db)
    try:
        if args.thread_id or args.title_contains:
            thread = resolve_thread(conn, args.thread_id, args.title_contains)
        else:
            thread = select_latest_thread(conn, args.workspace_filter)
            if not thread:
                raise SystemExit("no matching thread found")
    finally:
        conn.close()

    rollout_path = Path(thread["rollout_path"])
    if not rollout_path.exists():
        raise SystemExit(f"missing rollout file: {rollout_path}")

    result = inspect_thread(thread, rollout_path)
    result["apply"] = args.apply

    open_turn = result["open_turn"]
    open_turns = result.get("open_turns") or []
    if not open_turns:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = output_dir / "backups" / thread["id"] / stamp
    rollout_backup, state_backup = backup_files(backup_dir, rollout_path, state_db)
    result["backup_dir"] = str(backup_dir)
    result["backup_rollout"] = str(rollout_backup)
    result["backup_state_db"] = str(state_backup)

    target_turns = open_turns if args.all_open_turns else [open_turn]
    repairs = append_abort_events_many(
        rollout_path=rollout_path,
        open_turns=target_turns,
    )
    result["repair"] = repairs[0] if len(repairs) == 1 else repairs
    result["repaired_turn_count"] = len(repairs)

    update_thread_timestamp(
        state_db=state_db,
        thread_id=thread["id"],
        completed_at=repairs[-1]["completed_at"],
        completed_at_ms=repairs[-1]["completed_at_ms"],
    )

    report_path = output_dir / f"thread_unstick_{thread['id']}.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result["report_path"] = str(report_path)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
