from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from .auto_repair import (
        classify_live_interrupt_failure,
        is_codex_running,
        live_failure_can_fallback,
        run_live_interrupt_until_stable,
    )
    from .external_compact_fallback import run_external_compact_fallback
    from .unstick_thread import (
        append_abort_events_many,
        backup_files,
        connect_sqlite,
        inspect_thread,
        normalize_path,
        update_thread_timestamp,
    )
except ImportError:
    from auto_repair import (
        classify_live_interrupt_failure,
        is_codex_running,
        live_failure_can_fallback,
        run_live_interrupt_until_stable,
    )
    from external_compact_fallback import run_external_compact_fallback
    from unstick_thread import (
        append_abort_events_many,
        backup_files,
        connect_sqlite,
        inspect_thread,
        normalize_path,
        update_thread_timestamp,
    )


def bi(zh: str, en: str) -> str:
    return f"{zh} / {en}"


APP_TITLE = bi("Codex 线程修复器", "Codex Thread Rescue")
DEFAULT_LIMIT = 25
DEFAULT_STUCK_SECONDS = 180
DEFAULT_SETTLE_SECONDS = 6
DEFAULT_TIMEOUT_MS = 12000
DEFAULT_COMPACT_SETTLE_SECONDS = 45

FRONTEND_REFRESH_TIP = bi(
    "重要提示：只有在工具已经修复成功，并且再次刷新检查后确认这个线程已经没有 open turn 的情况下，才适合回到原聊天里发一句很短的话，比如“继续”，来刷新前端显示。如果线程仍然显示 open turn，或者你一发消息它又开始压缩，就不要把发消息当成刷新手段。",
    "Important: only use a short follow-up message such as 'continue' when the tool has already repaired the thread and a fresh re-check shows no open turn. If the thread still shows an open turn, or a new message immediately starts compaction again, do not use a new message as a refresh trick.",
)

STATUS_TEXT = {
    "healthy": bi("正常", "Healthy"),
    "stuck": bi("可能卡死", "Likely Stuck"),
    "active": bi("有未完成回合", "Open Turn"),
    "error": bi("检查失败", "Inspect Error"),
}

COMPACT_HTTP_STATUS_RE = re.compile(r"http\.response\.status_code=(\d+)")
COMPACT_SEND_ERROR_TEXT = "error sending request for url (https://chatgpt.com/backend-api/codex/responses/compact)"


def utc_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def pretty_path(value: str) -> str:
    value = value or ""
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return value


def basename_or_path(value: str) -> str:
    clean = pretty_path(value)
    if not clean:
        return ""
    return os.path.basename(clean.rstrip("\\/")) or clean


def format_local_timestamp(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, remain = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remain}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def now_seconds() -> int:
    return int(time.time())


def ensure_output_dir() -> Path:
    output_dir = Path(__file__).resolve().parent.parent / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass
class ThreadSummary:
    thread_id: str
    title: str
    cwd: str
    cwd_display: str
    updated_at_ms: int
    updated_text: str
    model: str
    reasoning_effort: str
    rollout_path: str
    inspect_status: str
    status_label: str
    status_rank: str
    open_age_seconds: int | None
    open_age_text: str
    rollout_idle_seconds: int | None
    open_turn: dict | None
    open_turns: list[dict]
    compact_event_count: int
    compact_http_500_count: int
    compact_send_error_count: int
    compact_state_label: str
    compact_state_time_text: str
    compact_state_detail: str
    risk_label: str
    risk_rank: str
    risk_reason: str
    raw_thread: dict


def load_thread_compact_stats(codex_home: Path, thread_id: str) -> dict:
    logs_db = codex_home / "logs_2.sqlite"
    stats = {
        "compact_event_count": 0,
        "compact_http_500_count": 0,
        "compact_send_error_count": 0,
    }
    if not logs_db.exists():
        return stats

    conn = sqlite3.connect(str(logs_db))
    try:
        conn.row_factory = sqlite3.Row
        stats["compact_event_count"] = int(
            (
                conn.execute(
                    "select count(*) as n from logs where thread_id = ? and feedback_log_body like ?",
                    (thread_id, '%api.path="responses/compact"%'),
                ).fetchone()
                or {"n": 0}
            )["n"]
        )
        stats["compact_http_500_count"] = int(
            (
                conn.execute(
                    "select count(*) as n from logs where thread_id = ? and feedback_log_body like ?",
                    (thread_id, "%http.response.status_code=500%"),
                ).fetchone()
                or {"n": 0}
            )["n"]
        )
        stats["compact_send_error_count"] = int(
            (
                conn.execute(
                    "select count(*) as n from logs where thread_id = ? and feedback_log_body like ?",
                    (
                        thread_id,
                        "%error sending request for url (https://chatgpt.com/backend-api/codex/responses/compact)%",
                    ),
                ).fetchone()
                or {"n": 0}
            )["n"]
        )
    except Exception:
        return stats
    finally:
        conn.close()

    return stats


def format_log_timestamp(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def summarize_compact_error_message(message: str) -> str:
    message = (message or "").strip()
    if not message:
        return "-"
    if "does not exist or you do not have access to it" in message:
        match = re.search(r"The model `([^`]+)` does not exist or you do not have access to it", message)
        if match:
            return bi(
                f"远端 compact 失败：模型 {match.group(1)} 不可用或当前账号无权限。",
                f"Remote compact failed: model {match.group(1)} is unavailable or not accessible for this account.",
            )
        return bi(
            "远端 compact 失败：模型不可用或当前账号无权限。",
            "Remote compact failed: the model is unavailable or not accessible for this account.",
        )
    if "unexpected status 404 Not Found" in message:
        return bi(
            "远端 compact 返回 404。",
            "Remote compact returned 404.",
        )
    if "unexpected status 403 Forbidden" in message:
        return bi(
            "远端 compact 返回 403。",
            "Remote compact returned 403.",
        )
    if "unexpected status 500" in message or "http 500" in message:
        return bi(
            "远端 compact 返回 500。",
            "Remote compact returned 500.",
        )
    return message.split(", url:", 1)[0].strip()


def load_rollout_compact_state(rollout_path: Path) -> dict | None:
    if not rollout_path.exists():
        return None

    tail: deque[str] = deque(maxlen=400)
    try:
        with rollout_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                text = line.rstrip("\n")
                if text:
                    tail.append(text)
    except Exception:
        return None

    for text in reversed(tail):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue

        if obj.get("type") != "event_msg":
            continue

        payload = obj.get("payload") or {}
        if payload.get("type") != "error":
            continue

        message = (payload.get("message") or "").strip()
        if "compact" not in message.lower():
            continue

        return {
            "label": bi("最近一次 compact 远端失败", "Latest compact failed remotely"),
            "time_text": format_local_timestamp(int(datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp() * 1000))
            if obj.get("timestamp")
            else "-",
            "detail": summarize_compact_error_message(message),
        }

    return None


def load_thread_compact_state(codex_home: Path, thread_id: str, rollout_path: Path) -> dict:
    rollout_state = load_rollout_compact_state(rollout_path)
    if rollout_state:
        return rollout_state

    logs_db = codex_home / "logs_2.sqlite"
    default_state = {
        "label": bi("未发现最近 compact 结果", "No recent compact result"),
        "time_text": "-",
        "detail": bi("最近没有看到明确的 compact 成功或失败记录。", "No recent compact success or failure record was found."),
    }
    if not logs_db.exists():
        return default_state

    conn = sqlite3.connect(str(logs_db))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select ts, feedback_log_body
            from logs
            where thread_id = ?
              and (
                feedback_log_body like ?
                or feedback_log_body like ?
                or feedback_log_body like ?
              )
            order by ts desc, id desc
            limit 20
            """,
            (
                thread_id,
                '%api.path="responses/compact"%',
                "%http.response.status_code=%",
                f"%{COMPACT_SEND_ERROR_TEXT}%",
            ),
        ).fetchall()
    except Exception:
        conn.close()
        return default_state
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for row in rows:
        body = row["feedback_log_body"] or ""
        time_text = format_log_timestamp(int(row["ts"]))
        if COMPACT_SEND_ERROR_TEXT in body:
            return {
                "label": bi("最近一次 compact 发送失败", "Latest compact send failed"),
                "time_text": time_text,
                "detail": bi(
                    "请求已经发起，但到 chatgpt.com 的 compact 请求没有成功发出去。",
                    "The compact request was started but was not sent successfully to chatgpt.com.",
                ),
            }

        match = COMPACT_HTTP_STATUS_RE.search(body)
        if match:
            status_code = int(match.group(1))
            if status_code == 200:
                return {
                    "label": bi("最近一次 compact 成功", "Latest compact succeeded"),
                    "time_text": time_text,
                    "detail": bi("最近一次 compact 请求返回了 200。", "The latest compact request returned 200."),
                }
            return {
                "label": bi(f"最近一次 compact 返回 {status_code}", f"Latest compact returned {status_code}"),
                "time_text": time_text,
                "detail": bi(
                    f"最近一次 compact 请求返回了 HTTP {status_code}。",
                    f"The latest compact request returned HTTP {status_code}.",
                ),
            }

    if rows:
        return {
            "label": bi("最近触发过 compact", "Recent compact activity"),
            "time_text": format_log_timestamp(int(rows[0]["ts"])),
            "detail": bi(
                "看到了 compact 链路活动，但最近几条日志里没有明确成功或失败结果。",
                "Compact activity was seen, but the latest log entries did not contain a clear success or failure outcome.",
            ),
        }

    return default_state


def assess_thread_risk(
    *,
    model: str,
    reasoning_effort: str,
    inspect_status: str,
    status_rank: str,
    compact_event_count: int,
    compact_http_500_count: int,
    compact_send_error_count: int,
) -> tuple[str, str, str]:
    reasons: list[str] = []
    score = 0

    if model == "gpt-5.5" and reasoning_effort.lower() == "xhigh":
        score += 2
        reasons.append(bi("`gpt-5.5 xhigh` 更容易把长线程推回 compact。", "`gpt-5.5 xhigh` is more likely to push long threads back into compaction."))

    if compact_event_count >= 20:
        score += 2
        reasons.append(bi(f"这条线程已经出现很多次 compact 相关事件（{compact_event_count} 次）。", f"This thread has already produced many compact-related events ({compact_event_count})."))

    if compact_http_500_count > 0:
        score += 3
        reasons.append(bi(f"这条线程已经出现 compact `HTTP 500`（{compact_http_500_count} 次）。", f"This thread has already hit compact `HTTP 500` ({compact_http_500_count} times)."))

    if compact_send_error_count > 0:
        score += 3
        reasons.append(bi(f"这条线程已经出现 compact 发送失败（{compact_send_error_count} 次）。", f"This thread has already hit compact send errors ({compact_send_error_count} times)."))

    if inspect_status == "orphan_task_started" or status_rank == "stuck":
        score += 2
        reasons.append(bi("它当前已经进入悬空 turn / 卡住状态。", "It is currently in an open-turn / stuck state."))

    if score >= 6:
        return (
            bi("高复发风险", "High recurrence risk"),
            "high",
            " ".join(reasons),
        )
    if score >= 3:
        return (
            bi("中等复发风险", "Medium recurrence risk"),
            "medium",
            " ".join(reasons),
        )
    return (
        bi("较低复发风险", "Lower recurrence risk"),
        "low",
        bi("目前没有看到特别明显的高危信号。", "No strong high-risk signals are visible right now."),
    )


def load_thread_rows(
    *,
    codex_home: Path,
    limit: int,
    workspace_filter: str,
    title_filter: str,
    stuck_seconds: int,
    only_stuck: bool,
) -> list[ThreadSummary]:
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        raise FileNotFoundError(f"Missing state db: {state_db}")

    conn = connect_sqlite(state_db)
    try:
        rows = conn.execute(
            """
            select *
            from threads
            where archived = 0
            order by coalesce(updated_at_ms, updated_at * 1000) desc
            limit ?
            """,
            (max(limit * 4, limit),),
        ).fetchall()
    finally:
        conn.close()

    normalized_workspace = normalize_path(workspace_filter)
    lowered_title = title_filter.strip().lower()
    summaries: list[ThreadSummary] = []
    current_time = now_seconds()

    for row in rows:
        thread = dict(row)
        thread_cwd = pretty_path(thread.get("cwd") or "")
        normalized_cwd = normalize_path(thread_cwd)
        title = thread.get("title") or bi("未命名", "untitled")

        if normalized_workspace and normalized_workspace not in normalized_cwd:
            continue
        if lowered_title and lowered_title not in title.lower():
            continue

        rollout_path = Path(thread["rollout_path"])
        inspect_status = "inspect_error"
        status_rank = "error"
        status_label = STATUS_TEXT["error"]
        open_turn = None
        open_turns: list[dict] = []
        open_age_seconds = None
        rollout_idle_seconds = None
        compact_stats = load_thread_compact_stats(codex_home, thread["id"])
        compact_state = load_thread_compact_state(codex_home, thread["id"], rollout_path)

        if rollout_path.exists():
            try:
                info = inspect_thread(thread, rollout_path)
                inspect_status = info["status"]
                open_turn = info.get("open_turn")
                open_turns = info.get("open_turns") or []
                rollout_idle_seconds = max(
                    0,
                    int(time.time() - rollout_path.stat().st_mtime),
                )
                if open_turn and isinstance(open_turn.get("started_at"), int):
                    open_age_seconds = max(0, current_time - int(open_turn["started_at"]))

                if inspect_status == "no_open_turn":
                    status_rank = "healthy"
                    status_label = STATUS_TEXT["healthy"]
                else:
                    is_stuck = (
                        open_age_seconds is not None
                        and open_age_seconds >= stuck_seconds
                        and (rollout_idle_seconds or 0) >= 30
                    )
                    if is_stuck:
                        status_rank = "stuck"
                        status_label = STATUS_TEXT["stuck"]
                    else:
                        status_rank = "active"
                        status_label = STATUS_TEXT["active"]
            except Exception:
                inspect_status = "inspect_error"
                status_rank = "error"
                status_label = STATUS_TEXT["error"]

        risk_label, risk_rank, risk_reason = assess_thread_risk(
            model=thread.get("model") or "",
            reasoning_effort=thread.get("reasoning_effort") or "",
            inspect_status=inspect_status,
            status_rank=status_rank,
            compact_event_count=compact_stats["compact_event_count"],
            compact_http_500_count=compact_stats["compact_http_500_count"],
            compact_send_error_count=compact_stats["compact_send_error_count"],
        )

        summary = ThreadSummary(
            thread_id=thread["id"],
            title=title,
            cwd=thread_cwd,
            cwd_display=basename_or_path(thread_cwd),
            updated_at_ms=int(thread.get("updated_at_ms") or (thread.get("updated_at") or 0) * 1000),
            updated_text=format_local_timestamp(
                int(thread.get("updated_at_ms") or (thread.get("updated_at") or 0) * 1000)
            ),
            model=thread.get("model") or "",
            reasoning_effort=thread.get("reasoning_effort") or "",
            rollout_path=str(rollout_path),
            inspect_status=inspect_status,
            status_label=status_label,
            status_rank=status_rank,
            open_age_seconds=open_age_seconds,
            open_age_text=format_age(open_age_seconds),
            rollout_idle_seconds=rollout_idle_seconds,
            open_turn=open_turn,
            open_turns=open_turns,
            compact_event_count=compact_stats["compact_event_count"],
            compact_http_500_count=compact_stats["compact_http_500_count"],
            compact_send_error_count=compact_stats["compact_send_error_count"],
            compact_state_label=compact_state["label"],
            compact_state_time_text=compact_state["time_text"],
            compact_state_detail=compact_state["detail"],
            risk_label=risk_label,
            risk_rank=risk_rank,
            risk_reason=risk_reason,
            raw_thread=thread,
        )
        if only_stuck and summary.status_rank != "stuck":
            continue
        summaries.append(summary)
        if len(summaries) >= limit:
            break

    return summaries


def run_fallback_repair(
    *,
    codex_home: Path,
    thread: ThreadSummary,
    output_dir: Path,
    repair_all_open_turns: bool,
) -> dict:
    state_db = codex_home / "state_5.sqlite"
    rollout_path = Path(thread.rollout_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = output_dir / "backups" / thread.thread_id / stamp
    rollout_backup, state_backup = backup_files(backup_dir, rollout_path, state_db)

    target_turns = thread.open_turns if repair_all_open_turns else ([thread.open_turn] if thread.open_turn else [])
    repairs = append_abort_events_many(rollout_path, [turn for turn in target_turns if turn])
    if not repairs:
        raise RuntimeError("No open turn was available for fallback repair.")

    update_thread_timestamp(
        state_db=state_db,
        thread_id=thread.thread_id,
        completed_at=repairs[-1]["completed_at"],
        completed_at_ms=repairs[-1]["completed_at_ms"],
    )

    result = {
        "mode": "fallback",
        "thread_id": thread.thread_id,
        "title": thread.title,
        "repaired_turn_count": len(repairs),
        "repairs": repairs,
        "backup_dir": str(backup_dir),
        "backup_rollout": str(rollout_backup),
        "backup_state_db": str(state_backup),
    }
    append_jsonl(output_dir / "gui_actions.jsonl", {"timestamp": utc_timestamp(), **result})
    return result


def should_try_compact_only_fallback(thread: ThreadSummary) -> bool:
    detail = (thread.compact_state_detail or "").lower()
    label = (thread.compact_state_label or "").lower()
    model = (thread.model or "").lower()
    if model != "gpt-5.5":
        return False
    compact_model_failure_markers = [
        "remote compact failed",
        "remote compact returned 404",
        "not accessible for this account",
        "model gpt-5.5 is unavailable",
        "远端 compact 失败",
        "远端 compact 返回 404",
        "不可用",
        "无权限",
    ]
    haystack = f"{label}\n{detail}"
    return any(marker in haystack for marker in compact_model_failure_markers)


def compact_attempt_models(thread: ThreadSummary) -> list[str]:
    original_model = (thread.model or "").strip() or "gpt-5.4"
    candidates: list[str] = []

    # gpt-5.5 chat can work while remote compact intermittently fails. Prefer
    # compacting through a known-good fallback model without rewriting the
    # thread's stored normal chat model.
    if original_model == "gpt-5.5":
        candidates.append("gpt-5.4")
    elif should_try_compact_only_fallback(thread):
        candidates.append("gpt-5.4")
    else:
        candidates.append(original_model)
        if original_model != "gpt-5.4":
            candidates.append("gpt-5.4")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped or ["gpt-5.4"]


def refreshed_thread_summary(thread: ThreadSummary, info: dict) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread.thread_id,
        title=thread.title,
        cwd=thread.cwd,
        cwd_display=thread.cwd_display,
        updated_at_ms=thread.updated_at_ms,
        updated_text=thread.updated_text,
        model=thread.model,
        reasoning_effort=thread.reasoning_effort,
        rollout_path=thread.rollout_path,
        inspect_status=info["status"],
        status_label=thread.status_label,
        status_rank=thread.status_rank,
        open_age_seconds=thread.open_age_seconds,
        open_age_text=thread.open_age_text,
        rollout_idle_seconds=thread.rollout_idle_seconds,
        open_turn=info.get("open_turn"),
        open_turns=info.get("open_turns") or [],
        compact_event_count=thread.compact_event_count,
        compact_http_500_count=thread.compact_http_500_count,
        compact_send_error_count=thread.compact_send_error_count,
        compact_state_label=thread.compact_state_label,
        compact_state_time_text=thread.compact_state_time_text,
        compact_state_detail=thread.compact_state_detail,
        risk_label=thread.risk_label,
        risk_rank=thread.risk_rank,
        risk_reason=thread.risk_reason,
        raw_thread=thread.raw_thread,
    )


def run_compact_assist(
    *,
    codex_home: Path,
    thread: ThreadSummary,
    output_dir: Path,
    settle_seconds: int = DEFAULT_COMPACT_SETTLE_SECONDS,
    poll_interval_seconds: int = 3,
) -> dict:
    rollout_path = Path(thread.rollout_path)
    attempts: list[dict] = []
    after = inspect_thread(thread.raw_thread, rollout_path)
    for attempt_model in compact_attempt_models(thread):
        external_result = run_external_compact_fallback(
            codex_home=codex_home,
            thread_id=thread.thread_id,
            fallback_model=attempt_model,
            timeout_seconds=120,
            output_dir=output_dir,
        )
        after = inspect_thread(thread.raw_thread, rollout_path)
        compact_outcome = (external_result.get("compact_outcome") or {})
        compact_succeeded = bool(
            external_result.get("status") == "compact_succeeded" or compact_outcome.get("ok")
        )
        started_compaction = any(
            ((n.get("method") == "item/started") and (((n.get("params") or {}).get("item") or {}).get("type") == "contextCompaction"))
            for n in (compact_outcome.get("notifications") or [])
        )
        settle_probes: list[dict] = []
        if after["status"] == "orphan_task_started" and started_compaction and settle_seconds > 0:
            deadline = time.time() + max(0, settle_seconds)
            while time.time() < deadline:
                remaining = deadline - time.time()
                time.sleep(min(max(0.2, poll_interval_seconds), remaining))
                probe = inspect_thread(thread.raw_thread, rollout_path)
                settle_probes.append(
                    {
                        "status": probe["status"],
                        "open_turns": probe.get("open_turns") or [],
                    }
                )
                after = probe
                if after["status"] != "orphan_task_started":
                    break
        attempt = {
            "compact_model": attempt_model,
            "external_result": external_result,
            "compact_succeeded": compact_succeeded,
            "started_compaction": started_compaction,
            "settle_probes": settle_probes,
            "after_status": after["status"],
            "after_open_turns": after.get("open_turns") or [],
            "ok": after["status"] != "orphan_task_started" or compact_succeeded,
        }
        attempts.append(attempt)
        if attempt["ok"]:
            break

    result = {
        "mode": "compact_assist",
        "thread_id": thread.thread_id,
        "title": thread.title,
        "after_status": after["status"],
        "after_open_turns": after.get("open_turns") or [],
        "attempts": attempts,
        "ok": bool(attempts and attempts[-1].get("ok")),
    }
    append_jsonl(output_dir / "gui_actions.jsonl", {"timestamp": utc_timestamp(), **result})
    return result


def run_one_click_repair(
    *,
    codex_home: Path,
    thread: ThreadSummary,
    output_dir: Path,
    node_command: str,
    timeout_ms: int,
    settle_seconds: int,
    allow_fallback: bool,
    repair_all_open_turns: bool,
) -> dict:
    rollout_path = Path(thread.rollout_path)
    before = inspect_thread(thread.raw_thread, rollout_path)
    result = {
        "timestamp": utc_timestamp(),
        "thread_id": thread.thread_id,
        "title": thread.title,
        "before_status": before["status"],
        "before_open_turns": before.get("open_turns") or [],
    }

    if before["status"] != "orphan_task_started":
        result["status"] = "healthy"
        return result

    compact_assist_result = run_compact_assist(
        codex_home=codex_home,
        thread=thread,
        output_dir=output_dir,
        settle_seconds=max(DEFAULT_COMPACT_SETTLE_SECONDS, settle_seconds),
    )
    result["compact_assist"] = compact_assist_result
    if compact_assist_result.get("ok"):
        result["status"] = (
            "compaction_succeeded"
            if compact_assist_result.get("after_status") == "orphan_task_started"
            else "repaired_compact_assist"
        )
        append_jsonl(output_dir / "gui_actions.jsonl", result)
        return result

    if is_codex_running():
        live_result = run_live_interrupt_until_stable(
            thread=thread.raw_thread,
            rollout_path=rollout_path,
            node_command=node_command,
            timeout_ms=timeout_ms,
            settle_seconds=settle_seconds,
        )
        result["live_interrupt"] = live_result
        if not live_result.get("ok"):
            after_live = live_result.get("final_info") or inspect_thread(thread.raw_thread, rollout_path)
            failure_reason = classify_live_interrupt_failure(live_result)
            result["after_live_status"] = after_live["status"]
            result["after_live_open_turns"] = after_live.get("open_turns") or []
            result["live_interrupt_failure_reason"] = failure_reason
            result["status"] = (
                "still_open_after_live"
                if after_live["status"] == "orphan_task_started"
                else "live_interrupt_failed"
            )
            allow_fallback_after_failure = live_failure_can_fallback(live_result, after_live)
            if (result["status"] == "live_interrupt_failed" and not allow_fallback_after_failure) or not allow_fallback:
                append_jsonl(output_dir / "gui_actions.jsonl", result)
                return result

            refreshed = refreshed_thread_summary(thread, after_live)
            fallback_result = run_fallback_repair(
                codex_home=codex_home,
                thread=refreshed,
                output_dir=output_dir,
                repair_all_open_turns=repair_all_open_turns,
            )
            result["fallback"] = fallback_result
            result["status"] = "repaired_fallback_after_live"
            append_jsonl(output_dir / "gui_actions.jsonl", result)
            return result

        after_live = live_result.get("final_info") or inspect_thread(thread.raw_thread, rollout_path)
        result["after_live_status"] = after_live["status"]
        result["after_live_open_turns"] = after_live.get("open_turns") or []
        if after_live["status"] != "orphan_task_started":
            result["status"] = "repaired_live"
            append_jsonl(output_dir / "gui_actions.jsonl", result)
            return result

        if not allow_fallback:
            result["status"] = "still_open_after_live"
            append_jsonl(output_dir / "gui_actions.jsonl", result)
            return result

        refreshed = refreshed_thread_summary(thread, after_live)
        fallback_result = run_fallback_repair(
            codex_home=codex_home,
            thread=refreshed,
            output_dir=output_dir,
            repair_all_open_turns=repair_all_open_turns,
        )
        result["fallback"] = fallback_result
        result["status"] = "repaired_fallback_after_live"
        append_jsonl(output_dir / "gui_actions.jsonl", result)
        return result

    if not allow_fallback:
        result["status"] = "codex_not_running_fallback_disabled"
        append_jsonl(output_dir / "gui_actions.jsonl", result)
        return result

    fallback_result = run_fallback_repair(
        codex_home=codex_home,
        thread=thread,
        output_dir=output_dir,
        repair_all_open_turns=repair_all_open_turns,
    )
    result["fallback"] = fallback_result
    result["status"] = "repaired_fallback"
    append_jsonl(output_dir / "gui_actions.jsonl", result)
    return result


class RescueApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1400x820")
        self.root.minsize(1100, 700)

        self.output_dir = ensure_output_dir()
        self.result_queue: queue.Queue = queue.Queue()
        self.current_rows: list[ThreadSummary] = []
        self.row_by_item: dict[str, ThreadSummary] = {}
        self.busy = False

        self.codex_home_var = tk.StringVar(value=str(Path.home() / ".codex"))
        self.workspace_filter_var = tk.StringVar()
        self.title_filter_var = tk.StringVar()
        self.limit_var = tk.IntVar(value=DEFAULT_LIMIT)
        self.stuck_seconds_var = tk.IntVar(value=DEFAULT_STUCK_SECONDS)
        self.only_stuck_var = tk.BooleanVar(value=False)
        self.allow_fallback_var = tk.BooleanVar(value=False)
        self.repair_all_turns_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value=bi("就绪", "Ready"))
        self.node_command_var = tk.StringVar(value="node")
        self.timeout_ms_var = tk.IntVar(value=DEFAULT_TIMEOUT_MS)
        self.settle_seconds_var = tk.IntVar(value=DEFAULT_SETTLE_SECONDS)

        self._build_ui()
        self.root.after(150, self._poll_queue)
        self.refresh_threads()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        controls = ttk.Frame(self.root, padding=12)
        controls.grid(row=0, column=0, sticky="ew")
        for column in range(9):
            controls.columnconfigure(column, weight=0)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(3, weight=1)

        ttk.Label(controls, text=bi("Codex 数据目录", "Codex Home")).grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.codex_home_var).grid(row=0, column=1, sticky="ew", padx=(6, 12))
        ttk.Label(controls, text=bi("工作区筛选", "Workspace Filter")).grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.workspace_filter_var).grid(row=0, column=3, sticky="ew", padx=(6, 12))
        ttk.Label(controls, text=bi("标题筛选", "Title Filter")).grid(row=0, column=4, sticky="w")
        ttk.Entry(controls, textvariable=self.title_filter_var, width=28).grid(row=0, column=5, sticky="ew", padx=(6, 12))
        ttk.Button(controls, text=bi("刷新列表", "Refresh"), command=self.refresh_threads).grid(row=0, column=6, sticky="ew")
        ttk.Button(controls, text=bi("修复选中线程", "Repair Selected"), command=self.repair_selected).grid(row=0, column=7, sticky="ew", padx=(8, 0))

        ttk.Label(controls, text=bi("显示数量", "Limit")).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=5, to=100, textvariable=self.limit_var, width=8).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(10, 0))
        ttk.Label(controls, text=bi("卡死阈值（秒）", "Stuck Threshold (s)")).grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=30, to=3600, increment=30, textvariable=self.stuck_seconds_var, width=10).grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(10, 0))
        ttk.Checkbutton(controls, text=bi("只看疑似卡死", "Only Likely Stuck"), variable=self.only_stuck_var).grid(row=1, column=4, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text=bi("允许保守补丁修复", "Allow fallback patch repair"),
            variable=self.allow_fallback_var,
        ).grid(row=1, column=5, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text=bi("保守修复时处理全部悬空回合", "Fallback repairs all open turns"),
            variable=self.repair_all_turns_var,
        ).grid(row=1, column=6, columnspan=2, sticky="w", padx=(8, 0), pady=(10, 0))

        ttk.Button(controls, text=bi("5.4 压缩", "Compact 5.4"), command=self.compact_selected_gpt54).grid(
            row=1, column=8, sticky="ew", padx=(8, 0), pady=(10, 0)
        )

        ttk.Label(controls, text="Node").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.node_command_var, width=12).grid(row=2, column=1, sticky="w", padx=(6, 12), pady=(10, 0))
        ttk.Label(controls, text=bi("IPC 超时（毫秒）", "IPC Timeout (ms)")).grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=3000, to=60000, increment=1000, textvariable=self.timeout_ms_var, width=10).grid(
            row=2,
            column=3,
            sticky="w",
            padx=(6, 12),
            pady=(10, 0),
        )
        ttk.Label(controls, text=bi("等待稳定（秒）", "Settle (s)")).grid(row=2, column=4, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=0, to=60, textvariable=self.settle_seconds_var, width=8).grid(
            row=2,
            column=5,
            sticky="w",
            padx=(6, 12),
            pady=(10, 0),
        )
        ttk.Button(controls, text=bi("复制线程 ID", "Copy Thread ID"), command=self.copy_thread_id).grid(row=2, column=6, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text=bi("打开 Rollout 文件", "Open Rollout"), command=self.open_rollout).grid(row=2, column=7, sticky="ew", padx=(8, 0), pady=(10, 0))

        tip_frame = ttk.LabelFrame(self.root, text=bi("重要提示", "Important Tip"), padding=(12, 8))
        tip_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        tip_frame.columnconfigure(0, weight=1)
        ttk.Label(
            tip_frame,
            text=FRONTEND_REFRESH_TIP,
            justify="left",
            wraplength=1220,
        ).grid(row=0, column=0, sticky="w")

        panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        panes.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        left = ttk.Frame(panes)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        panes.add(left, weight=3)

        columns = ("status", "title", "workspace", "updated", "open_age", "model", "thread_id")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=18)
        headings = {
            "status": bi("状态", "Status"),
            "title": bi("标题", "Title"),
            "workspace": bi("工作区", "Workspace"),
            "updated": bi("更新时间", "Updated"),
            "open_age": bi("开启时长", "Open Age"),
            "model": bi("模型", "Model"),
            "thread_id": bi("线程 ID", "Thread ID"),
        }
        widths = {
            "status": 120,
            "title": 420,
            "workspace": 170,
            "updated": 160,
            "open_age": 90,
            "model": 130,
            "thread_id": 240,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.tag_configure("healthy", foreground="#1f7a1f")
        self.tree.tag_configure("stuck", foreground="#b3261e")
        self.tree.tag_configure("active", foreground="#9a6700")
        self.tree.tag_configure("error", foreground="#7a7a7a")

        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        right = ttk.Frame(panes, padding=(12, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        panes.add(right, weight=2)

        ttk.Label(right, text=bi("详情", "Details")).grid(row=0, column=0, sticky="w")
        self.details = tk.Text(right, wrap="word", font=("Consolas", 10))
        self.details.grid(row=1, column=0, sticky="nsew")
        self.details.configure(state="disabled")

        details_scroll = ttk.Scrollbar(right, orient="vertical", command=self.details.yview)
        details_scroll.grid(row=1, column=1, sticky="ns")
        self.details.configure(yscrollcommand=details_scroll.set)

        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", padding=(12, 6))
        status_bar.grid(row=3, column=0, sticky="ew")

    def selected_row(self) -> ThreadSummary | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.row_by_item.get(selection[0])

    def set_busy(self, busy: bool, text: str | None = None) -> None:
        self.busy = busy
        if text:
            self.status_var.set(text)

    def run_background(self, action_text: str, func, callback) -> None:
        if self.busy:
            return

        self.set_busy(True, action_text)

        def worker():
            try:
                payload = func()
                self.result_queue.put(("success", callback, payload))
            except Exception:
                self.result_queue.put(("error", callback, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                outcome, callback, payload = self.result_queue.get_nowait()
                self.set_busy(False)
                if outcome == "error":
                    self.status_var.set(bi("操作失败", "Operation failed"))
                    messagebox.showerror(APP_TITLE, payload)
                else:
                    callback(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._poll_queue)

    def refresh_threads(self) -> None:
        codex_home = Path(self.codex_home_var.get()).expanduser()
        limit = max(5, int(self.limit_var.get() or DEFAULT_LIMIT))
        workspace_filter = self.workspace_filter_var.get().strip()
        title_filter = self.title_filter_var.get().strip()
        stuck_seconds = max(30, int(self.stuck_seconds_var.get() or DEFAULT_STUCK_SECONDS))
        only_stuck = bool(self.only_stuck_var.get())

        def task():
            return load_thread_rows(
                codex_home=codex_home,
                limit=limit,
                workspace_filter=workspace_filter,
                title_filter=title_filter,
                stuck_seconds=stuck_seconds,
                only_stuck=only_stuck,
            )

        self.run_background(bi("正在刷新线程列表...", "Refreshing threads..."), task, self.on_threads_loaded)

    def on_threads_loaded(self, rows: list[ThreadSummary]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.current_rows = rows
        self.row_by_item = {}

        for row in rows:
            item = self.tree.insert(
                "",
                "end",
                values=(
                    row.status_label,
                    row.title,
                    row.cwd_display,
                    row.updated_text,
                    row.open_age_text,
                    row.model,
                    row.thread_id,
                ),
                tags=(row.status_rank,),
            )
            self.row_by_item[item] = row

        if rows:
            first_item = self.tree.get_children()[0]
            self.tree.selection_set(first_item)
            self.tree.focus(first_item)
            self.on_select()

        running_text = bi("正在运行", "running") if is_codex_running() else bi("未运行", "not running")
        self.status_var.set(f"{bi('已加载线程数量', 'Loaded threads')}: {len(rows)}. Codex Desktop {running_text}.")

    def on_select(self, _event=None) -> None:
        row = self.selected_row()
        if not row:
            return

        detail_lines = [
            f"{bi('线程 ID', 'Thread ID')}: {row.thread_id}",
            f"{bi('标题', 'Title')}: {row.title}",
            f"{bi('状态', 'Status')}: {row.status_label}",
            f"{bi('检查状态', 'Inspect Status')}: {row.inspect_status}",
            f"{bi('Compact 状态', 'Compact State')}: {row.compact_state_label}",
            f"{bi('Compact 时间', 'Compact Time')}: {row.compact_state_time_text}",
            f"{bi('工作区', 'Workspace')}: {row.cwd or '-'}",
            f"{bi('更新时间', 'Updated')}: {row.updated_text}",
            f"{bi('模型', 'Model')}: {(row.model + ' ' + row.reasoning_effort).strip()}",
            f"{bi('Rollout 文件', 'Rollout')}: {row.rollout_path}",
            f"{bi('开启时长', 'Open Age')}: {row.open_age_text}",
            f"{bi('文件静止时长', 'Rollout Idle')}: {format_age(row.rollout_idle_seconds)}",
            f"{bi('悬空回合数量', 'Open Turn Count')}: {len(row.open_turns)}",
        ]
        if row.compact_state_detail:
            detail_lines.append(f"{bi('Compact 说明', 'Compact Detail')}: {row.compact_state_detail}")
        if row.open_turn:
            detail_lines.extend(
                [
                    "",
                    bi("最新悬空回合", "Latest Open Turn") + ":",
                    json.dumps(row.open_turn, ensure_ascii=False, indent=2),
                ]
            )
        detail_lines.extend(
            [
                "",
                bi("修复策略", "Repair Strategy") + ":",
                "1. "
                + bi(
                    "如果最近的 compact 是远端 404 / 模型无权限，一键修复会先尝试 compact-only fallback：仅把压缩会话临时降到 gpt-5.4，不改线程保存下来的正常聊天模型",
                    "One-click repair first tries to manually push compaction through. For gpt-5.5 it goes straight to gpt-5.4 for compact; other models first try manual compact with the original model, then retry once with gpt-5.4 if needed. This only affects the compact session and does not rewrite the thread's stored normal chat model",
                )
                + ".",
                "2. "
                + bi(
                    "如果 compact-only fallback 不适用或没有修好，再通过本地 Codex IPC 发送真实 interrupt",
                    "If manual compact still does not clear the thread, send a real interrupt through the local Codex IPC pipe",
                )
                + ".",
                "3. "
                + bi(
                    "如果仍然卡住，并且你允许保守修复，就在本地追加 turn_aborted 并自动备份",
                    "If still stuck and fallback is allowed, append turn_aborted locally with backups",
                )
                + ".",
            ]
        )

        detail_lines.extend(
            [
                "",
                bi("前端刷新提示", "Frontend Refresh Tip") + ":",
                FRONTEND_REFRESH_TIP,
            ]
        )

        self.details.configure(state="normal")
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", "\n".join(detail_lines))
        self.details.configure(state="disabled")

    def repair_selected(self) -> None:
        row = self.selected_row()
        if not row:
            messagebox.showinfo(APP_TITLE, bi("请先选中一个线程。", "Select a thread first."))
            return

        if row.inspect_status != "orphan_task_started":
            messagebox.showinfo(APP_TITLE, bi("当前选中的线程看起来并没有卡住。", "The selected thread does not currently look stuck."))
            return

        if row.status_rank == "active":
            proceed = messagebox.askyesno(
                APP_TITLE,
                bi(
                    "这个线程看起来仍然像是活跃中，而不是明确卡死。\n\n你仍然要尝试修复吗？",
                    "This thread still looks active rather than clearly stuck.\n\nDo you want to try a repair anyway?",
                ),
            )
            if not proceed:
                return

        codex_home = Path(self.codex_home_var.get()).expanduser()
        output_dir = self.output_dir
        node_command = self.node_command_var.get().strip() or "node"
        timeout_ms = max(3000, int(self.timeout_ms_var.get() or DEFAULT_TIMEOUT_MS))
        settle_seconds = max(0, int(self.settle_seconds_var.get() or DEFAULT_SETTLE_SECONDS))
        allow_fallback = bool(self.allow_fallback_var.get())
        repair_all_open_turns = bool(self.repair_all_turns_var.get())

        def task():
            return run_one_click_repair(
                codex_home=codex_home,
                thread=row,
                output_dir=output_dir,
                node_command=node_command,
                timeout_ms=timeout_ms,
                settle_seconds=settle_seconds,
                allow_fallback=allow_fallback,
                repair_all_open_turns=repair_all_open_turns,
            )

        self.run_background(bi("正在修复选中线程...", "Repairing selected thread..."), task, self.on_repair_complete)

    def compact_selected_gpt54(self) -> None:
        row = self.selected_row()
        if not row:
            messagebox.showinfo(APP_TITLE, bi("璇峰厛閫変腑涓€涓嚎绋嬨€?", "Select a thread first."))
            return

        codex_home = Path(self.codex_home_var.get()).expanduser()
        output_dir = self.output_dir

        def task():
            return {
                "timestamp": utc_timestamp(),
                "mode": "compact_only_gpt54",
                "thread_id": row.thread_id,
                "title": row.title,
                "result": run_external_compact_fallback(
                    codex_home=codex_home,
                    thread_id=row.thread_id,
                    fallback_model="gpt-5.4",
                    timeout_seconds=120,
                    output_dir=output_dir,
                ),
            }

        self.run_background(
            bi("姝ｅ湪鐢?gpt-5.4 鎵嬪姩鍘嬬缉...", "Running manual gpt-5.4 compaction..."),
            task,
            self.on_compact_only_complete,
        )

    def on_repair_complete(self, payload: dict) -> None:
        status = payload.get("status", "unknown")
        self.status_var.set(f"{bi('修复完成', 'Repair finished')}: {status}")

        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("最近一次修复结果", "Last Repair Result") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

        if status.startswith("repaired_"):
            messagebox.showinfo(APP_TITLE, FRONTEND_REFRESH_TIP)

        self.refresh_threads()

    def on_compact_only_complete(self, payload: dict) -> None:
        result = payload.get("result") or {}
        compact_outcome = result.get("compact_outcome") or {}
        status = result.get("status", "unknown")
        outcome = compact_outcome.get("status") or compact_outcome.get("kind") or "-"
        self.status_var.set(f"{bi('5.4 鍘嬬缉瀹屾垚', '5.4 compaction finished')}: {status} / {outcome}")

        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("鏈€杩戜竴娆?5.4 鍘嬬缉缁撴灉", "Last 5.4 Compaction Result") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

        self.refresh_threads()

    def copy_thread_id(self) -> None:
        row = self.selected_row()
        if not row:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(row.thread_id)
        self.status_var.set(f"{bi('已复制线程 ID', 'Copied thread ID')}: {row.thread_id}")

    def open_rollout(self) -> None:
        row = self.selected_row()
        if not row:
            return
        rollout_path = Path(row.rollout_path)
        if not rollout_path.exists():
            messagebox.showerror(APP_TITLE, f"{bi('Rollout 文件不存在', 'Rollout file does not exist')}:\n{rollout_path}")
            return
        os.startfile(str(rollout_path))


def main() -> int:
    root = tk.Tk()
    app = RescueApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
