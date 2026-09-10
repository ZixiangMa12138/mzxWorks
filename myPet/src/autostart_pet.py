#!/usr/bin/env python3
"""Idempotently launch Swing Pet for Codex hooks and desktop shortcuts.

The launcher holds a file lock while checking the PID file, verifies the PID's
actual ``/proc`` command line, starts the GUI in a detached process group, then
returns immediately. Both Codex hooks and repeated desktop double-clicks can call
this file safely without creating duplicate pets.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path


# Resolve from this file, not from the caller. A Hook and a .desktop launcher
# are commonly invoked from unrelated working directories after the repo moves.
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
PID_PATH = RUNTIME_DIR / "standalone-pet.pid"
LOCK_PATH = RUNTIME_DIR / "standalone-pet.lock"
LOG_PATH = RUNTIME_DIR / "standalone-pet.log"
PET_SCRIPT = ROOT / "src" / "standalone_pet.py"
PROCESS_NAME = "PetCat"


def is_our_pet(pid: int) -> bool:
    """Reject stale/reused PIDs unless their command line names this pet script."""
    if pid <= 1:
        return False
    try:
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return str(PET_SCRIPT).encode() in command


def read_running_pid() -> int | None:
    """Return the verified running PID, or ``None`` for stale/missing state."""
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    return pid if is_our_pet(pid) else None


def emit_hook_result() -> None:
    """Emit the small response Codex expects from a non-blocking startup hook."""
    print(json.dumps({"continue": True, "suppressOutput": True}))


def main() -> int:
    """Serialize launch attempts, detach one GUI process, and persist its PID."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        running_pid = read_running_pid()
        if running_pid is not None:
            emit_hook_result()
            return 0

        # Preserve DISPLAY, Wayland and optional SWING_PET_INITIAL_AFFECTION from
        # the caller; unbuffered logs make startup failures visible immediately.
        # All generated state remains under ROOT/runtime and is intentionally
        # disposable, so cloning the project never carries another host's PID.
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        with LOG_PATH.open("a", encoding="utf-8") as log:
            # No terminal/stdin is inherited. ``start_new_session`` also prevents
            # Codex or the desktop launcher from terminating the pet on exit.
            # ``exec -a`` provides a human-readable argv[0] in tools such as
            # ``ps -ef``. The pet process also sets Linux's shorter ``comm``
            # field itself, while ``is_our_pet`` continues to verify the script
            # path so the friendly name cannot cause a false PID match.
            process = subprocess.Popen(
                ["bash", "-c", 'exec -a "$0" "$@"', PROCESS_NAME, sys.executable, str(PET_SCRIPT)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                env=environment,
            )
        PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
        emit_hook_result()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
