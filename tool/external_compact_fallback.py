from __future__ import annotations

import argparse
import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from .unstick_thread import connect_sqlite
except ImportError:
    from unstick_thread import connect_sqlite


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def desktop_path(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text.replace("/", "\\")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume a thread in an independent local app-server with a fallback model, then trigger compact."
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--fallback-model", default="gpt-5.4")
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def ensure_output_dir(codex_home: Path, explicit: str) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
    else:
        path = codex_home.parent / "codex-thread-rescue-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def safe_print_json(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        import sys

        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))


def load_thread_row(codex_home: Path, thread_id: str) -> dict:
    conn = connect_sqlite(codex_home / "state_5.sqlite")
    try:
        row = conn.execute("select * from threads where id = ?", (thread_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Thread not found: {thread_id}")
        return dict(row)
    finally:
        conn.close()


@dataclass
class ServerEvent:
    stream: str
    raw: str
    payload: dict | None


class AppServerClient:
    def __init__(self) -> None:
        popen_kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            ["cmd.exe", "/c", "codex", "app-server", "--listen", "stdio://"],
            **popen_kwargs,
        )
        self.events: queue.Queue[ServerEvent] = queue.Queue()
        self._start_reader(self.process.stdout, "stdout")
        self._start_reader(self.process.stderr, "stderr")
        self._initialize()

    def _start_reader(self, stream, name: str) -> None:
        def reader() -> None:
            while True:
                line = stream.readline()
                if line == "":
                    self.events.put(ServerEvent(name, "", None))
                    return
                payload = None
                if name == "stdout":
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        payload = None
                self.events.put(ServerEvent(name, line.rstrip("\n"), payload))

        threading.Thread(target=reader, daemon=True).start()

    def _write(self, payload: dict) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app-server stdin is unavailable")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-repair-probe",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
            request_id="init-1",
            timeout=20,
        )
        self.notify("initialized")

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method: str, params: dict, *, request_id: str, timeout: int) -> tuple[dict, list[dict], list[str]]:
        self._write({"id": request_id, "method": method, "params": params})
        notifications: list[dict] = []
        stderr_lines: list[str] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                event = self.events.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue

            if event.payload is None:
                if event.stream == "stderr" and event.raw:
                    stderr_lines.append(event.raw)
                    continue
                raise RuntimeError(
                    f"app-server {event.stream} closed while waiting for {request_id}; stderr_tail={stderr_lines[-3:]}"
                )

            if event.stream == "stderr":
                stderr_lines.append(event.raw)
                continue

            payload = event.payload
            if payload.get("id") == request_id:
                return payload, notifications, stderr_lines

            if payload.get("method"):
                notifications.append(payload)

        raise TimeoutError(f"Timed out waiting for {request_id}; stderr_tail={stderr_lines[-3:]}")

    def wait_for_compact(self, *, thread_id: str, timeout: int) -> tuple[dict, list[dict], list[str]]:
        deadline = time.time() + timeout
        notifications: list[dict] = []
        stderr_lines: list[str] = []
        saw_compaction_item_started = False
        saw_compaction_item_completed = False
        saw_thread_idle_after_compaction = False
        compaction_turn_id: str | None = None

        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                event = self.events.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue

            if event.payload is None:
                if event.stream == "stderr" and event.raw:
                    stderr_lines.append(event.raw)
                    continue
                return {
                    "status": "stream_closed",
                    "ok": False,
                }, notifications, stderr_lines

            if event.stream == "stderr":
                stderr_lines.append(event.raw)
                continue

            payload = event.payload
            method = payload.get("method")
            params = payload.get("params") or {}
            notifications.append(payload)

            if method == "thread/compacted" and params.get("threadId") == thread_id:
                return {
                    "status": "thread_compacted",
                    "ok": True,
                    "turn_id": params.get("turnId"),
                }, notifications, stderr_lines

            if params.get("threadId") != thread_id:
                continue

            if method == "turn/started":
                turn = params.get("turn") or {}
                if turn.get("id"):
                    compaction_turn_id = turn.get("id")
                continue

            if method == "item/started":
                item = params.get("item") or {}
                if item.get("type") == "contextCompaction":
                    saw_compaction_item_started = True
                    compaction_turn_id = params.get("turnId") or compaction_turn_id
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "contextCompaction":
                    saw_compaction_item_completed = True
                    compaction_turn_id = params.get("turnId") or compaction_turn_id
                    if saw_thread_idle_after_compaction:
                        return {
                            "status": "item_completed_then_thread_idle",
                            "ok": True,
                            "turn_id": compaction_turn_id,
                        }, notifications, stderr_lines
                continue

            if method == "thread/status/changed":
                status = params.get("status") or {}
                if status.get("type") == "idle" and (saw_compaction_item_started or saw_compaction_item_completed):
                    saw_thread_idle_after_compaction = True
                    if saw_compaction_item_completed:
                        return {
                            "status": "item_completed_then_thread_idle",
                            "ok": True,
                            "turn_id": compaction_turn_id,
                        }, notifications, stderr_lines
                continue

            if method == "turn/completed" and params.get("threadId") == thread_id:
                turn = params.get("turn") or {}
                items = turn.get("items") or []
                if any(item.get("type") == "contextCompaction" for item in items):
                    return {
                        "status": "turn_completed_with_context_compaction",
                        "ok": True,
                        "turn_id": turn.get("id"),
                        "turn_status": turn.get("status"),
                    }, notifications, stderr_lines
                if (
                    turn.get("status") == "completed"
                    and (saw_compaction_item_started or saw_compaction_item_completed)
                    and (compaction_turn_id is None or turn.get("id") == compaction_turn_id)
                ):
                    return {
                        "status": "turn_completed_after_context_compaction_item",
                        "ok": True,
                        "turn_id": turn.get("id"),
                        "turn_status": turn.get("status"),
                    }, notifications, stderr_lines
                if turn.get("status") in {"failed", "interrupted"}:
                    return {
                        "status": "turn_completed_without_compaction",
                        "ok": False,
                        "turn_id": turn.get("id"),
                        "turn_status": turn.get("status"),
                        "turn_error": turn.get("error"),
                    }, notifications, stderr_lines

            if method == "warning" and params.get("threadId") == thread_id:
                message = (params.get("message") or "").lower()
                if "compact" in message or "compaction" in message:
                    return {
                        "status": "warning",
                        "ok": False,
                        "message": params.get("message"),
                    }, notifications, stderr_lines

            if method == "error":
                message = (params.get("message") or "")
                if "compact" in message.lower():
                    return {
                        "status": "error",
                        "ok": False,
                        "message": message,
                    }, notifications, stderr_lines

        return {
            "status": "timeout_waiting_compact",
            "ok": False,
        }, notifications, stderr_lines

    def close(self) -> None:
        try:
            self.process.terminate()
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def trim_notifications(items: list[dict], limit: int = 30) -> list[dict]:
    return items[-limit:]


def run_external_compact_fallback(
    *,
    codex_home: Path,
    thread_id: str,
    fallback_model: str = "gpt-5.4",
    timeout_seconds: int = 120,
    output_dir: Path | None = None,
) -> dict:
    codex_home = Path(codex_home).expanduser()
    output_dir = ensure_output_dir(codex_home, str(output_dir) if output_dir else "")
    thread = load_thread_row(codex_home, thread_id)
    resume_path = desktop_path(thread.get("rollout_path"))
    resume_cwd = desktop_path(thread.get("cwd"))
    result = {
        "timestamp": now_iso(),
        "thread_id": thread_id,
        "title": thread.get("title") or "",
        "original_model": thread.get("model") or "",
        "original_reasoning_effort": thread.get("reasoning_effort") or "",
        "fallback_model": fallback_model,
        "state_before": {
            "model": thread.get("model"),
            "reasoning_effort": thread.get("reasoning_effort"),
            "cwd": thread.get("cwd"),
            "rollout_path": thread.get("rollout_path"),
            "resume_cwd": resume_cwd,
            "resume_path": resume_path,
        },
    }

    client = AppServerClient()
    try:
        read_response, read_notifications, read_stderr = client.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": False,
            },
            request_id="read-1",
            timeout=30,
        )
        read_thread = (read_response.get("result") or {}).get("thread") or {}
        result["thread_read"] = {
            "preview": read_thread.get("preview"),
            "status": read_thread.get("status"),
            "cwd": read_thread.get("cwd"),
            "notifications": trim_notifications(read_notifications),
            "stderr_tail": read_stderr[-5:],
        }

        resume_response, resume_notifications, resume_stderr = client.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "history": None,
                "path": resume_path,
                "model": fallback_model,
                "modelProvider": None,
                "cwd": resume_cwd,
                "approvalPolicy": None,
                "sandbox": None,
                "config": None,
                "personality": None,
            },
            request_id="resume-1",
            timeout=45,
        )
        resume_result = resume_response.get("result") or {}
        result["resume"] = {
            "model": resume_result.get("model"),
            "reasoning_effort": resume_result.get("reasoningEffort"),
            "thread_status": (resume_result.get("thread") or {}).get("status"),
            "notifications": trim_notifications(resume_notifications),
            "stderr_tail": resume_stderr[-5:],
        }

        compact_response, compact_notifications, compact_stderr = client.request(
            "thread/compact/start",
            {
                "threadId": thread_id,
            },
            request_id="compact-1",
            timeout=20,
        )
        result["compact_request"] = {
            "response": compact_response,
            "notifications": trim_notifications(compact_notifications),
            "stderr_tail": compact_stderr[-5:],
        }

        compact_outcome, outcome_notifications, outcome_stderr = client.wait_for_compact(
            thread_id=thread_id,
            timeout=timeout_seconds,
        )
        result["compact_outcome"] = {
            **compact_outcome,
            "notifications": trim_notifications(outcome_notifications),
            "stderr_tail": outcome_stderr[-8:],
        }

        state_after = load_thread_row(codex_home, thread_id)
        result["state_after"] = {
            "model": state_after.get("model"),
            "reasoning_effort": state_after.get("reasoning_effort"),
            "updated_at": state_after.get("updated_at"),
            "updated_at_ms": state_after.get("updated_at_ms"),
        }
        result["status"] = "compact_succeeded" if compact_outcome.get("ok") else "compact_failed"
    except Exception as exc:
        result["status"] = "compact_failed"
        result["exception"] = str(exc)
    finally:
        client.close()

    append_jsonl(output_dir / "external_compact_fallback.jsonl", result)
    return result


def main() -> int:
    args = parse_args()
    result = run_external_compact_fallback(
        codex_home=Path(args.codex_home),
        thread_id=args.thread_id,
        fallback_model=args.fallback_model,
        timeout_seconds=args.timeout_seconds,
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
    )
    safe_print_json(result)
    return 0 if result.get("status") == "compact_succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
