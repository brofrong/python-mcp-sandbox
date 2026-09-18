from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SANDBOX_SECRET", "test-secret")
os.environ["SANDBOX_DATA"] = tempfile.mkdtemp(prefix="sandbox-test-")

from fastapi.testclient import TestClient

from sandbox.main import app
from sandbox.paths import resolve_relative, workspace_dir

AUTH = {"Authorization": "Bearer test-secret"}


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


if __name__ == "__main__":
    unittest.main()

