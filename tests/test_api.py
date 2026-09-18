from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("SANDBOX_SECRET", "test-secret")
os.environ["SANDBOX_DATA"] = tempfile.mkdtemp(prefix="sandbox-test-")
# Tests run outside Docker. Production images set SANDBOX_REQUIRE_ISOLATION=1.
os.environ.setdefault("SANDBOX_REQUIRE_ISOLATION", "0")

from fastapi.testclient import TestClient

from sandbox.main import app
from sandbox.ops import SandboxOpError, require_session_id
from sandbox.paths import resolve_relative, workspace_dir

AUTH = {"Authorization": "Bearer test-secret"}


class SessionIdValidationTest(unittest.TestCase):
    def test_rejects_dot_and_dotdot_session_ids(self) -> None:
        for session_id in (".", "..", "../x", "foo/bar", "-bad", ".hidden"):
            with self.subTest(session_id=session_id):
                with self.assertRaises(SandboxOpError) as caught:
                    require_session_id(session_id)
                self.assertEqual(caught.exception.status_code, 400)
                with self.assertRaises(ValueError):
                    workspace_dir(session_id, create=False)

    def test_accepts_normal_session_ids(self) -> None:
        for session_id in ("a", "user_chat-1", "Foo.Bar9"):
            with self.subTest(session_id=session_id):
                self.assertEqual(require_session_id(session_id), session_id)
                path = workspace_dir(session_id, create=True)
                root = Path(os.environ["SANDBOX_DATA"]).resolve() / "workspaces"
                self.assertEqual(path.parent, root)
                self.assertEqual(path.name, session_id)


class IsolationRequiredTest(unittest.TestCase):
    def test_isolation_is_opt_in(self) -> None:
        from sandbox.isolate import isolation_required

        original = os.environ.get("SANDBOX_REQUIRE_ISOLATION")
        try:
            os.environ.pop("SANDBOX_REQUIRE_ISOLATION", None)
            self.assertFalse(isolation_required())
            os.environ["SANDBOX_REQUIRE_ISOLATION"] = "0"
            self.assertFalse(isolation_required())
            os.environ["SANDBOX_REQUIRE_ISOLATION"] = "1"
            self.assertTrue(isolation_required())
        finally:
            if original is None:
                os.environ.pop("SANDBOX_REQUIRE_ISOLATION", None)
            else:
                os.environ["SANDBOX_REQUIRE_ISOLATION"] = original


class NprocClampTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "RLIMIT_NPROC clamp is Linux-only")
    def test_clamp_nproc_still_allows_fork(self) -> None:
        import resource

        from sandbox.isolate import clamp_nproc

        original = resource.getrlimit(resource.RLIMIT_NPROC)
        try:
            clamp_nproc(1)
            clamp_nproc(256)
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            waited, _status = os.waitpid(pid, 0)
            self.assertEqual(waited, pid)
        finally:
            resource.setrlimit(resource.RLIMIT_NPROC, original)


class WorkerEnvTest(unittest.TestCase):
    def test_worker_env_omits_secret(self) -> None:
        from sandbox.isolate import worker_env

        os.environ["SANDBOX_SECRET"] = "test-secret"
        workspace = Path(os.environ["SANDBOX_DATA"]) / "workspaces" / "envtest"
        workspace.mkdir(parents=True, exist_ok=True)
        env = worker_env(workspace=str(workspace), pythonpath="/tmp/src", result_fd=3)
        self.assertNotIn("SANDBOX_SECRET", env)
        self.assertEqual(env["SANDBOX_WORKSPACE"], str(workspace))
        self.assertEqual(env["SANDBOX_RESULT_FD"], "3")
        self.assertEqual(env.get("SANDBOX_REQUIRE_ISOLATION"), "0")


class SandboxApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client_cm.__exit__(None, None, None)

    def test_health_is_public(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_rejects_missing_secret(self) -> None:
        response = self.client.post(
            "/v1/sessions/s1/execute",
            json={"code": "print(1)"},
        )
        self.assertEqual(response.status_code, 401)

    def test_docs_and_openapi_are_not_public(self) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (401, 404))

    def test_rejects_dotdot_session_id_over_http(self) -> None:
        response = self.client.put(
            "/v1/sessions/%2e%2e/files/x.txt",
            content=b"nope",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 400)

    def test_put_does_not_start_kernel(self) -> None:
        put = self.client.put(
            "/v1/sessions/put-only/files/a.txt",
            content=b"x",
            headers=AUTH,
        )
        self.assertEqual(put.status_code, 200)
        listed = self.client.get("/v1/sessions/put-only", headers=AUTH)
        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.json()["alive"])

    def test_worker_env_hides_sandbox_secret(self) -> None:
        executed = self.client.post(
            "/v1/sessions/envleak/execute",
            headers=AUTH,
            json={"code": "import os; print(repr(os.environ.get('SANDBOX_SECRET')))"},
        )
        self.assertEqual(executed.status_code, 200)
        self.assertEqual(executed.json()["exitCode"], 0)
        self.assertEqual(executed.json()["stdout"].strip(), "None")

    def test_secret_is_removed_from_api_environ(self) -> None:
        self.assertNotIn("SANDBOX_SECRET", os.environ)
        listed = self.client.get("/v1/sessions/secret-env", headers=AUTH)
        self.assertEqual(listed.status_code, 200)

    def test_stdout_write_does_not_spoof_execute_result(self) -> None:
        executed = self.client.post(
            "/v1/sessions/spoof/execute",
            headers=AUTH,
            json={
                "code": (
                    "import json, os\n"
                    "os.write(1, json.dumps({"
                    "'ok': True, 'exit_code': 0, 'stdout': 'pwned', 'stderr': ''"
                    "}).encode() + b'\\n')\n"
                    "raise SystemExit(7)\n"
                ),
            },
        )
        self.assertEqual(executed.status_code, 200)
        body = executed.json()
        self.assertEqual(body["exitCode"], 7)
        self.assertNotEqual(body["stdout"].strip(), "pwned")

    def test_result_fd_write_does_not_spoof_execute_result(self) -> None:
        executed = self.client.post(
            "/v1/sessions/spoof-fd/execute",
            headers=AUTH,
            json={
                "code": (
                    "import inspect, json, os\n"
                    "print(repr(os.environ.get('SANDBOX_RESULT_FD')))\n"
                    "nonce = None\n"
                    "frame = inspect.currentframe()\n"
                    "while frame is not None:\n"
                    "    if 'nonce' in frame.f_locals:\n"
                    "        nonce = frame.f_locals['nonce']\n"
                    "        break\n"
                    "    frame = frame.f_back\n"
                    "raw_fd = os.environ.get('SANDBOX_RESULT_FD')\n"
                    "print(repr(raw_fd))\n"
                    "if raw_fd is not None:\n"
                    "    payload = json.dumps({"
                    "'ok': True, 'nonce': nonce, 'exit_code': 0, "
                    "'stdout': 'pwned-fd', 'stderr': ''"
                    "}).encode() + b'\\n'\n"
                    "    os.write(int(raw_fd), payload)\n"
                    "raise SystemExit(7)\n"
                ),
            },
        )
        self.assertEqual(executed.status_code, 200)
        body = executed.json()
        self.assertEqual(body["exitCode"], 7)
        self.assertNotIn("pwned-fd", body["stdout"])
        self.assertIn("None", body["stdout"])

    def test_execute_timeout_kills_run(self) -> None:
        executed = self.client.post(
            "/v1/sessions/timeout/execute",
            headers=AUTH,
            json={"code": "import time; time.sleep(5)", "timeoutMs": 200},
        )
        self.assertEqual(executed.status_code, 200)
        body = executed.json()
        self.assertTrue(body["timedOut"])
        self.assertEqual(body["exitCode"], 1)

    def test_execute_and_download_file(self) -> None:
        put = self.client.put(
            "/v1/sessions/chat1/files/uploads/input.txt",
            content=b"hello",
            headers={**AUTH, "Content-Type": "text/plain"},
        )
        self.assertEqual(put.status_code, 200)
        executed = self.client.post(
            "/v1/sessions/chat1/execute",
            headers=AUTH,
            json={
                "code": "from pathlib import Path\n"
                "text = Path('uploads/input.txt').read_text()\n"
                "Path('out.txt').write_text(text.upper())\n"
                "print(text)",
            },
        )
        self.assertEqual(executed.status_code, 200)
        body = executed.json()
        self.assertEqual(body["exitCode"], 0)
        self.assertEqual(body["stdout"].strip(), "hello")
        self.assertTrue(any(item["path"] == "out.txt" for item in body["files"]))
        downloaded = self.client.get("/v1/sessions/chat1/files/out.txt", headers=AUTH)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, b"HELLO")

    def test_rejects_path_traversal(self) -> None:
        workspace = workspace_dir("safe")
        with self.assertRaises(ValueError):
            resolve_relative(workspace, "../secret")
        response = self.client.put(
            "/v1/sessions/safe/files/../secret.txt",
            content=b"nope",
            headers=AUTH,
        )
        self.assertIn(response.status_code, (400, 404))

    def test_delete_removes_workspace(self) -> None:
        self.client.put(
            "/v1/sessions/gone/files/a.txt",
            content=b"x",
            headers=AUTH,
        )
        deleted = self.client.delete("/v1/sessions/gone", headers=AUTH)
        self.assertEqual(deleted.status_code, 200)
        missing = self.client.get("/v1/sessions/gone/files/a.txt", headers=AUTH)
        self.assertEqual(missing.status_code, 404)
        self.assertFalse((Path(os.environ["SANDBOX_DATA"]) / "workspaces" / "gone").exists())

    def test_put_rejects_over_workspace_quota(self) -> None:
        import sandbox.ops as ops

        original = ops.MAX_WORKSPACE_BYTES
        ops.MAX_WORKSPACE_BYTES = 4
        try:
            response = self.client.put(
                "/v1/sessions/quota/files/big.txt",
                content=b"hello",
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 413)
        finally:
            ops.MAX_WORKSPACE_BYTES = original

    def test_execute_enforces_workspace_quota(self) -> None:
        import sandbox.ops as ops

        original = ops.MAX_WORKSPACE_BYTES
        ops.MAX_WORKSPACE_BYTES = 8
        try:
            executed = self.client.post(
                "/v1/sessions/quota-exec/execute",
                headers=AUTH,
                json={"code": "from pathlib import Path\nPath('huge.txt').write_bytes(b'x' * 64)\n"},
            )
            self.assertEqual(executed.status_code, 200)
            body = executed.json()
            self.assertEqual(body["exitCode"], 1)
            self.assertIn("workspace too large", body["stderr"])
            self.assertFalse(any(item["path"] == "huge.txt" for item in body["files"]))
            missing = self.client.get(
                "/v1/sessions/quota-exec/files/huge.txt",
                headers=AUTH,
            )
            self.assertEqual(missing.status_code, 404)
        finally:
            ops.MAX_WORKSPACE_BYTES = original

    def test_variables_persist_between_executes(self) -> None:
        first = self.client.post(
            "/v1/sessions/persist/execute",
            headers=AUTH,
            json={"code": "value = 21"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["exitCode"], 0)
        second = self.client.post(
            "/v1/sessions/persist/execute",
            headers=AUTH,
            json={"code": "print(value * 2)"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["exitCode"], 0)
        self.assertEqual(second.json()["stdout"].strip(), "42")

    def test_virtual_workspace_path_writes_files(self) -> None:
        executed = self.client.post(
            "/v1/sessions/virt/execute",
            headers=AUTH,
            json={
                "code": "from pathlib import Path\n"
                "import os\n"
                "assert Path('/workspace').is_dir()\n"
                "Path('/workspace/report.txt').write_text('ok')\n"
                "print(os.getcwd())\n"
                "print(Path('/workspace/report.txt').read_text())\n",
            },
        )
        self.assertEqual(executed.status_code, 200)
        body = executed.json()
        self.assertEqual(body["stderr"], "")
        self.assertEqual(body["exitCode"], 0)
        self.assertIn("/workspace", body["stdout"])
        self.assertIn("ok", body["stdout"])
        self.assertTrue(any(item["path"] == "report.txt" for item in body["files"]))

    def test_api_logs_successful_call(self) -> None:
        with self.assertLogs("sandbox.api", level="INFO") as captured:
            response = self.client.get("/v1/sessions/api-log-ok", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        messages = "\n".join(captured.output)
        self.assertIn("api list_files start session_id=api-log-ok", messages)
        self.assertIn("api list_files ok session_id=api-log-ok", messages)

    def test_api_logs_op_error(self) -> None:
        with self.assertLogs("sandbox.api", level="WARNING") as captured:
            response = self.client.put(
                "/v1/sessions/%2e%2e/files/x.txt",
                content=b"nope",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 400)
        messages = "\n".join(captured.output)
        self.assertIn("api write_file failed session_id=", messages)
        self.assertIn("invalid session id", messages)

    def test_api_logs_execute_runtime_error(self) -> None:
        with self.assertLogs("sandbox.api", level="WARNING") as captured:
            response = self.client.post(
                "/v1/sessions/api-log-exec-fail/execute",
                headers=AUTH,
                json={"code": "raise RuntimeError('nope')"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["exitCode"], 0)
        messages = "\n".join(captured.output)
        self.assertIn("api execute error session_id=api-log-exec-fail", messages)
        self.assertIn("exitCode=", messages)

    def test_api_logs_unexpected_exception(self) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sandbox.main.execute_op",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with self.assertLogs("sandbox.api", level="ERROR") as captured:
                with self.assertRaises(RuntimeError):
                    self.client.post(
                        "/v1/sessions/api-log-crash/execute",
                        headers=AUTH,
                        json={"code": "print(1)"},
                    )
        messages = "\n".join(captured.output)
        self.assertIn("api execute crashed session_id=api-log-crash", messages)
        self.assertIn("boom", messages)

    def test_mcp_requires_bearer(self) -> None:
        response = self.client.post("/mcp")
        self.assertEqual(response.status_code, 401)

    def test_mcp_accepts_bearer(self) -> None:
        response = self.client.post(
            "/mcp",
            headers={
                **AUTH,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        self.assertNotEqual(response.status_code, 401)
        self.assertLess(response.status_code, 500)


class SandboxMcpTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_write_and_read_via_mcp(self) -> None:
        from mcp import Client

        from sandbox.mcp_server import mcp

        async with Client(mcp) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            self.assertEqual(
                names,
                {"execute", "write_file", "read_file", "list_files", "delete_session"},
            )
            written = await client.call_tool(
                "write_file",
                {
                    "session_id": "mcp1",
                    "path": "uploads/input.txt",
                    "content": "hello",
                    "encoding": "utf-8",
                    "mime": "text/plain",
                },
            )
            self.assertFalse(written.is_error)
            executed = await client.call_tool(
                "execute",
                {
                    "session_id": "mcp1",
                    "code": "from pathlib import Path\n"
                    "text = Path('uploads/input.txt').read_text()\n"
                    "Path('out.txt').write_text(text.upper())\n"
                    "print(text)",
                },
            )
            self.assertFalse(executed.is_error)
            body = executed.structured_content
            self.assertIsNotNone(body)
            self.assertEqual(body["exitCode"], 0)
            self.assertEqual(body["stdout"].strip(), "hello")
            self.assertTrue(any(item["path"] == "out.txt" for item in body["files"]))
            read = await client.call_tool(
                "read_file",
                {"session_id": "mcp1", "path": "out.txt", "encoding": "utf-8"},
            )
            self.assertFalse(read.is_error)
            self.assertEqual(read.structured_content["content"], "HELLO")
            listed_files = await client.call_tool(
                "list_files",
                {"session_id": "mcp1"},
            )
            paths = {item["path"] for item in listed_files.structured_content["files"]}
            self.assertIn("out.txt", paths)
            deleted = await client.call_tool(
                "delete_session",
                {"session_id": "mcp1"},
            )
            self.assertEqual(deleted.structured_content["ok"], True)

    async def test_mcp_rejects_path_traversal(self) -> None:
        from mcp import Client

        from sandbox.mcp_server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool(
                "write_file",
                {
                    "session_id": "mcp-safe",
                    "path": "../secret.txt",
                    "content": "nope",
                },
            )
            self.assertTrue(result.is_error)

    async def test_mcp_logs_successful_call(self) -> None:
        from mcp import Client

        from sandbox.mcp_server import mcp

        with self.assertLogs("sandbox.mcp", level="INFO") as captured:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_files",
                    {"session_id": "mcp-log-ok"},
                )
        self.assertFalse(result.is_error)
        messages = "\n".join(captured.output)
        self.assertIn("mcp list_files start session_id=mcp-log-ok", messages)
        self.assertIn("mcp list_files ok session_id=mcp-log-ok", messages)

    async def test_mcp_logs_tool_error(self) -> None:
        from mcp import Client

        from sandbox.mcp_server import mcp

        with self.assertLogs("sandbox.mcp", level="WARNING") as captured:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "write_file",
                    {
                        "session_id": "mcp-log-fail",
                        "path": "../secret.txt",
                        "content": "nope",
                    },
                )
        self.assertTrue(result.is_error)
        messages = "\n".join(captured.output)
        self.assertIn("mcp write_file failed session_id=mcp-log-fail", messages)
        self.assertIn("invalid path", messages)

    async def test_mcp_logs_execute_runtime_error(self) -> None:
        from mcp import Client

        from sandbox.mcp_server import mcp

        with self.assertLogs("sandbox.mcp", level="WARNING") as captured:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "execute",
                    {
                        "session_id": "mcp-log-exec-fail",
                        "code": "raise RuntimeError('nope')",
                    },
                )
        self.assertFalse(result.is_error)
        body = result.structured_content
        self.assertIsNotNone(body)
        self.assertNotEqual(body["exitCode"], 0)
        messages = "\n".join(captured.output)
        self.assertIn("mcp execute error session_id=mcp-log-exec-fail", messages)
        self.assertIn("exitCode=", messages)

    async def test_mcp_logs_unexpected_exception(self) -> None:
        from unittest.mock import AsyncMock, patch

        from mcp import Client

        from sandbox.mcp_server import mcp

        with patch(
            "sandbox.mcp_server.execute_op",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with self.assertLogs("sandbox.mcp", level="ERROR") as captured:
                async with Client(mcp) as client:
                    result = await client.call_tool(
                        "execute",
                        {"session_id": "mcp-log-crash", "code": "print(1)"},
                    )
        self.assertTrue(result.is_error)
        messages = "\n".join(captured.output)
        self.assertIn("mcp execute crashed session_id=mcp-log-crash", messages)
        self.assertIn("boom", messages)


class SandboxReapTest(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_idle_default_is_15_minutes(self) -> None:
        import sandbox.sessions as sessions

        self.assertEqual(sessions.IDLE_WORKSPACE_SECONDS, 900)

    async def test_reap_deletes_idle_workspace(self) -> None:
        from sandbox.sessions import manager

        sid = "reap-idle"
        session = await manager.get(sid)
        marker = session.workspace / "keep.txt"
        marker.write_text("x")
        session.last_used = time.time() - 901
        await manager.reap()
        self.assertFalse(session.workspace.exists())
        self.assertFalse(await manager.is_alive(sid))

    async def test_reap_keeps_active_workspace(self) -> None:
        from sandbox.sessions import manager

        sid = "reap-active"
        session = await manager.get(sid)
        marker = session.workspace / "keep.txt"
        marker.write_text("x")
        try:
            await manager.reap()
            self.assertTrue(marker.is_file())
            self.assertFalse(await manager.is_alive(sid))
        finally:
            await manager.delete(sid, delete_workspace=True)

    async def test_reap_deletes_stale_orphan_workspace(self) -> None:
        from sandbox.paths import workspace_dir
        from sandbox.sessions import manager

        sid = "reap-orphan-old"
        path = workspace_dir(sid)
        (path / "stale.txt").write_text("x")
        old = time.time() - 901
        os.utime(path, (old, old))
        await manager.reap()
        self.assertFalse(path.exists())

    async def test_reap_keeps_fresh_orphan_workspace(self) -> None:
        from sandbox.paths import workspace_dir
        from sandbox.sessions import manager

        sid = "reap-orphan-fresh"
        path = workspace_dir(sid)
        (path / "fresh.txt").write_text("x")
        try:
            await manager.reap()
            self.assertTrue((path / "fresh.txt").is_file())
        finally:
            await manager.delete(sid, delete_workspace=True)

    async def test_reap_pops_idle_kernel_but_keeps_workspace_before_ttl(self) -> None:
        import sandbox.sessions as sessions
        from sandbox.sessions import manager

        original_kernel = sessions.IDLE_KERNEL_SECONDS
        original_workspace = sessions.IDLE_WORKSPACE_SECONDS
        sessions.IDLE_KERNEL_SECONDS = 10
        sessions.IDLE_WORKSPACE_SECONDS = 1000
        sid = "reap-kernel-only"
        try:
            session = await manager.get(sid)
            marker = session.workspace / "keep.txt"
            marker.write_text("x")
            session.last_used = time.time() - 20
            await manager.reap()
            self.assertTrue(marker.is_file())
            self.assertFalse(await manager.is_alive(sid))
        finally:
            sessions.IDLE_KERNEL_SECONDS = original_kernel
            sessions.IDLE_WORKSPACE_SECONDS = original_workspace
            await manager.delete(sid, delete_workspace=True)

    async def test_get_refreshes_workspace_mtime(self) -> None:
        from sandbox.sessions import manager

        sid = "reap-touch"
        session = await manager.get(sid)
        old = time.time() - 901
        os.utime(session.workspace, (old, old))
        try:
            await manager.get(sid)
            mtime = session.workspace.stat().st_mtime
            self.assertGreater(mtime, time.time() - 5)
        finally:
            await manager.delete(sid, delete_workspace=True)


if __name__ == "__main__":
    unittest.main()

