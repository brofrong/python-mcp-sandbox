"""Execute real library code against a running Docker sandbox.

The small image must already be up. 8090 is often taken by another container,
so the local image is expected on 8091:

    docker build -t python-mcp-sandbox:small --build-arg FLAVOR=small .
    docker run -d --name sandbox-e2e --user 1000:1000 --read-only \\
      --cap-drop ALL --security-opt no-new-privileges:true \\
      --pids-limit 256 --memory 4g --cpus 2 \\
      --ulimit core=0 --ulimit nofile=1024 \\
      --tmpfs /tmp:size=64M,mode=1777,nosuid,nodev,noexec \\
      --tmpfs /home/sandbox:size=16M,uid=1000,gid=1000,mode=0755,nosuid,nodev \\
      -p 127.0.0.1:8091:8090 -e SANDBOX_SECRET=e2e-secret \\
      -v sandbox-e2e-data:/data python-mcp-sandbox:small

    SANDBOX_URL=http://127.0.0.1:8091 SANDBOX_SECRET=e2e-secret \\
        PYTHONPATH=src python -m unittest tests.test_e2e_libraries -v
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request

BASE_URL = os.environ.get("SANDBOX_URL", "http://127.0.0.1:8091").rstrip("/")
SECRET = os.environ.get("SANDBOX_SECRET", "")


def _request(method: str, path: str, payload: dict[str, object] | None, timeout: float) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {SECRET}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {path} -> {error.code} {detail}") from error
    if len(raw) == 0:
        return {}
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise AssertionError(f"expected object from {path}")
    return body


class LibraryE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if SECRET == "":
            raise unittest.SkipTest("SANDBOX_SECRET is required")
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=5) as response:
                health = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise unittest.SkipTest(f"sandbox is not reachable at {BASE_URL}: {error}") from error
        if health.get("ok") is not True:
            raise AssertionError(f"unhealthy sandbox: {health}")

    def tearDown(self) -> None:
        session_id = getattr(self, "_session_id", None)
        if not isinstance(session_id, str):
            return
        try:
            _request("DELETE", f"/v1/sessions/{session_id}", None, 10)
        except (AssertionError, OSError):
            pass

    def execute(self, name: str, code: str, *, timeout_ms: int = 60_000) -> dict[str, object]:
        self._session_id = f"e2e-{name}"
        body = _request(
            "POST",
            f"/v1/sessions/{self._session_id}/execute",
            {"code": code, "timeoutMs": timeout_ms},
            timeout_ms / 1000 + 20,
        )
        self.assertFalse(body["timedOut"], body.get("stderr"))
        self.assertEqual(body["exitCode"], 0, body.get("stderr"))
        return body

    def assert_stdout(self, name: str, code: str, expected: str, *, timeout_ms: int = 60_000) -> dict[str, object]:
        body = self.execute(name, code, timeout_ms=timeout_ms)
        self.assertEqual(str(body["stdout"]).strip(), expected, body.get("stderr"))
        return body

    def assert_file(self, body: dict[str, object], path: str) -> None:
        files = body["files"]
        self.assertIsInstance(files, list)
        self.assertTrue(any(isinstance(item, dict) and item.get("path") == path for item in files), files)

    def test_stdlib_compute_and_persist_values(self) -> None:
        self._session_id = "e2e-stdlib"
        first = self.execute(
            "stdlib",
            "import json, math\n"
            "value = 21\n"
            "print(json.dumps({'n': value * 2, 'root': int(math.sqrt(81))}))\n",
        )
        self.assertEqual(str(first["stdout"]).strip(), '{"n": 42, "root": 9}')
        second = _request(
            "POST",
            "/v1/sessions/e2e-stdlib/execute",
            {"code": "print(value + 1)", "timeoutMs": 30000},
            40,
        )
        self.assertEqual(second["exitCode"], 0, second.get("stderr"))
        self.assertEqual(str(second["stdout"]).strip(), "22")

    def test_stdlib_files_csv_zip_sqlite(self) -> None:
        body = self.assert_stdout(
            "files",
            "import csv, sqlite3, zipfile\n"
            "from pathlib import Path\n"
            "Path('/workspace/note.txt').write_text('note')\n"
            "print(Path('/workspace/note.txt').read_text())\n"
            "with Path('/workspace/rows.csv').open('w', newline='') as handle:\n"
            "    csv.writer(handle).writerow(['a', '3'])\n"
            "with Path('/workspace/rows.csv').open() as handle:\n"
            "    print(next(csv.reader(handle))[1])\n"
            "with zipfile.ZipFile('/workspace/bundle.zip', 'w') as archive:\n"
            "    archive.write('/workspace/note.txt', 'note.txt')\n"
            "with zipfile.ZipFile('/workspace/bundle.zip') as archive:\n"
            "    print(archive.read('note.txt').decode())\n"
            "db = sqlite3.connect('t.db')\n"
            "db.execute('create table n(v integer)')\n"
            "db.execute('insert into n values (7)')\n"
            "db.commit()\n"
            "print(db.execute('select v from n').fetchone()[0])\n",
            "note\n3\nnote\n7",
        )
        self.assert_file(body, "note.txt")
        self.assert_file(body, "bundle.zip")
        self.assert_file(body, "t.db")

    def test_numpy(self) -> None:
        self.assert_stdout(
            "numpy",
            "import numpy as np\n"
            "values = np.arange(6).reshape(2, 3)\n"
            "print(int(values.sum()), int(np.dot(values, values.T)[0, 1]))\n",
            "15 14",
        )

    def test_pandas(self) -> None:
        self.assert_stdout(
            "pandas",
            "import pandas as pd\n"
            "frame = pd.DataFrame({'n': [1, 2, 3], 'label': ['a', 'b', 'c']})\n"
            "print(int(frame['n'].sum()), frame['label'].iloc[1])\n",
            "6 b",
        )

    def test_pillow(self) -> None:
        body = self.assert_stdout(
            "pillow",
            "from PIL import Image\n"
            "image = Image.new('RGB', (8, 4), (255, 0, 0))\n"
            "image.save('/workspace/pixel.png')\n"
            "loaded = Image.open('/workspace/pixel.png')\n"
            "print(loaded.size, loaded.mode, loaded.getpixel((1, 1)))\n",
            "(8, 4) RGB (255, 0, 0)",
        )
        self.assert_file(body, "pixel.png")

    def test_matplotlib(self) -> None:
        body = self.assert_stdout(
            "matplotlib",
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 4, 9])\n"
            "plt.savefig('/workspace/chart.png')\n"
            "print(open('/workspace/chart.png', 'rb').read(8).hex())\n",
            "89504e470d0a1a0a",
            timeout_ms=90_000,
        )
        self.assert_file(body, "chart.png")

    def test_openpyxl(self) -> None:
        body = self.assert_stdout(
            "openpyxl",
            "from openpyxl import Workbook, load_workbook\n"
            "book = Workbook()\n"
            "book.active['A1'] = 21\n"
            "book.save('/workspace/book.xlsx')\n"
            "loaded = load_workbook('/workspace/book.xlsx')\n"
            "print(loaded.active['A1'].value * 2)\n",
            "42",
        )
        self.assert_file(body, "book.xlsx")

    def test_python_docx(self) -> None:
        body = self.assert_stdout(
            "docx",
            "from docx import Document\n"
            "document = Document()\n"
            "document.add_paragraph('hello-docx')\n"
            "document.save('/workspace/note.docx')\n"
            "loaded = Document('/workspace/note.docx')\n"
            "print(loaded.paragraphs[0].text)\n",
            "hello-docx",
        )
        self.assert_file(body, "note.docx")

    def test_reportlab(self) -> None:
        body = self.assert_stdout(
            "reportlab",
            "from reportlab.pdfgen import canvas\n"
            "pdf = canvas.Canvas('/workspace/page.pdf')\n"
            "pdf.drawString(72, 720, 'hello-pdf')\n"
            "pdf.save()\n"
            "print(open('/workspace/page.pdf', 'rb').read(5).decode())\n",
            "%PDF-",
        )
        self.assert_file(body, "page.pdf")

    def test_python_pptx(self) -> None:
        body = self.assert_stdout(
            "pptx",
            "from pptx import Presentation\n"
            "deck = Presentation()\n"
            "deck.slides.add_slide(deck.slide_layouts[6])\n"
            "deck.save('/workspace/deck.pptx')\n"
            "loaded = Presentation('/workspace/deck.pptx')\n"
            "print(len(loaded.slides))\n",
            "1",
        )
        self.assert_file(body, "deck.pptx")

    def test_pymupdf(self) -> None:
        # MuPDF opens paths in C. `/workspace` exists only for Python file APIs;
        # the session cwd is the real workspace.
        body = self.assert_stdout(
            "pymupdf",
            "import pymupdf\n"
            "document = pymupdf.open()\n"
            "page = document.new_page()\n"
            "page.insert_text((72, 72), 'hello-fitz')\n"
            "document.save('fitz.pdf')\n"
            "document.close()\n"
            "opened = pymupdf.open('fitz.pdf')\n"
            "print(opened[0].get_text().strip())\n",
            "hello-fitz",
            timeout_ms=90_000,
        )
        self.assert_file(body, "fitz.pdf")

    def test_pypandoc(self) -> None:
        self.assert_stdout(
            "pypandoc",
            "import pypandoc\n"
            "print(pypandoc.convert_text('Hello **pandoc**', 'plain', format='md').strip())\n",
            "Hello pandoc",
            timeout_ms=90_000,
        )

    def test_aspose_slides(self) -> None:
        # The .NET host writes through libc, so the path is relative to the session cwd.
        body = self.assert_stdout(
            "aspose",
            "import aspose.slides as slides\n"
            "with slides.Presentation() as presentation:\n"
            "    shape = presentation.slides[0].shapes.add_auto_shape(\n"
            "        slides.ShapeType.RECTANGLE, 40, 40, 200, 80)\n"
            "    shape.text_frame.text = 'aspose-ok'\n"
            "    presentation.save('aspose.pptx', slides.export.SaveFormat.PPTX)\n"
            "    print(len(presentation.slides))\n",
            "1",
            timeout_ms=120_000,
        )
        self.assert_file(body, "aspose.pptx")


if __name__ == "__main__":
    unittest.main()
