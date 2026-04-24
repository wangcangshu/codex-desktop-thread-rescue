import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    from .unstick_thread import (
        append_abort_events,
        backup_files,
        connect_sqlite,
        inspect_thread,
        select_latest_thread,
        update_thread_timestamp,
    )
    from .external_compact_fallback import run_external_compact_fallback
except ImportError:
    from unstick_thread import (
        append_abort_events,
        backup_files,
        connect_sqlite,
        inspect_thread,
        select_latest_thread,
        update_thread_timestamp,
    )
    from external_compact_fallback import run_external_compact_fallback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conservative auto-repair for stuck Codex threads with orphan task_started state."
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workspace-filter", default="")
    parser.add_argument("--min-open-seconds", type=int, default=180)
    parser.add_argument("--observe-seconds", type=int, default=120)
    parser.add_argument("--cooldown-seconds", type=int, default=1800)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-live-repair", action="store_true")
    parser.add_argument("--node-command", default="node")
    parser.add_argument("--ipc-timeout-ms", type=int, default=12000)
    parser.add_argument("--ipc-settle-seconds", type=int, default=8)
    parser.add_argument("--compact-timeout-seconds", type=int, default=120)
    parser.add_argument("--force-on-first-observation", action="store_true")
    return parser.parse_args()


def now_s() -> int:
    return int(time.time())


def is_codex_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import subprocess

        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process | Where-Object { $_.ProcessName -in @('Codex','codex') } | Select-Object -First 1 -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    acquired = False
    try:
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age > 3600:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        acquired = True
        yield
    finally:
        if fd is not None:
            os.close(fd)
        if acquired:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(info: dict) -> str:
    open_turn = info.get("open_turn") or {}
    return "|".join(
        [
            info.get("thread_id", ""),
            open_turn.get("turn_id", ""),
            str(open_turn.get("line", "")),
            str(info.get("rollout_size_bytes", "")),
            str(info.get("rollout_mtime_ns", "")),
        ]
    )


def append_action_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_json_line(text: str):
    for line in reversed([line.strip() for line in text.splitlines() if line.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def run_ipc_action(
    *,
    action: str,
    thread_id: str,
    node_command: str,
    timeout_ms: int,
) -> dict:
    helper_path = Path(__file__).resolve().parent / "codex_ipc_control.js"
    if not helper_path.exists():
        return {
            "ok": False,
            "error": "missing_helper",
            "helper_path": str(helper_path),
        }

    command = [
        node_command,
        str(helper_path),
        action,
        thread_id,
        "--timeout-ms",
        str(timeout_ms),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(10, int(timeout_ms / 1000) + 5),
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "spawn_failed",
            "command": command,
            "exception": str(exc),
        }

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    payload = parse_json_line(stdout)

    return {
        "ok": bool(result.returncode == 0 and isinstance(payload, dict) and payload.get("status") == "success"),
        "action": action,
        "command": command,
        "exit_code": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "payload": payload,
    }


def run_live_interrupt(
    *,
    thread_id: str,
    node_command: str,
    timeout_ms: int,
) -> dict:
    return run_ipc_action(
        action="interrupt",
        thread_id=thread_id,
        node_command=node_command,
        timeout_ms=timeout_ms,
    )


def run_live_compact(
    *,
    thread_id: str,
    node_command: str,
    timeout_ms: int,
) -> dict:
    return run_ipc_action(
        action="compact",
        thread_id=thread_id,
        node_command=node_command,
        timeout_ms=timeout_ms,
    )


def classify_live_interrupt_failure(live_result: dict) -> str:
    attempts = live_result.get("attempts") or []
    if attempts:
        nested = attempts[-1].get("live_interrupt")
        if isinstance(nested, dict) and nested is not live_result:
            nested_reason = classify_live_interrupt_failure(nested)
            if nested_reason != "unknown":
                return nested_reason

    payload = live_result.get("payload") or {}
    response = payload.get("response") or {}

    if response.get("error"):
        return str(response["error"])
    if payload.get("error"):
        return str(payload["error"])
    if live_result.get("error"):
        return str(live_result["error"])
    if payload.get("status") and payload.get("status") != "success":
        return str(payload["status"])
    return "unknown"


def live_failure_can_fallback(live_result: dict, post_info: dict | None) -> bool:
    if not post_info or post_info.get("status") != "orphan_task_started":
        return False
    return classify_live_interrupt_failure(live_result) in {"no-client-found"}


def compact_attempt_models(thread: dict) -> list[str]:
    original_model = str(thread.get("model") or "").strip() or "gpt-5.4"
    if original_model == "gpt-5.5":
        return ["gpt-5.4"]
    if original_model == "gpt-5.4":
        return ["gpt-5.4"]
    return [original_model, "gpt-5.4"]


def run_compact_assist_until_clear(
    *,
    codex_home: Path,
    thread: dict,
    rollout_path: Path,
    output_dir: Path,
    timeout_seconds: int,
    settle_seconds: int = 45,
    poll_interval_seconds: int = 3,
) -> dict:
    attempts = []

    for compact_model in compact_attempt_models(thread):
        compact_result = run_external_compact_fallback(
            codex_home=codex_home,
            thread_id=thread["id"],
            fallback_model=compact_model,
            timeout_seconds=timeout_seconds,
            output_dir=output_dir,
        )
        post_info = inspect_thread(thread, rollout_path)
        compact_outcome = (compact_result.get("compact_outcome") or {})
        compact_succeeded = bool(
            compact_result.get("status") == "compact_succeeded" or compact_outcome.get("ok")
        )
        started_compaction = any(
            ((n.get("method") == "item/started") and (((n.get("params") or {}).get("item") or {}).get("type") == "contextCompaction"))
            for n in (compact_outcome.get("notifications") or [])
        )
        settle_probes = []
        if post_info["status"] == "orphan_task_started" and started_compaction and settle_seconds > 0:
            deadline = time.time() + max(0, settle_seconds)
            while time.time() < deadline:
                remaining = deadline - time.time()
                time.sleep(min(max(0.2, poll_interval_seconds), remaining))
                probe = inspect_thread(thread, rollout_path)
                settle_probes.append(
                    {
                        "status": probe["status"],
                        "open_turns": probe.get("open_turns") or [],
                    }
                )
                post_info = probe
                if post_info["status"] != "orphan_task_started":
                    break
        attempt = {
            "compact_model": compact_model,
            "compact_result": compact_result,
            "compact_succeeded": compact_succeeded,
            "started_compaction": started_compaction,
            "settle_probes": settle_probes,
            "post_compact_status": post_info["status"],
            "post_compact_open_turn": post_info.get("open_turn"),
        }
        attempts.append(attempt)

        if post_info["status"] != "orphan_task_started":
            return {
                "ok": True,
                "status": "cleared_by_manual_compact",
                "attempts": attempts,
                "final_info": post_info,
            }
        if compact_succeeded:
            return {
                "ok": True,
                "status": "manual_compact_succeeded_but_open_turn_remains",
                "attempts": attempts,
                "final_info": post_info,
            }

    return {
        "ok": False,
        "status": "manual_compact_did_not_clear",
        "attempts": attempts,
        "final_info": inspect_thread(thread, rollout_path),
    }


def run_live_interrupt_until_stable(
    *,
    thread: dict,
    rollout_path: Path,
    node_command: str,
    timeout_ms: int,
    settle_seconds: int,
    stability_window_seconds: int = 12,
    poll_interval_seconds: int = 2,
    max_attempts: int = 3,
) -> dict:
    attempts = []

    for attempt_number in range(1, max_attempts + 1):
        before = inspect_thread(thread, rollout_path)
        attempt = {
            "attempt": attempt_number,
            "before_status": before["status"],
            "before_open_turns": before.get("open_turns") or [],
        }

        if before["status"] != "orphan_task_started":
            attempt["result"] = "already_clear"
            attempts.append(attempt)
            return {
                "ok": True,
                "status": "already_clear" if attempt_number == 1 else "cleared_after_retry",
                "attempts": attempts,
                "final_info": before,
            }

        live_result = run_live_interrupt(
            thread_id=thread["id"],
            node_command=node_command,
            timeout_ms=timeout_ms,
        )
        attempt["live_interrupt"] = live_result
        if not live_result.get("ok"):
            attempt["result"] = "interrupt_failed"
            attempts.append(attempt)
            return {
                "ok": False,
                "status": "interrupt_failed",
                "attempts": attempts,
                "final_info": inspect_thread(thread, rollout_path),
            }

        if settle_seconds > 0:
            time.sleep(settle_seconds)

        after_settle = inspect_thread(thread, rollout_path)
        attempt["after_settle_status"] = after_settle["status"]
        attempt["after_settle_open_turns"] = after_settle.get("open_turns") or []

        if after_settle["status"] == "orphan_task_started":
            attempt["result"] = "still_open_after_settle"
            attempts.append(attempt)
            continue

        stability_probes = []
        reopened = None
        deadline = time.time() + max(0, stability_window_seconds)
        while time.time() < deadline:
            remaining = deadline - time.time()
            time.sleep(min(max(0.1, poll_interval_seconds), remaining))
            probe = inspect_thread(thread, rollout_path)
            stability_probes.append(
                {
                    "status": probe["status"],
                    "open_turns": probe.get("open_turns") or [],
                }
            )
            if probe["status"] == "orphan_task_started":
                reopened = probe
                break

        attempt["stability_probes"] = stability_probes
        if reopened is not None:
            attempt["result"] = "reopened_within_stability_window"
            attempt["reopened_open_turns"] = reopened.get("open_turns") or []
            attempts.append(attempt)
            continue

        final_info = inspect_thread(thread, rollout_path)
        attempt["result"] = "stable_clear"
        attempts.append(attempt)
        return {
            "ok": final_info["status"] != "orphan_task_started",
            "status": "stable_clear" if attempt_number == 1 else "stable_clear_after_retry",
            "attempts": attempts,
            "final_info": final_info,
        }

    final_info = inspect_thread(thread, rollout_path)
    return {
        "ok": final_info["status"] != "orphan_task_started",
        "status": "max_attempts_exceeded",
        "attempts": attempts,
        "final_info": final_info,
    }


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    state_dir = output_dir / "state"
    observation_path = state_dir / "auto_repair_observations.json"
    action_log_path = output_dir / "auto_repair_actions.jsonl"
    lock_path = state_dir / "auto_repair.lock"
    state_db = codex_home / "state_5.sqlite"

    report = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "workspace_filter": args.workspace_filter,
        "apply": args.apply,
        "allow_live_repair": args.allow_live_repair,
        "status": "unknown",
        "decision": "none",
    }

    if not state_db.exists():
        report["status"] = "missing_state_db"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 20

    try:
        with file_lock(lock_path):
            observations = load_json(observation_path, {})
            conn = connect_sqlite(state_db)
            try:
                thread = select_latest_thread(conn, args.workspace_filter)
            finally:
                conn.close()

            if not thread:
                report["status"] = "no_thread"
                save_json(observation_path, observations)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            rollout_path = Path(thread["rollout_path"])
            if not rollout_path.exists():
                report["status"] = "missing_rollout"
                report["thread_id"] = thread["id"]
                save_json(observation_path, observations)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            info = inspect_thread(thread, rollout_path)
            report["thread_id"] = info["thread_id"]
            report["title"] = info["title"]
            report["rollout_path"] = info["rollout_path"]
            report["inspect_status"] = info["status"]
            report["open_turn"] = info.get("open_turn")

            thread_id = info["thread_id"]
            current_fp = fingerprint(info)
            existing = observations.get(thread_id)
            current_time = now_s()

            if info["status"] != "orphan_task_started":
                report["status"] = "healthy"
                report["decision"] = "clear_observation"
                if thread_id in observations:
                    observations.pop(thread_id, None)
                    save_json(observation_path, observations)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            open_turn = info["open_turn"] or {}
            started_at = int(open_turn.get("started_at") or 0)
            open_age = max(0, current_time - started_at) if started_at else None
            report["open_age_seconds"] = open_age

            if not existing or existing.get("fingerprint") != current_fp:
                observations[thread_id] = {
                    "fingerprint": current_fp,
                    "first_seen_at": current_time,
                    "last_seen_at": current_time,
                    "last_repaired_at": existing.get("last_repaired_at") if existing else None,
                }
                save_json(observation_path, observations)
                existing = observations[thread_id]
                if not args.force_on_first_observation:
                    report["status"] = "observing"
                    report["decision"] = "wait_for_second_observation"
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return 0

            existing["last_seen_at"] = current_time
            observed_for = current_time - int(existing.get("first_seen_at") or current_time)
            report["observed_for_seconds"] = observed_for

            if open_age is not None and open_age < int(args.min_open_seconds):
                observations[thread_id] = existing
                save_json(observation_path, observations)
                report["status"] = "observing"
                report["decision"] = "open_turn_too_young"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            if observed_for < int(args.observe_seconds):
                observations[thread_id] = existing
                save_json(observation_path, observations)
                report["status"] = "observing"
                report["decision"] = "waiting_stability_window"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            last_repaired_at = int(existing.get("last_repaired_at") or 0)
            cooldown_left = (last_repaired_at + int(args.cooldown_seconds)) - current_time
            if last_repaired_at and cooldown_left > 0:
                observations[thread_id] = existing
                save_json(observation_path, observations)
                report["status"] = "cooldown"
                report["decision"] = "skip_recent_repair"
                report["cooldown_left_seconds"] = cooldown_left
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            codex_running = is_codex_running()
            report["codex_running"] = codex_running

            if not args.apply:
                observations[thread_id] = existing
                save_json(observation_path, observations)
                report["status"] = "repairable_compact_first"
                report["decision"] = "dry_run_only_compact_first"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            compact_result = run_compact_assist_until_clear(
                codex_home=codex_home,
                thread=thread,
                rollout_path=rollout_path,
                output_dir=output_dir,
                timeout_seconds=args.compact_timeout_seconds,
            )
            report["compact_assist"] = compact_result
            observations[thread_id] = existing
            save_json(observation_path, observations)

            post_compact = compact_result.get("final_info") or inspect_thread(thread, rollout_path)
            report["post_compact_status"] = post_compact["status"]
            report["post_compact_open_turn"] = post_compact.get("open_turn")

            if compact_result.get("ok"):
                existing["last_repaired_at"] = current_time
                existing["last_repaired_turn_id"] = open_turn["turn_id"]
                existing["last_compact_assist_at"] = current_time
                observations[thread_id] = existing
                save_json(observation_path, observations)

                action = {
                    "timestamp": report["timestamp"],
                    "thread_id": thread_id,
                    "title": report["title"],
                    "turn_id": open_turn["turn_id"],
                    "repair": {
                        "mode": "manual_compact_assist",
                        "result": compact_result,
                    },
                    "workspace_filter": args.workspace_filter,
                    "live_repair": False,
                }
                append_action_log(action_log_path, action)
                report["action"] = action
                report["status"] = "repaired"
                report["decision"] = "manual_compact_assist"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            if codex_running and not args.allow_live_repair:
                observations[thread_id] = existing
                save_json(observation_path, observations)
                report["status"] = "blocked"
                report["decision"] = "compact_failed_live_repair_disabled"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            if codex_running and args.allow_live_repair:
                live_result = run_live_interrupt_until_stable(
                    thread=thread,
                    rollout_path=rollout_path,
                    node_command=args.node_command,
                    timeout_ms=args.ipc_timeout_ms,
                    settle_seconds=args.ipc_settle_seconds,
                )
                report["live_ipc"] = live_result
                observations[thread_id] = existing
                save_json(observation_path, observations)

                if not live_result.get("ok"):
                    post_info = live_result.get("final_info") or inspect_thread(thread, rollout_path)
                    failure_reason = classify_live_interrupt_failure(live_result)
                    report["post_interrupt_status"] = post_info["status"]
                    report["post_interrupt_open_turn"] = post_info.get("open_turn")
                    report["live_interrupt_failure_reason"] = failure_reason
                    if live_failure_can_fallback(live_result, post_info):
                        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                        backup_dir = output_dir / "backups" / thread_id / stamp
                        rollout_backup, state_backup = backup_files(backup_dir, rollout_path, state_db)
                        append_info = append_abort_events(
                            rollout_path=rollout_path,
                            turn_id=post_info["open_turn"]["turn_id"],
                            started_at=post_info["open_turn"].get("started_at"),
                        )
                        update_thread_timestamp(
                            state_db=state_db,
                            thread_id=thread_id,
                            completed_at=append_info["completed_at"],
                            completed_at_ms=append_info["completed_at_ms"],
                        )

                        existing["last_repaired_at"] = current_time
                        existing["last_repaired_turn_id"] = post_info["open_turn"]["turn_id"]
                        existing["last_live_interrupt_at"] = current_time
                        observations[thread_id] = existing
                        save_json(observation_path, observations)

                        action = {
                            "timestamp": report["timestamp"],
                            "thread_id": thread_id,
                            "title": report["title"],
                            "turn_id": post_info["open_turn"]["turn_id"],
                            "backup_dir": str(backup_dir),
                            "backup_rollout": str(rollout_backup),
                            "backup_state_db": str(state_backup),
                            "repair": {
                                "mode": "fallback_after_no_client",
                                "live_result": live_result,
                                "append_info": append_info,
                            },
                            "workspace_filter": args.workspace_filter,
                            "live_repair": True,
                        }
                        append_action_log(action_log_path, action)
                        report["action"] = action
                        report["post_interrupt_status"] = "no_open_turn"
                        report["post_interrupt_open_turn"] = None
                        report["status"] = "repaired"
                        report["decision"] = "fallback_after_no_client"
                        print(json.dumps(report, ensure_ascii=False, indent=2))
                        return 0

                    report["status"] = (
                        "live_interrupt_still_reopening"
                        if post_info["status"] == "orphan_task_started"
                        else "live_interrupt_failed"
                    )
                    report["decision"] = (
                        "ipc_interrupt_reopened"
                        if report["status"] == "live_interrupt_still_reopening"
                        else "ipc_interrupt_failed"
                    )
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return 0

                existing["last_repaired_at"] = current_time
                existing["last_repaired_turn_id"] = open_turn["turn_id"]
                existing["last_live_interrupt_at"] = current_time
                observations[thread_id] = existing
                save_json(observation_path, observations)

                action = {
                    "timestamp": report["timestamp"],
                    "thread_id": thread_id,
                    "title": report["title"],
                    "turn_id": open_turn["turn_id"],
                    "repair": {
                        "mode": "ipc_interrupt",
                        "result": live_result,
                    },
                    "workspace_filter": args.workspace_filter,
                    "live_repair": True,
                }
                append_action_log(action_log_path, action)

                post_info = live_result.get("final_info") or inspect_thread(thread, rollout_path)
                report["post_interrupt_status"] = post_info["status"]
                report["post_interrupt_open_turn"] = post_info.get("open_turn")
                report["action"] = action
                if post_info["status"] != "orphan_task_started":
                    report["status"] = "repaired"
                    report["decision"] = "ipc_interrupt"
                else:
                    report["status"] = "live_interrupt_sent"
                    report["decision"] = "ipc_interrupt_waiting_followup"
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_dir = output_dir / "backups" / thread_id / stamp
            rollout_backup, state_backup = backup_files(backup_dir, rollout_path, state_db)
            append_info = append_abort_events(
                rollout_path=rollout_path,
                turn_id=open_turn["turn_id"],
                started_at=open_turn.get("started_at"),
            )
            update_thread_timestamp(
                state_db=state_db,
                thread_id=thread_id,
                completed_at=append_info["completed_at"],
                completed_at_ms=append_info["completed_at_ms"],
            )

            existing["last_repaired_at"] = current_time
            existing["last_repaired_turn_id"] = open_turn["turn_id"]
            observations[thread_id] = existing
            save_json(observation_path, observations)

            action = {
                "timestamp": report["timestamp"],
                "thread_id": thread_id,
                "title": report["title"],
                "turn_id": open_turn["turn_id"],
                "backup_dir": str(backup_dir),
                "backup_rollout": str(rollout_backup),
                "backup_state_db": str(state_backup),
                "repair": append_info,
                "workspace_filter": args.workspace_filter,
                "live_repair": bool(codex_running),
            }
            append_action_log(action_log_path, action)

            report["status"] = "repaired"
            report["decision"] = "appended_turn_aborted"
            report["action"] = action
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
    except FileExistsError:
        report["status"] = "busy"
        report["decision"] = "lock_held"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())
