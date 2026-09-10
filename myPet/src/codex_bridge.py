#!/usr/bin/env python3
"""Translate local Codex activity into the pet's five coarse runtime states.

The bridge prefers the official ``codex app-server proxy`` JSON-RPC stream. If
the desktop build does not expose that socket, it tails the newest local rollout
JSONL file instead. A manual JSON override written by :mod:`petctl` has highest
priority in both modes. Network access is never used.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


PET_STATES = {"idle", "working", "waiting", "success", "error"}


def derive_pet_state(threads: Iterable[dict]) -> str:
    """Map many thread statuses to one stable, priority-ordered pet state.

    Waiting wins over working, working wins over errors, and an entirely quiet
    thread list becomes idle. This avoids rapid state flicker when several Codex
    threads are present at the same time.
    """
    saw_active = False
    saw_error = False
    for thread in threads:
        status = thread.get("status") or {}
        status_type = status.get("type", "")
        flags = set(status.get("activeFlags") or [])
        if "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
            return "waiting"
        if status_type == "active":
            saw_active = True
        elif status_type in {"systemError", "error", "failed"}:
            saw_error = True
    if saw_active:
        return "working"
    if saw_error:
        return "error"
    return "idle"


@dataclass(frozen=True)
class BridgeUpdate:
    """Immutable message passed from the bridge worker to GTK's main thread."""
    state: str
    connected: bool
    detail: str


class CodexBridge:
    """Poll the running Codex daemon through ``codex app-server proxy``.

    The proxy attaches to the same local App Server control socket used by Codex.
    When that socket is unavailable, callers still receive a disconnected/idle
    update and the bridge retries without stopping the pet window.
    """

    def __init__(
        self,
        on_update: Callable[[BridgeUpdate], None],
        manual_state_path: Path,
        poll_seconds: float = 1.0,
    ) -> None:
        self.on_update = on_update
        self.manual_state_path = manual_state_path
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_update: BridgeUpdate | None = None
        self._sessions_dir = Path.home() / ".codex" / "sessions"
        self._log_path: Path | None = None
        self._log_offset = 0
        self._log_active = False
        self._log_state = "idle"
        self._log_success_until = 0.0

    def start(self) -> None:
        """Start one daemon poller; repeated launcher calls remain idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="codex-pet-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request shutdown and briefly join without blocking application exit."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _emit(self, update: BridgeUpdate) -> None:
        """Suppress duplicate UI updates when the observable state is unchanged."""
        if update != self._last_update:
            self._last_update = update
            self.on_update(update)

    def _read_manual_state(self) -> str | None:
        """Read and validate ``runtime/pet-state.json`` when petctl created it."""
        try:
            payload = json.loads(self.manual_state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        state = payload.get("state")
        return state if state in PET_STATES else None

    def _run(self) -> None:
        """Reconnect forever with bounded exponential backoff and log fallback.

        The fallback is intentionally local and read-only. It lets the pet still
        show useful activity when a desktop build does not expose App Server
        proxy access, without baking any account, machine, or network endpoint
        into the project.
        """
        retry_delay = 1.0
        while not self._stop.is_set():
            try:
                self._run_connection()
                retry_delay = 1.0
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                deadline = time.monotonic() + retry_delay
                while not self._stop.is_set() and time.monotonic() < deadline:
                    update = self._poll_rollout_log(str(exc))
                    self._emit(update)
                    self._stop.wait(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))
                retry_delay = min(retry_delay * 1.8, 10.0)

    def _poll_rollout_log(self, proxy_error: str) -> BridgeUpdate:
        """Infer task start/completion by incrementally tailing the newest session.

        ``_log_offset`` prevents re-reading the complete session on every poll.
        A short synthetic success state is emitted after clean task completion so
        the pet can briefly celebrate before returning to idle.
        """
        manual = self._read_manual_state()
        try:
            candidates = self._sessions_dir.rglob("*.jsonl")
            latest = max(candidates, key=lambda path: path.stat().st_mtime)
            if latest != self._log_path:
                self._log_path = latest
                self._log_offset = 0
                self._log_active = False
                self._log_state = "idle"

            with latest.open("r", encoding="utf-8") as handle:
                handle.seek(self._log_offset)
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload") or {}
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        self._log_active = True
                        self._log_state = "working"
                    elif event_type == "task_complete":
                        self._log_active = False
                        self._log_state = "error" if payload.get("error") else "idle"
                        if self._log_state == "idle":
                            self._log_success_until = time.monotonic() + 1.8
                self._log_offset = handle.tell()

            if manual:
                state = manual
            elif self._log_active:
                state = self._log_state
            elif time.monotonic() < self._log_success_until:
                state = "success"
            else:
                state = self._log_state
            return BridgeUpdate(
                state=state,
                connected=True,
                detail=f"Linked through Codex session log ({latest.name})",
            )
        except (ValueError, OSError) as log_error:
            return BridgeUpdate(
                state=manual or "idle",
                connected=False,
                detail=f"App Server: {proxy_error}; session log: {log_error}",
            )

    def _run_connection(self) -> None:
        """Run one JSON-RPC proxy session until failure or requested shutdown."""
        proc = subprocess.Popen(
            ["codex", "app-server", "proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("failed to open App Server pipes")

        incoming: queue.Queue[dict | None] = queue.Queue()

        def read_output() -> None:
            """Decode newline-delimited JSON without blocking the control loop."""
            try:
                for line in proc.stdout:
                    try:
                        incoming.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            finally:
                incoming.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        next_id = 1

        def send(method: str, params: dict | None = None, request_id: int | None = None) -> None:
            """Write one compact JSON-RPC request/notification to the proxy."""
            message: dict = {"method": method}
            if params is not None:
                message["params"] = params
            if request_id is not None:
                message["id"] = request_id
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()

        def wait_response(request_id: int, timeout: float = 4.0) -> dict:
            """Wait for a matching response while safely ignoring notifications."""
            deadline = time.monotonic() + timeout
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    message = incoming.get(timeout=max(0.05, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if message is None:
                    stderr = proc.stderr.read().strip() if proc.stderr else ""
                    raise RuntimeError(stderr or "App Server proxy exited")
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError(str(message["error"]))
                    return message.get("result") or {}
            raise RuntimeError(f"App Server request {request_id} timed out")

        try:
            send(
                "initialize",
                {
                    "clientInfo": {
                        "name": "swing_pet",
                        "title": "Swing Pet",
                        "version": "0.1.0",
                    }
                },
                next_id,
            )
            wait_response(next_id)
            next_id += 1
            send("initialized", {})

            previous_state = "idle"
            success_until = 0.0
            while not self._stop.is_set():
                manual = self._read_manual_state()
                send("thread/list", {"limit": 50, "archived": False}, next_id)
                result = wait_response(next_id)
                next_id += 1
                threads = result.get("data") or []
                app_state = derive_pet_state(threads)

                now = time.monotonic()
                if previous_state in {"working", "waiting"} and app_state == "idle":
                    success_until = now + 1.8
                previous_state = app_state
                state = manual or ("success" if now < success_until else app_state)
                self._emit(
                    BridgeUpdate(
                        state=state,
                        connected=True,
                        detail=f"Connected to Codex; {len(threads)} threads observed",
                    )
                )
                self._stop.wait(self.poll_seconds)
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
