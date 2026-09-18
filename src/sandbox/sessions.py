from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.paths import changed_files, snapshot, workspace_dir

IDLE_KERNEL_SECONDS = int(os.environ.get("SANDBOX_IDLE_KERNEL_SECONDS", "900"))
IDLE_WORKSPACE_SECONDS = int(os.environ.get("SANDBOX_IDLE_WORKSPACE_SECONDS", "3600"))
MAX_KERNELS = int(os.environ.get("SANDBOX_MAX_KERNELS", "32"))
WORKER_MODULE = "sandbox.worker"


def _drop_network() -> None:
    try:
        os.unshare(os.CLONE_NEWNET)  # type: ignore[attr-defined]
    except (AttributeError, OSError, PermissionError):
        return


@dataclass
class Session:
    session_id: str
    workspace: Path
    process: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.time)

    async def ensure_worker(self) -> asyncio.subprocess.Process:
        process = self.process
        if process is not None and process.returncode is None:
            return process
        src_root = str(Path(__file__).resolve().parent.parent)
        pythonpath = os.pathsep.join(
            [src_root, os.environ.get("PYTHONPATH", "")],
        ).strip(os.pathsep)
        env = {
            **os.environ,
            "SANDBOX_WORKSPACE": str(self.workspace),
            "HOME": str(self.workspace),
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": pythonpath,
        }
        created = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.workspace),
            env=env,
            preexec_fn=_drop_network if os.name == "posix" else None,
        )
        self.process = created
        return created

    async def kill_worker(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError, OSError):
            process.send_signal(signal.SIGKILL)
        with suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=2)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> Session:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_used = time.time()
                return existing
            if len(self._sessions) >= MAX_KERNELS:
                oldest = min(self._sessions.values(), key=lambda item: item.last_used)
                await self._evict_locked(oldest.session_id, delete_workspace=False)
            session = Session(session_id=session_id, workspace=workspace_dir(session_id))
            self._sessions[session_id] = session
            return session

    async def execute(
        self,
        session_id: str,
        code: str,
        timeout_ms: int,
    ) -> dict[str, object]:
        session = await self.get(session_id)
        timeout_s = max(1, min(timeout_ms, 120_000)) / 1000
        async with session.lock:
            before = snapshot(session.workspace)
            process = await session.ensure_worker()
            stdin = process.stdin
            stdout = process.stdout
            if stdin is None or stdout is None:
                await session.kill_worker()
                return {
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Python worker is not available",
                    "timedOut": False,
                    "files": [],
                }
            stdin.write((json.dumps({"cmd": "exec", "code": code}) + "\n").encode("utf-8"))
            await stdin.drain()
            try:
                line = await asyncio.wait_for(stdout.readline(), timeout=timeout_s)
            except TimeoutError:
                await session.kill_worker()
                return {
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Execution timed out",
                    "timedOut": True,
                    "files": changed_files(session.workspace, before),
                }
            session.last_used = time.time()
            if len(line) == 0:
                await session.kill_worker()
                return {
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Python worker exited",
                    "timedOut": False,
                    "files": [],
                }
            body = json.loads(line.decode("utf-8"))
            return {
                "exitCode": int(body.get("exit_code", 1)),
                "stdout": str(body.get("stdout", "")),
                "stderr": str(body.get("stderr", "")),
                "timedOut": False,
                "files": changed_files(session.workspace, before),
            }

    async def is_alive(self, session_id: str) -> bool:
        async with self._lock:
            return session_id in self._sessions

    async def delete(self, session_id: str, *, delete_workspace: bool) -> None:
        async with self._lock:
            await self._evict_locked(session_id, delete_workspace=delete_workspace)

    async def _evict_locked(self, session_id: str, *, delete_workspace: bool) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            if delete_workspace:
                shutil.rmtree(workspace_dir(session_id, create=False), ignore_errors=True)
            return
        await session.kill_worker()
        if delete_workspace:
            shutil.rmtree(session.workspace, ignore_errors=True)

    async def reap(self) -> None:
        now = time.time()
        async with self._lock:
            stale = [
                session.session_id
                for session in list(self._sessions.values())
                if now - session.last_used > IDLE_KERNEL_SECONDS
            ]
            for session_id in stale:
                session = self._sessions.get(session_id)
                if session is None:
                    continue
                delete_workspace = now - session.last_used > IDLE_WORKSPACE_SECONDS
                if delete_workspace:
                    await self._evict_locked(session_id, delete_workspace=True)
                else:
                    await session.kill_worker()

    async def close(self) -> None:
        async with self._lock:
            for session_id in list(self._sessions):
                await self._evict_locked(session_id, delete_workspace=False)


manager = SessionManager()
