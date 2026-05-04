from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import subprocess
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
        run_live_compact,
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
        run_live_compact,
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
FRONTEND_SYNC_WINDOW_SECONDS = 900

FRONTEND_REFRESH_TIP = bi(
    "重要：如果已经显示“压缩完成”，但聊天页还是旧画面，或者背景信息窗口里的小圈已经满格却没有重置，这通常是前端没同步，不代表压缩还没完成。只有在工具已经修复成功，并且重新检查后确认这条线程已经没有 open turn 时，才适合回到原聊天里发一句很短的话，例如“继续”，来刷新前端显示。如果线程仍然显示 open turn，或者你一发消息它又开始压缩，就不要把发消息当成刷新手段。",
    "Important: if compaction is already shown as completed, but the chat page is still stale, or the small progress circle in the background-info panel stays full and does not reset, that usually means the frontend did not sync. It does not necessarily mean compaction is still running. Only use a short follow-up message such as 'continue' when the tool has already repaired the thread and a fresh re-check shows no open turn. If the thread still shows an open turn, or a new message immediately starts compaction again, do not use a new message as a refresh trick.",
)

RELOAD_DISABLED_TIP = bi(
    "最新 Codex 更新后，这个工具里的界面层重载容易触发 `mismatched path`，导致线程无法恢复。为了避免再把线程搞坏，GUI 里的重载按钮已临时停用。",
    "After the latest Codex updates, the GUI reload actions can trigger `mismatched path` and make a thread fail to resume. To avoid damaging threads again, the reload buttons are temporarily disabled in the GUI.",
)

STATUS_TEXT = {
    "healthy": bi("正常", "Healthy"),
    "sync": bi("后台已好", "Backend Healed"),
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
    compact_state_kind: str
    compact_state_timestamp_ms: int | None
    compact_state_label: str
    compact_state_time_text: str
    compact_state_detail: str
    frontend_sync_label: str
    frontend_sync_detail: str
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


def iso_to_timestamp_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def summarize_compact_error_message(message: str) -> str:
    message = (message or "").strip()
    if not message:
        return "-"
    if "does not exist or you do not have access to it" in message:
        match = re.search(r"The model `([^`]+)` does not exist or you do not have access to it", message)
        if match:
            return bi(
                f"远端 compact 失败：模型 {match.group(1)} 不可用，或当前账号无权限。",
                f"Remote compact failed: model {match.group(1)} is unavailable or not accessible for this account.",
            )
        return bi(
            "远端 compact 失败：模型不可用，或当前账号无权限。",
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

        if obj.get("type") == "compacted":
            timestamp_ms = iso_to_timestamp_ms(obj.get("timestamp"))
            return {
                "kind": "success",
                "timestamp_ms": timestamp_ms,
                "label": bi("最近一次 compact 成功", "Latest compact succeeded"),
                "time_text": format_local_timestamp(timestamp_ms),
                "detail": bi(
                    "后端已经完成了一次上下文压缩。",
                    "The backend has already completed a context compaction.",
                ),
            }

        if obj.get("type") != "event_msg":
            continue

        payload = obj.get("payload") or {}
        if payload.get("type") != "error":
            continue

        message = (payload.get("message") or "").strip()
        if "compact" not in message.lower():
            continue

        return {
            "kind": "failure",
            "timestamp_ms": iso_to_timestamp_ms(obj.get("timestamp")),
            "label": bi("最近一次 compact 远端失败", "Latest compact failed remotely"),
            "time_text": format_local_timestamp(iso_to_timestamp_ms(obj.get("timestamp"))),
            "detail": summarize_compact_error_message(message),
        }

    return None


def load_thread_compact_state(codex_home: Path, thread_id: str, rollout_path: Path) -> dict:
    rollout_state = load_rollout_compact_state(rollout_path)
    if rollout_state:
        return rollout_state

    logs_db = codex_home / "logs_2.sqlite"
    default_state = {
        "kind": "unknown",
        "timestamp_ms": None,
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
        ts = int(row["ts"])
        time_text = format_log_timestamp(ts)
        timestamp_ms = ts * 1000
        if COMPACT_SEND_ERROR_TEXT in body:
            return {
                "kind": "failure",
                "timestamp_ms": timestamp_ms,
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
                    "kind": "success",
                    "timestamp_ms": timestamp_ms,
                    "label": bi("最近一次 compact 成功", "Latest compact succeeded"),
                    "time_text": time_text,
                    "detail": bi("最近一次 compact 请求返回了 200。", "The latest compact request returned 200."),
                }
            return {
                "kind": "failure",
                "timestamp_ms": timestamp_ms,
                "label": bi(f"最近一次 compact 返回 {status_code}", f"Latest compact returned {status_code}"),
                "time_text": time_text,
                "detail": bi(
                    f"最近一次 compact 请求返回了 HTTP {status_code}。",
                    f"The latest compact request returned HTTP {status_code}.",
                ),
            }

    if rows:
        return {
            "kind": "activity",
            "timestamp_ms": int(rows[0]["ts"]) * 1000,
            "label": bi("最近触发过 compact", "Recent compact activity"),
            "time_text": format_log_timestamp(int(rows[0]["ts"])),
            "detail": bi(
                "看到了 compact 链路活动，但最近几条日志里没有明确成功或失败结果。",
                "Compact activity was seen, but the latest log entries did not contain a clear success or failure outcome.",
            ),
        }

    return default_state


def load_recent_compact_trace(codex_home: Path, thread_id: str, min_ts_s: int) -> dict:
    logs_db = codex_home / "logs_2.sqlite"
    result = {
        "kind": "none",
        "saw_activity": False,
        "timestamp_ms": None,
        "time_text": "-",
        "detail": bi(
            "没有看到这次新压缩的链路活动。",
            "No new compact activity was seen for this attempt.",
        ),
    }
    if not logs_db.exists():
        return result

    conn = sqlite3.connect(str(logs_db))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select ts, feedback_log_body
            from logs
            where thread_id = ?
              and ts >= ?
              and (
                feedback_log_body like ?
                or feedback_log_body like ?
                or feedback_log_body like ?
              )
            order by ts asc, id asc
            """,
            (
                thread_id,
                int(min_ts_s),
                '%api.path="responses/compact"%',
                "%http.response.status_code=%",
                f"%{COMPACT_SEND_ERROR_TEXT}%",
            ),
        ).fetchall()
    except Exception:
        return result
    finally:
        conn.close()

    if not rows:
        return result

    saw_activity = False
    latest_ts = None
    latest_body = ""
    status_code = None
    send_error = False

    for row in rows:
        ts = int(row["ts"])
        body = row["feedback_log_body"] or ""
        if 'api.path="responses/compact"' in body:
            saw_activity = True
            latest_ts = ts
            latest_body = body
        if COMPACT_SEND_ERROR_TEXT in body:
            saw_activity = True
            send_error = True
            latest_ts = ts
            latest_body = body
        match = COMPACT_HTTP_STATUS_RE.search(body)
        if match:
            saw_activity = True
            status_code = int(match.group(1))
            latest_ts = ts
            latest_body = body

    if latest_ts is not None:
        result["timestamp_ms"] = latest_ts * 1000
        result["time_text"] = format_log_timestamp(latest_ts)
    result["saw_activity"] = saw_activity

    if send_error:
        result["kind"] = "failure"
        result["detail"] = bi(
            "这次新压缩已经发起，但 compact 请求发送失败了。",
            "This compact attempt was started, but the compact request failed to send.",
        )
        return result

    if status_code is not None:
        result["kind"] = "success" if status_code == 200 else "failure"
        result["detail"] = (
            bi(
                "这次新压缩已经完成并返回 200。",
                "This compact attempt completed and returned 200.",
            )
            if status_code == 200
            else bi(
                f"这次新压缩返回了 HTTP {status_code}。",
                f"This compact attempt returned HTTP {status_code}.",
            )
        )
        return result

    if saw_activity:
        result["kind"] = "activity"
        result["detail"] = bi(
            "这次新压缩已经开始触发 `/responses/compact`，但还没有看到成功或失败收尾。",
            "This compact attempt has already reached `/responses/compact`, but no success or failure outcome has appeared yet.",
        )
        return result

    if latest_body:
        result["detail"] = latest_body[:300]
    return result


def determine_frontend_sync_hint(
    *,
    inspect_status: str,
    compact_state_kind: str,
    compact_state_timestamp_ms: int | None,
    now_ms: int,
) -> tuple[str, str]:
    if inspect_status != "no_open_turn":
        return "", ""
    if compact_state_kind != "success":
        return "", ""
    if compact_state_timestamp_ms and (now_ms - compact_state_timestamp_ms) > FRONTEND_SYNC_WINDOW_SECONDS * 1000:
        return "", ""
    return (
        bi("后台已经压好，前端可能没刷新", "Backend compacted; UI may be stale"),
        bi(
            "这类情况说明后台大概率已经压好，但前端没同步。当前版本里不要再用工具里的重载按钮，以免触发 `mismatched path`。先只把它当成“后台已好”的提示。",
            "This usually means the backend compacted successfully but the frontend did not sync. In the current version, do not use the GUI reload buttons again because they can trigger `mismatched path`. Treat this as a backend-healed signal only.",
        ),
    )


def run_powershell(script: str, timeout_seconds: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def soft_reload_codex_ui() -> dict:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$ws = New-Object -ComObject WScript.Shell
if (-not $ws.AppActivate('Codex')) {
  throw 'Could not focus a Codex window.'
}
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('^r')
'soft_reload_sent'
"""
    result = run_powershell(script, timeout_seconds=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Soft reload failed.").strip())
    return {
        "mode": "soft_reload_ui",
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def restart_codex_renderer_only() -> dict:
    script = r"""
$before = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'Codex.exe' -and $_.CommandLine -match '--type=renderer' } | ForEach-Object { $_.ProcessId })
if (-not $before -or $before.Count -eq 0) {
  throw 'No Codex renderer process found.'
}
$before | ForEach-Object { Stop-Process -Id $_ -Force }
Start-Sleep -Seconds 2
$after = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'Codex.exe' -and $_.CommandLine -match '--type=renderer' } | ForEach-Object { $_.ProcessId })
@{ before = $before; after = $after } | ConvertTo-Json -Compress
"""
    result = run_powershell(script, timeout_seconds=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Renderer restart failed.").strip())
    payload = {}
    stdout = (result.stdout or "").strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"raw": stdout}
    return {
        "mode": "restart_renderer_only",
        "payload": payload,
        "stderr": (result.stderr or "").strip(),
    }


def frontend_reload_guard_message(row: ThreadSummary | None) -> str:
    if row is None:
        return bi("请先选中一个线程。", "Select a thread first.")
    if row.inspect_status != "no_open_turn":
        return bi(
            "这条线程当前还有 open turn。现在重载前端风险很高，先不要用重载按钮。",
            "This thread still has an open turn. Reloading the frontend is risky right now, so do not use the reload buttons yet.",
        )
    if row.compact_state_kind != "success":
        return bi(
            "这条线程后台还没有明确出现 compact 成功记录，现在不应该重载前端。",
            "This thread does not yet have a confirmed backend compact success, so the frontend should not be reloaded.",
        )
    if not row.frontend_sync_label:
        return bi(
            "这条线程目前不符合“后台已好但前端没刷新”的条件，先不要用重载按钮。",
            "This thread does not currently match the 'backend healed but frontend stale' case, so do not use the reload buttons yet.",
        )
    return ""


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
        reasons.append(
            bi(
                "`gpt-5.5 xhigh` 更容易把长线程推回 compact。",
                "`gpt-5.5 xhigh` is more likely to push long threads back into compaction.",
            )
        )

    if compact_event_count >= 20:
        score += 2
        reasons.append(
            bi(
                f"这条线程已经出现很多次 compact 相关事件（{compact_event_count} 次）。",
                f"This thread has already produced many compact-related events ({compact_event_count}).",
            )
        )

    if compact_http_500_count > 0:
        score += 3
        reasons.append(
            bi(
                f"这条线程已经出现 compact `HTTP 500`（{compact_http_500_count} 次）。",
                f"This thread has already hit compact `HTTP 500` ({compact_http_500_count} times).",
            )
        )

    if compact_send_error_count > 0:
        score += 3
        reasons.append(
            bi(
                f"这条线程已经出现 compact 发送失败（{compact_send_error_count} 次）。",
                f"This thread has already hit compact send errors ({compact_send_error_count} times).",
            )
        )

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
    current_time_ms = current_time * 1000

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
        frontend_sync_label, frontend_sync_detail = determine_frontend_sync_hint(
            inspect_status=inspect_status,
            compact_state_kind=compact_state["kind"],
            compact_state_timestamp_ms=compact_state["timestamp_ms"],
            now_ms=current_time_ms,
        )
        if frontend_sync_label:
            status_rank = "sync"
            status_label = STATUS_TEXT["sync"]

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
            compact_state_kind=compact_state["kind"],
            compact_state_timestamp_ms=compact_state["timestamp_ms"],
            compact_state_label=compact_state["label"],
            compact_state_time_text=compact_state["time_text"],
            compact_state_detail=compact_state["detail"],
            frontend_sync_label=frontend_sync_label,
            frontend_sync_detail=frontend_sync_detail,
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
        compact_state_kind=thread.compact_state_kind,
        compact_state_timestamp_ms=thread.compact_state_timestamp_ms,
        compact_state_label=thread.compact_state_label,
        compact_state_time_text=thread.compact_state_time_text,
        compact_state_detail=thread.compact_state_detail,
        frontend_sync_label=thread.frontend_sync_label,
        frontend_sync_detail=thread.frontend_sync_detail,
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
    node_command: str = "node",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    settle_seconds: int = DEFAULT_COMPACT_SETTLE_SECONDS,
    poll_interval_seconds: int = 3,
    allow_model_fallback: bool = True,
) -> dict:
    rollout_path = Path(thread.rollout_path)
    attempts: list[dict] = []
    after = inspect_thread(thread.raw_thread, rollout_path)

    if not allow_model_fallback:
        same_model = (thread.model or "").strip() or (thread.raw_thread.get("model") or "").strip() or "gpt-5.5"
        external_result = run_external_compact_fallback(
            codex_home=codex_home,
            thread_id=thread.thread_id,
            fallback_model=same_model,
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

        external_attempt = {
            "mode": "external_same_model_compact",
            "compact_model": same_model,
            "external_result": external_result,
            "compact_succeeded": compact_succeeded,
            "started_compaction": started_compaction,
            "settle_probes": settle_probes,
            "after_status": after["status"],
            "after_open_turns": after.get("open_turns") or [],
            "ok": after["status"] != "orphan_task_started" or compact_succeeded,
        }
        attempts.append(external_attempt)

        same_model_status = "same_model_compact_failed"
        compact_outcome_status = (compact_outcome.get("status") or "").strip()
        if external_attempt["ok"]:
            same_model_status = "same_model_compact_succeeded"
        elif compact_outcome_status == "timeout_waiting_compact" and started_compaction:
            same_model_status = "same_model_compact_hung_after_request"

        result = {
            "mode": "compact_assist",
            "thread_id": thread.thread_id,
            "title": thread.title,
            "after_status": after["status"],
            "after_open_turns": after.get("open_turns") or [],
            "attempts": attempts,
            "ok": bool(external_attempt["ok"]),
            "status": same_model_status,
        }
        append_jsonl(output_dir / "gui_actions.jsonl", {"timestamp": utc_timestamp(), **result})
        return result

    if is_codex_running():
        request_started_ms = int(time.time() * 1000)
        request_started_s = max(0, int(request_started_ms / 1000) - 1)
        live_compact = run_live_compact(
            thread_id=thread.thread_id,
            node_command=node_command,
            timeout_ms=timeout_ms,
        )
        compact_state = load_thread_compact_state(codex_home, thread.thread_id, rollout_path)
        recent_trace = load_recent_compact_trace(codex_home, thread.thread_id, request_started_s)
        settle_probes: list[dict] = []
        deadline = time.time() + max(0, settle_seconds)
        while time.time() < deadline:
            recent_success = recent_trace.get("kind") == "success"
            recent_failure = recent_trace.get("kind") == "failure"
            if recent_success or recent_failure:
                break

            remaining = deadline - time.time()
            time.sleep(min(max(0.2, poll_interval_seconds), remaining))
            after = inspect_thread(thread.raw_thread, rollout_path)
            compact_state = load_thread_compact_state(codex_home, thread.thread_id, rollout_path)
            recent_trace = load_recent_compact_trace(codex_home, thread.thread_id, request_started_s)
            settle_probes.append(
                {
                    "status": after["status"],
                    "open_turns": after.get("open_turns") or [],
                    "compact_state_kind": compact_state.get("kind"),
                    "compact_state_label": compact_state.get("label"),
                    "compact_state_time_text": compact_state.get("time_text"),
                    "recent_trace_kind": recent_trace.get("kind"),
                    "recent_trace_detail": recent_trace.get("detail"),
                }
            )

        recent_success = recent_trace.get("kind") == "success"
        live_attempt = {
            "mode": "live_compact",
            "live_compact": live_compact,
            "compact_state": compact_state,
            "recent_trace": recent_trace,
            "settle_probes": settle_probes,
            "after_status": after["status"],
            "after_open_turns": after.get("open_turns") or [],
            "ok": bool(live_compact.get("ok") and recent_success and after["status"] != "orphan_task_started"),
        }
        attempts.append(live_attempt)
        if live_attempt["ok"]:
            result = {
                "mode": "compact_assist",
                "thread_id": thread.thread_id,
                "title": thread.title,
                "after_status": after["status"],
                "after_open_turns": after.get("open_turns") or [],
                "attempts": attempts,
                "ok": True,
                "status": "same_model_compact_succeeded",
            }
            append_jsonl(output_dir / "gui_actions.jsonl", {"timestamp": utc_timestamp(), **result})
            return result

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
        "status": "fallback_compact_succeeded" if bool(attempts and attempts[-1].get("ok")) else "fallback_compact_failed",
    }
    append_jsonl(output_dir / "gui_actions.jsonl", {"timestamp": utc_timestamp(), **result})
    return result


def run_same_model_manual_compact_trigger(
    *,
    codex_home: Path,
    thread: ThreadSummary,
    output_dir: Path,
    node_command: str = "node",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    observe_seconds: int = 12,
    poll_interval_seconds: float = 0.75,
) -> dict:
    rollout_path = Path(thread.rollout_path)
    request_started_ms = int(time.time() * 1000)
    request_started_s = max(0, int(request_started_ms / 1000) - 1)
    same_model = (thread.model or "").strip() or (thread.raw_thread.get("model") or "").strip() or "gpt-5.5"
    before = inspect_thread(thread.raw_thread, rollout_path)
    live_compact = run_live_compact(
        thread_id=thread.thread_id,
        node_command=node_command,
        timeout_ms=timeout_ms,
    )

    trigger_probes: list[dict] = []
    recent_trace = load_recent_compact_trace(codex_home, thread.thread_id, request_started_s)
    compact_state = load_thread_compact_state(codex_home, thread.thread_id, rollout_path)
    after = inspect_thread(thread.raw_thread, rollout_path)

    deadline = time.time() + max(3, observe_seconds)
    while time.time() < deadline:
        compact_ts = compact_state.get("timestamp_ms")
        fresh_compact_state = bool(compact_ts and compact_ts >= request_started_ms)
        trace_kind = recent_trace.get("kind")
        if trace_kind in {"activity", "success", "failure"} or fresh_compact_state:
            break

        remaining = deadline - time.time()
        time.sleep(min(max(0.2, poll_interval_seconds), remaining))
        after = inspect_thread(thread.raw_thread, rollout_path)
        recent_trace = load_recent_compact_trace(codex_home, thread.thread_id, request_started_s)
        compact_state = load_thread_compact_state(codex_home, thread.thread_id, rollout_path)
        trigger_probes.append(
            {
                "status": after["status"],
                "open_turns": after.get("open_turns") or [],
                "recent_trace_kind": recent_trace.get("kind"),
                "recent_trace_detail": recent_trace.get("detail"),
                "compact_state_kind": compact_state.get("kind"),
                "compact_state_time_text": compact_state.get("time_text"),
            }
        )

    compact_ts = compact_state.get("timestamp_ms")
    fresh_compact_state = bool(compact_ts and compact_ts >= request_started_ms)
    trace_kind = recent_trace.get("kind")

    started = bool(trace_kind in {"activity", "success"} or fresh_compact_state)
    completed = bool(trace_kind == "success" or (fresh_compact_state and compact_state.get("kind") == "success"))
    failed = bool(trace_kind == "failure" or (fresh_compact_state and compact_state.get("kind") == "failure"))

    status = "same_model_compact_no_activity"
    ok = False
    if not live_compact.get("ok"):
        status = "same_model_compact_trigger_failed"
    elif failed:
        status = "same_model_compact_failed_after_request"
    elif completed:
        status = "same_model_compact_completed"
        ok = True
    elif started:
        status = "same_model_compact_started"
        ok = True

    result = {
        "mode": "same_model_manual_compact_trigger",
        "thread_id": thread.thread_id,
        "title": thread.title,
        "requested_model": same_model,
        "before_status": before["status"],
        "before_open_turns": before.get("open_turns") or [],
        "after_status": after["status"],
        "after_open_turns": after.get("open_turns") or [],
        "live_compact": live_compact,
        "recent_trace": recent_trace,
        "compact_state": compact_state,
        "trigger_probes": trigger_probes,
        "ok": ok,
        "status": status,
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
        node_command=node_command,
        timeout_ms=timeout_ms,
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
        self.root.configure(bg="#f4f7fb")

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

        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure(".", font=("Segoe UI", 10))
        self.style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        self.style.configure("TLabelframe", background="#f4f7fb")
        self.style.configure("TLabelframe.Label", background="#f4f7fb", foreground="#15233b", font=("Segoe UI Semibold", 10))

        self._build_ui()
        self.root.after(150, self._poll_queue)
        self.refresh_threads()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        hero = tk.Frame(self.root, bg="#15233b", padx=18, pady=16)
        hero.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 10))
        tk.Label(
            hero,
            text=APP_TITLE,
            bg="#15233b",
            fg="#ffffff",
            font=("Segoe UI Semibold", 18),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            hero,
            text=bi(
                "先用真实手动压缩，再用 5.4 回退压缩；如果后台已好而页面不动，再做前端同步。",
                "Try real manual compact first, then 5.4 fallback compact; if the backend is healed but the page is stale, sync the frontend.",
            ),
            bg="#15233b",
            fg="#dbe7ff",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
            wraplength=1220,
        ).pack(anchor="w", pady=(6, 0))

        controls = ttk.Frame(self.root, padding=12)
        controls.grid(row=1, column=0, sticky="ew")
        for column in range(10):
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
        ttk.Label(controls, text=bi("卡死阈值(秒)", "Stuck Threshold (s)")).grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=30, to=3600, increment=30, textvariable=self.stuck_seconds_var, width=10).grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(10, 0))
        ttk.Checkbutton(controls, text=bi("只看疑似卡死", "Only Likely Stuck"), variable=self.only_stuck_var).grid(row=1, column=4, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text=bi("允许保守补丁修复", "Allow fallback patch repair"),
            variable=self.allow_fallback_var,
        ).grid(row=1, column=5, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls,
            text=bi("兜底修复时处理全部悬空回合", "Fallback repairs all open turns"),
            variable=self.repair_all_turns_var,
        ).grid(row=1, column=6, columnspan=2, sticky="w", padx=(8, 0), pady=(10, 0))

        ttk.Button(controls, text=bi("手动压缩(同模型优先)", "Manual Compact (Same Model First)"), command=self.compact_selected_gpt54).grid(
            row=1, column=8, sticky="ew", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(controls, text=bi("5.4 回退压缩", "5.4 Fallback Compact"), command=self.fallback_compact_selected_gpt54).grid(
            row=1, column=9, sticky="ew", padx=(8, 0), pady=(10, 0)
        )

        ttk.Label(controls, text="Node").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(controls, textvariable=self.node_command_var, width=12).grid(row=2, column=1, sticky="w", padx=(6, 12), pady=(10, 0))
        ttk.Label(controls, text=bi("IPC 超时(毫秒)", "IPC Timeout (ms)")).grid(row=2, column=2, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=3000, to=60000, increment=1000, textvariable=self.timeout_ms_var, width=10).grid(
            row=2,
            column=3,
            sticky="w",
            padx=(6, 12),
            pady=(10, 0),
        )
        ttk.Label(controls, text=bi("等待稳定(秒)", "Settle (s)")).grid(row=2, column=4, sticky="w", pady=(10, 0))
        ttk.Spinbox(controls, from_=0, to=60, textvariable=self.settle_seconds_var, width=8).grid(
            row=2,
            column=5,
            sticky="w",
            padx=(6, 12),
            pady=(10, 0),
        )
        ttk.Button(controls, text=bi("复制线程 ID", "Copy Thread ID"), command=self.copy_thread_id).grid(row=2, column=6, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text=bi("打开 Rollout 文件", "Open Rollout"), command=self.open_rollout).grid(row=2, column=7, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Button(controls, text=bi("软刷新前端", "Soft Reload UI"), command=self.soft_reload_ui).grid(
            row=2, column=8, sticky="ew", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(controls, text=bi("只重载界面层", "Restart Renderer Only"), command=self.restart_renderer_only).grid(
            row=2, column=9, sticky="ew", padx=(8, 0), pady=(10, 0)
        )

        tip_frame = ttk.LabelFrame(self.root, text=bi("重要提示", "Important Tip"), padding=(12, 8))
        tip_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        tip_frame.columnconfigure(0, weight=1)
        ttk.Label(
            tip_frame,
            text=FRONTEND_REFRESH_TIP,
            justify="left",
            wraplength=1220,
        ).grid(row=0, column=0, sticky="w")

        panes = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        panes.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))

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
        self.tree.tag_configure("sync", foreground="#005fb8")
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
        status_bar.grid(row=4, column=0, sticky="ew")

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
            self.status_var.set(
                bi(
                    "工具正在执行上一项操作，请等状态栏恢复后再点一次。",
                    "The tool is still busy with the previous action. Wait for the status bar to return before clicking again.",
                )
            )
            messagebox.showinfo(
                APP_TITLE,
                bi(
                    "当前还有一个操作在运行，所以这次点击没有开始新任务。",
                    "Another operation is still running, so this click did not start a new task.",
                ),
            )
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
            f"{bi('压缩状态', 'Compact State')}: {row.compact_state_label}",
            f"{bi('压缩时间', 'Compact Time')}: {row.compact_state_time_text}",
            f"{bi('工作区', 'Workspace')}: {row.cwd or '-'}",
            f"{bi('更新时间', 'Updated')}: {row.updated_text}",
            f"{bi('模型', 'Model')}: {(row.model + ' ' + row.reasoning_effort).strip()}",
            f"{bi('Rollout 文件', 'Rollout')}: {row.rollout_path}",
            f"{bi('开启时长', 'Open Age')}: {row.open_age_text}",
            f"{bi('文件静止时长', 'Rollout Idle')}: {format_age(row.rollout_idle_seconds)}",
            f"{bi('悬空回合数量', 'Open Turn Count')}: {len(row.open_turns)}",
        ]
        if row.compact_state_detail:
            detail_lines.append(f"{bi('压缩说明', 'Compact Detail')}: {row.compact_state_detail}")
        if row.frontend_sync_label:
            detail_lines.append(f"{bi('前端同步提示', 'Frontend Sync Hint')}: {row.frontend_sync_label}")
            detail_lines.append(f"{bi('推荐动作', 'Recommended Action')}: {row.frontend_sync_detail}")
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
                    "一键修复会先尝试手动推进压缩。默认先走和终端手动压缩一致的同模型压缩；如果最近的 compact 像是 gpt-5.5 的远端拒绝或模型权限问题，再回退到只给 compact 临时使用 gpt-5.4。",
                    "One-click repair first tries to manually push compaction through. It prefers the same-model compact path that terminal/manual compaction uses. If recent compact signals look like a gpt-5.5 remote reject or model-access problem, it falls back to using gpt-5.4 only for the compact step.",
                )
                + ".",
                "2. "
                + bi(
                    "如果手动压缩没有把线程拉回来，再通过本地 Codex IPC 发送真实 interrupt。",
                    "If manual compact still does not clear the thread, send a real interrupt through the local Codex IPC pipe",
                )
                + ".",
                "3. "
                + bi(
                    "如果仍然卡住，并且你允许保守修复，就在本地追加 turn_aborted，并先自动备份。",
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
                    "杩欎釜绾跨▼鐪嬭捣鏉ヤ粛鐒跺儚鏄椿璺冧腑锛岃€屼笉鏄槑纭崱姝汇€俓n\n浣犱粛鐒惰灏濊瘯淇鍚楋紵",
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

    def fallback_compact_selected_gpt54(self) -> None:
        row = self.selected_row()
        if not row:
            messagebox.showinfo(APP_TITLE, bi("请先选中一个线程。", "Select a thread first."))
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
            bi("正在执行 5.4 回退压缩...", "Running 5.4 fallback compaction..."),
            task,
            self.on_fallback_compact_complete,
        )

    def on_repair_complete(self, payload: dict) -> None:
        status = payload.get("status", "unknown")
        self.status_var.set(f"{bi('淇瀹屾垚', 'Repair finished')}: {status}")

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

    def on_fallback_compact_complete(self, payload: dict) -> None:
        result = payload.get("result") or {}
        compact_outcome = result.get("compact_outcome") or {}
        status = result.get("status", "unknown")
        outcome = compact_outcome.get("status") or compact_outcome.get("kind") or "-"
        self.status_var.set(f"{bi('5.4 压缩完成', '5.4 compaction finished')}: {status} / {outcome}")

        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("最近一次 5.4 回退压缩结果", "Last 5.4 Fallback Compaction Result") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

        self.refresh_threads()

    def compact_selected_gpt54(self) -> None:
        row = self.selected_row()
        if not row:
            messagebox.showinfo(APP_TITLE, bi("请先选中一个线程。", "Select a thread first."))
            return

        codex_home = Path(self.codex_home_var.get()).expanduser()
        output_dir = self.output_dir
        node_command = self.node_command_var.get().strip() or "node"
        timeout_ms = max(3000, int(self.timeout_ms_var.get() or DEFAULT_TIMEOUT_MS))
        settle_seconds = max(0, int(self.settle_seconds_var.get() or DEFAULT_SETTLE_SECONDS))

        def task():
            result = run_same_model_manual_compact_trigger(
                codex_home=codex_home,
                thread=row,
                output_dir=output_dir,
                node_command=node_command,
                timeout_ms=timeout_ms,
                observe_seconds=max(8, settle_seconds),
            )
            return {
                "timestamp": utc_timestamp(),
                "mode": "manual_same_model_compact",
                "thread_id": row.thread_id,
                "title": row.title,
                "result": result,
                "after_status": result.get("after_status"),
                "after_open_turns": result.get("after_open_turns") or [],
            }

        self.run_background(
            bi("正在手动压缩线程...", "Running manual thread compaction..."),
            task,
            self.on_manual_compact_complete,
        )

    def on_manual_compact_complete(self, payload: dict) -> None:
        result = payload.get("result") or {}
        status = result.get("status", "unknown")
        after_status = payload.get("after_status") or result.get("after_status") or "-"
        trace_kind = ((result.get("recent_trace") or {}).get("kind") or "-")
        self.status_var.set(f"{bi('手动压缩结果', 'Manual compaction result')}: {status} / {trace_kind} / {bi('后置状态', 'After')}: {after_status}")

        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("最近一次手动压缩结果", "Last Manual Compaction Result") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

        compact_ok = bool(result.get("ok"))
        if status == "same_model_compact_started":
            messagebox.showinfo(
                APP_TITLE,
                bi(
                    "同模型手动压缩已经真正触发。现在 Codex 里应该会出现“正在压缩上下文”。这个按钮只负责触发同模型压缩，不会等它完全结束。",
                    "The same-model manual compaction was triggered for real. Codex should now show 'Compressing context'. This button only triggers the same-model compact and does not wait for full completion.",
                ),
            )
        elif status == "same_model_compact_completed":
            messagebox.showinfo(
                APP_TITLE,
                bi(
                    "这次同模型压缩已经完成。如果聊天页还是旧画面，更像是前端没有同步。",
                    "This same-model compaction has already completed. If the chat page is still stale, that is more likely a frontend sync issue.",
                ),
            )
        elif compact_ok and after_status == "no_open_turn":
            messagebox.showinfo(
                APP_TITLE,
                bi(
                    "后台压缩已经完成。如果聊天页还是旧画面，这更像是前端没同步。当前版本先不要再用工具里的重载按钮，以免触发 `mismatched path`。",
                    "The backend compaction has finished. If the chat page is still stale, that is more likely a frontend sync issue. In the current version, do not use the GUI reload buttons again because they can trigger `mismatched path`.",
                ),
            )
        elif status == "same_model_compact_failed_after_request":
            messagebox.showwarning(
                APP_TITLE,
                bi(
                    "这次同模型压缩已经真正发到了 `/responses/compact`，但它没有成功收尾。",
                    "This same-model compact attempt really reached `/responses/compact`, but it did not complete successfully.",
                ),
            )
        elif status == "same_model_compact_no_activity":
            messagebox.showwarning(
                APP_TITLE,
                bi(
                    "这次按钮点击没有看到新的 compact 活动，说明桌面端这次没有真正开始同模型压缩。",
                    "This click did not produce new compact activity, which means the desktop did not actually start a same-model compact this time.",
                ),
            )
        elif status == "same_model_compact_trigger_failed":
            messagebox.showwarning(
                APP_TITLE,
                bi(
                    "这次按钮点击没有成功触发桌面端的同模型压缩请求。",
                    "This click did not successfully trigger the desktop same-model compact request.",
                ),
            )

        self.refresh_threads()
    def soft_reload_ui(self) -> None:
        row = self.selected_row()
        reason = frontend_reload_guard_message(row)
        if reason:
            messagebox.showwarning(APP_TITLE, reason + "\n\n" + RELOAD_DISABLED_TIP)
            return

        messagebox.showwarning(APP_TITLE, RELOAD_DISABLED_TIP)

    def restart_renderer_only(self) -> None:
        row = self.selected_row()
        reason = frontend_reload_guard_message(row)
        if reason:
            messagebox.showwarning(APP_TITLE, reason + "\n\n" + RELOAD_DISABLED_TIP)
            return

        messagebox.showwarning(APP_TITLE, RELOAD_DISABLED_TIP)

    def on_soft_reload_complete(self, payload: dict) -> None:
        self.status_var.set(bi("已发送软刷新前端", "Soft Reload UI sent"))
        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("最近一次前端软刷新", "Last Soft UI Reload") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

    def on_renderer_restart_complete(self, payload: dict) -> None:
        self.status_var.set(bi("已重载界面层", "Renderer reloaded"))
        self.details.configure(state="normal")
        self.details.insert(
            tk.END,
            "\n\n" + bi("最近一次界面层重载", "Last Renderer Reload") + ":\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        )
        self.details.see(tk.END)
        self.details.configure(state="disabled")

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

