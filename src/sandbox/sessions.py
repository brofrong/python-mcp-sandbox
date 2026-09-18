from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from io import BufferedReader
from pathlib import Path

from sandbox.isolate import worker_env
from sandbox.paths import changed_files, data_root, snapshot, touch_workspace, workspace_dir

IDLE_KERNEL_SECONDS = int(os.environ.get("SANDBOX_IDLE_KERNEL_SECONDS", "900"))
IDLE_WORKSPACE_SECONDS = int(os.environ.get("SANDBOX_IDLE_WORKSPACE_SECONDS", "900"))
MAX_KERNELS = int(os.environ.get("SANDBOX_MAX_KERNELS", "32"))
WORKER_MODULE = "sandbox.worker"


@dataclass
class Session:
    session_id: str
    workspace: Path
    process: asyncio.subprocess.Process | None = None
    result_file: BufferedReader | None = None
    stderr_task: asyncio.Task[None] | None = None
    last_used: float = field(default_factory=time.time)

    async def ensure_worker(self) -> asyncio.subprocess.Process:
        process = self.process
        if process is not None and process.returncode is None and self.result_file is not None:
            return process
        await self.kill_worker()
        src_root = str(Path(__file__).resolve().parent.parent)
        result_r, result_w = os.pipe()
        os.set_inheritable(result_w, True)
        try:
            created = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                WORKER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=worker_env(
                    workspace=str(self.workspace),
                    pythonpath=src_root,
                    result_fd=result_w,
                ),
                pass_fds=(result_w,),
            )
        except Exception:
            os.close(result_r)
            os.close(result_w)
            raise
        os.close(result_w)
        self.process = created
        self.result_file = os.fdopen(result_r, "rb")
        self.stderr_task = asyncio.create_task(_drain_stderr(created))
        return created

    async def kill_worker(self) -> None:
        process = self.process
        result_file = self.result_file
        stderr_task = self.stderr_task
        self.process = None
        self.result_file = None
        self.stderr_task = None
        if result_file is not None:
            result_file.close()
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError, OSError):
                process.send_signal(signal.SIGKILL)
            with suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=2)
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    stream = process.stderr
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._op_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def operation(self, session_id: str) -> AsyncIterator[None]:
        lock = self._op_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield

    async def get(self, session_id: str) -> Session:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_used = time.time()
                touch_workspace(existing.workspace)
                return existing
            if len(self._sessions) >= MAX_KERNELS:
                oldest = min(self._sessions.values(), key=lambda item: item.last_used)
                await self._evict_locked(oldest.session_id, delete_workspace=False)
            session = Session(session_id=session_id, workspace=workspace_dir(session_id))
            touch_workspace(session.workspace)
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
        async with self.operation(session_id):
            before = snapshot(session.workspace)
            process = await session.ensure_worker()
            stdin = process.stdin
            if stdin is None or session.result_file is None:
                await session.kill_worker()
                return _execute_error("Python worker is not available")
            try:
                body = await self._rpc(
                    session,
                    {"cmd": "exec", "code": code},
                    timeout_s,
                )
            except TimeoutError:
                await session.kill_worker()
                return {
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": "Execution timed out",
                    "timedOut": True,
                    "files": changed_files(session.workspace, before),
                }
            except (json.JSONDecodeError, ValueError, OSError):
                await session.kill_worker()
                return _execute_error("Python worker protocol error")
            session.last_used = time.time()
            touch_workspace(session.workspace)
            if body.get("ok") is not True:
                return _execute_error(str(body.get("error", "Python worker exited")))
            return {
                "exitCode": int(body.get("exit_code", 1)),
                "stdout": str(body.get("stdout", "")),
                "stderr": str(body.get("stderr", "")),
                "timedOut": False,
                "files": changed_files(session.workspace, before),
            }

    async def _rpc(
        self,
        session: Session,
        payload: dict[str, object],
        timeout_s: float,
    ) -> dict[str, object]:
        process = session.process
        stdin = None if process is None else process.stdin
        result_file = session.result_file
        if process is None or stdin is None or result_file is None:
            raise OSError("worker is not available")
        nonce = secrets.token_hex(16)
        message = dict(payload)
        message["nonce"] = nonce
        stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await stdin.drain()
        loop = asyncio.get_running_loop()
        line = await asyncio.wait_for(
            loop.run_in_executor(None, result_file.readline),
            timeout=timeout_s,
        )
        if len(line) == 0:
            raise OSError("Python worker exited")
        body = json.loads(line.decode("utf-8"))
        if not isinstance(body, dict) or body.get("nonce") != nonce:
            raise ValueError("worker protocol mismatch")
        return body

    async def is_alive(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            process = session.process
            return process is not None and process.returncode is None

    async def delete(self, session_id: str, *, delete_workspace: bool) -> None:
        async with self.operation(session_id):
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
                lock = self._op_locks.get(session_id)
                if lock is not None and lock.locked():
                    continue
                delete_workspace = now - session.last_used > IDLE_WORKSPACE_SECONDS
                if delete_workspace:
                    await self._evict_locked(session_id, delete_workspace=True)
                else:
                    await session.kill_worker()
            await self._reap_orphan_workspaces_locked(now)

    async def _reap_orphan_workspaces_locked(self, now: float) -> None:
        root = data_root() / "workspaces"
        if not root.is_dir():
            return
        for path in root.iterdir():
            if not path.is_dir() or path.name in self._sessions:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > IDLE_WORKSPACE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)

    async def close(self) -> None:
        async with self._lock:
            for session_id in list(self._sessions):
                await self._evict_locked(session_id, delete_workspace=False)


def _execute_error(message: str) -> dict[str, object]:
    return {
        "exitCode": 1,
        "stdout": "",
        "stderr": message,
        "timedOut": False,
        "files": [],
    }


manager = SessionManager()
