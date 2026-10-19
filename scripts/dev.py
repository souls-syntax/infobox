"""Run both development servers and clean up their complete process groups."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_group(process: subprocess.Popen) -> None:
    if not group_exists(process.pid):
        process.poll()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and group_exists(process.pid):
        process.poll()
        time.sleep(0.1)

    if group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def main() -> int:
    backend_port, frontend_port = map(int, sys.argv[1:3])
    backend_cmd = [str(ROOT / ".venv/bin/uvicorn"), "backend.main:app", "--host", "127.0.0.1", "--port", str(backend_port)]
    frontend_cmd = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(frontend_port)]
    processes: list[tuple[str, subprocess.Popen]] = []
    interrupted = False

    def request_shutdown(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    old_int = signal.signal(signal.SIGINT, request_shutdown)
    old_term = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        project_env = {**os.environ, "LOCAL_MAIL_PROJECT_ROOT": str(ROOT)}
        backend = subprocess.Popen(backend_cmd, cwd=ROOT, env={**project_env, "FRONTEND_PORT": str(frontend_port)}, start_new_session=True)
        processes.append(("Backend", backend))
        time.sleep(0.25)
        if backend.poll() is not None:
            print("Backend failed to start; see the backend error above.", file=sys.stderr)
            return 1
        if interrupted:
            return 0

        frontend = subprocess.Popen(frontend_cmd, cwd=ROOT / "frontend", env=project_env, start_new_session=True)
        processes.append(("Frontend", frontend))

        while not interrupted:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    print(f"{name} exited unexpectedly (exit code {code}).", file=sys.stderr)
                    return 1
            time.sleep(0.2)
        return 0
    finally:
        for _, process in reversed(processes):
            stop_group(process)
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    raise SystemExit(main())
