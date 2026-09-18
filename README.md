# Python MCP Sandbox

A **ChatGPT-style Python code interpreter** you can run next to your own backend.

The model writes Python. This service runs it in a jail: persistent kernel, `/workspace` files, no `pip`. You get stdout, stderr, and the files the code created — same loop as Advanced Data Analysis / Code Interpreter in ChatGPT, but as a standalone Docker service.

It does **not** talk to OpenAI, OpenRouter, or your users. It only executes code. Your backend owns sessions, auth, and download URLs.

```
user  →  your app / LLM  →  this sandbox  →  Python kernel + /workspace
                              REST / MCP
```

## Why it exists

ChatGPT’s code interpreter is a long-lived Python process per chat:

- variables survive between turns (`x = 1` now, `print(x)` later)
- files live in a workspace the model can read and write
- the runtime cannot reach the public internet
- plots, spreadsheets, PDFs come back as artifacts

This repo is that runtime as an HTTP + MCP service. Point any agent at it (Cursor, Claude, a custom chat backend) and you get the same behavior without giving the model a shell on your machine.

## What you get

- **Persistent session** — one Python worker per `sessionId` (`userId_chatId` is a typical choice). `exec` runs in shared globals.
- **Virtual cwd `/workspace`** — uploads under `/workspace/uploads/`, outputs written into `/workspace`.
- **No runtime package install.** Packages are baked into the image flavor; there is no `pip` in the jail.
- **REST** for backends that want a plain HTTP client.
- **MCP Streamable HTTP** at `/mcp` for agents that speak MCP.
- **Harvest, then delete** — workspace is a cache. Copy file bytes to your object store, then `DELETE` the session (or let 15-minute idle TTL wipe it).

One container = one backend. Do not share an instance across untrusted services.

## Image flavors

Published to `ghcr.io/brofrong/python-mcp-sandbox`. Sandbox libraries are unpinned (`latest` on PyPI at build time). There is no `pip` inside the jail — pick the flavor that matches what the model is allowed to import.

| Tag | Sandbox libraries | Typical use |
| --- | --- | --- |
| `zero` | none (Python stdlib only) | smallest image, no data-science stack |
| `small` | openpyxl, python-docx, reportlab, python-pptx, pandas, pypandoc, numpy, matplotlib | default; also tagged `latest` |
| `gpt` | ChatGPT-style scientific stack (pandas, scipy, sklearn, torch **CPU**, jax, opencv, spacy, geo, audio, CAD, …) | amd64 only; much larger |

```bash
docker build -t python-mcp-sandbox:small --build-arg FLAVOR=small .
docker build -t python-mcp-sandbox:zero --build-arg FLAVOR=zero .
docker build -t python-mcp-sandbox:gpt --build-arg FLAVOR=gpt .
```

`gpt` needs extra RAM for PyTorch/JAX (compose: `SANDBOX_MEM_LIMIT=8g`, `SANDBOX_MEMORY_BYTES=2147483648`). Compose flavor: `SANDBOX_FLAVOR=gpt`.

## Quick start

```bash
docker build -t python-mcp-sandbox:small --build-arg FLAVOR=small .
docker run -d --name sandbox \
  -p 127.0.0.1:8090:8090 \
  -e SANDBOX_SECRET=change-me \
  -v sandbox-data:/data \
  python-mcp-sandbox:small
```

The process **will not start** without `SANDBOX_SECRET`.

```bash
# healthcheck is public
curl http://localhost:8090/health

# run Python in a session
curl -s -X POST http://localhost:8090/v1/sessions/demo/execute \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"code":"print(2 + 2)"}'
```

Locally, without Docker (`requirements.txt` = server + `small` libraries):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SANDBOX_SECRET=change-me
export SANDBOX_DATA=./data
PYTHONPATH=src uvicorn sandbox.main:app --host 127.0.0.1 --port 8090
```

Server-only (stdlib sandbox, like `zero`): `pip install -r requirements-server.txt`.

## Typical backend loop

1. Choose `sessionId` yourself. Never let the browser or the model pick it.
2. `PUT` user uploads into `uploads/...`.
3. `POST /execute` with the model’s Python.
4. For each item in `files`, `GET` the bytes and store them in **your** blob store / CDN.
5. Return stdout + your public URLs to the model. Never expose the sandbox URL or the Bearer secret.
6. `DELETE` the session when the chat is done (or let idle TTL reap it).

Idle TTL is 15 minutes from the last PUT or execute. GET / list do not extend it. If the kernel or workspace was reaped, `PUT` previously harvested files back, then execute again. RAM variables are not restored.

## HTTP API

All `/v1/*` routes require `Authorization: Bearer $SANDBOX_SECRET`. `GET /health` does not.

| Method | Path | Body / response |
| --- | --- | --- |
| `GET` | `/health` | `{ "ok": true }` |
| `PUT` | `/v1/sessions/{id}/files/{path}` | raw bytes → `{ path, size, mime }` |
| `GET` | `/v1/sessions/{id}/files/{path}` | bytes + `Content-Type` |
| `POST` | `/v1/sessions/{id}/execute` | `{ "code", "timeoutMs"? }` → `{ exitCode, stdout, stderr, timedOut, files }` |
| `GET` | `/v1/sessions/{id}` | `{ files, alive }` |
| `DELETE` | `/v1/sessions/{id}` | kill kernel + wipe workspace |

- `sessionId`: `[A-Za-z0-9._-]{1,200}`
- `path`: relative, no `..`, no absolute paths
- `files` after execute: only files created or changed by **this** run (`__pycache__` skipped)
- Code is not passed through a shell

Example — upload a CSV, plot it, download the PNG:

```bash
curl -X PUT http://localhost:8090/v1/sessions/chat1/files/uploads/data.csv \
  -H "Authorization: Bearer change-me" \
  --data-binary @data.csv

curl -s -X POST http://localhost:8090/v1/sessions/chat1/execute \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"code":"import pandas as pd\nimport matplotlib.pyplot as plt\ndf = pd.read_csv(\"uploads/data.csv\")\nprint(df.head())\ndf.plot()\nplt.savefig(\"/workspace/chart.png\")\n"}'

curl -O http://localhost:8090/v1/sessions/chat1/files/chart.png \
  -H "Authorization: Bearer change-me"
```

## MCP

Same process, same secret, same sessions:

```
http://localhost:8090/mcp
Authorization: Bearer $SANDBOX_SECRET
```

| Tool | Role |
| --- | --- |
| `execute` | run Python in a persistent kernel |
| `write_file` | put bytes into the workspace (`utf-8` or `base64`) |
| `read_file` | harvest bytes (`base64` by default) |
| `list_files` | workspace listing + whether the kernel is alive |
| `delete_session` | kill kernel and wipe disk |

`session_id` is an argument on every tool — it is the sandbox session, not the MCP protocol session. A backend that talks MCP should inject `session_id` itself and expose only `execute({ code })` to the model.

## Preinstalled libraries

There is no `pip` inside the jail. What user code can import depends on the image flavor.

### `zero`

CPython stdlib only.

### `small` (default / `latest`)

| Format / job | Package | Import |
| --- | --- | --- |
| Excel | openpyxl | `import openpyxl` |
| Word | python-docx | `import docx` |
| PDF | reportlab | `from reportlab.pdfgen import canvas` |
| PowerPoint | python-pptx | `from pptx import Presentation` |
| CSV / tables | pandas | `import pandas as pd` |
| Markdown / text | pypandoc (+ system pandoc) | `import pypandoc` |
| Plots / arrays | matplotlib, numpy | `import matplotlib.pyplot as plt`, `import numpy as np` |

### `gpt`

ChatGPT Code Interpreter–style stack from `requirements-gpt.txt` (latest compatible versions at build). Highlights: numpy/pandas/scipy, sklearn, matplotlib/seaborn/plotly, pillow/opencv, torch CPU + jax, keras, spacy/nltk, geopandas, librosa, PyMuPDF/pdfplumber, cadquery, rdkit, and the usual office libraries.

Omitted from the ChatGPT freeze: CUDA wheels, Jupyter, GUI automation, Playwright, OpenAI-internal packages, pytest/APM. Torch is the official CPU build.

After changing `requirements-*.txt`, rebuild the matching flavor.

## Limits

| Limit | Default |
| --- | --- |
| Execute timeout | 30s (max 120s) |
| Code size | 200_000 characters |
| Captured stdout / stderr | ~1 MB |
| Single file | 20 MB |
| Workspace | ~200 MB |
| Concurrent kernels | 32 |
| Worker RSS | 512 MB |
| Worker CPU | 30s |
| Idle kernel | 15 min |
| Idle workspace | 15 min |

Tune with env vars:

| Variable | Default |
| --- | --- |
| `SANDBOX_SECRET` | required |
| `SANDBOX_DATA` | `/data` |
| `SANDBOX_MAX_KERNELS` | `32` |
| `SANDBOX_IDLE_KERNEL_SECONDS` | `900` |
| `SANDBOX_IDLE_WORKSPACE_SECONDS` | `900` |
| `SANDBOX_MAX_WORKSPACE_BYTES` | `209715200` |
| `SANDBOX_MEMORY_BYTES` | `536870912` |
| `SANDBOX_CPU_SECONDS` | `30` |

## Security

This is a **code jail for a trusted backend**, not a multi-tenant public interpreter.

- Keep the port on a private network. Do not put it on a public reverse proxy.
- Only the backend should hold `SANDBOX_SECRET`.
- Still assume user code can burn CPU and disk up to the quotas, and can make outbound network requests.
- Do not mount `docker.sock`. Do not execute code through a shell.
- One container per backend. Do not share it across untrusted apps.

## Tests

```bash
PYTHONPATH=src python -m unittest tests.test_api
```

## Prompt for an integrating backend

Copy this into another chat if you want an LLM to write the HTTP client (your backend calls the sandbox; the model never does).

````markdown
You are integrating a backend with an existing Docker Python sandbox. The sandbox is a standalone HTTP service. Do not put any LLM API key in the sandbox. The model must only call a tool on OUR backend; OUR backend calls the sandbox.

## Deploy

- Image: `docker build -t python-mcp-sandbox:small --build-arg FLAVOR=small .` (or pull `ghcr.io/brofrong/python-mcp-sandbox:small`; tags `zero` / `small` / `gpt`, `latest` = `small`)
- Run one container PER backend:

```bash
docker run -d --name sandbox \
  -p 127.0.0.1:8090:8090 \
  -e SANDBOX_SECRET=<long random secret> \
  -v sandbox-data:/data \
  python-mcp-sandbox:small
```

- Local: `SANDBOX_URL=http://localhost:8090`. In compose/k8s: `SANDBOX_URL=http://sandbox:8090`.
- Process refuses to start without `SANDBOX_SECRET`.
- `GET /health` has no auth. Everything under `/v1` and `/mcp` requires `Authorization: Bearer $SANDBOX_SECRET`.

## Session model

- Opaque `sessionId` chosen by the backend, regex `^[A-Za-z0-9._-]{1,200}$`. Typical: `{userId}_{chatId}`. The client must NEVER pick sessionId.
- One long-lived Python worker per sessionId: `exec` in shared globals, so variables and files persist between turns (ChatGPT-style).
- Python code should use `/workspace` (session cwd). Uploads go to `/workspace/uploads/`. Write outputs to `/workspace`.
- Workspace is a CACHE. After execute, GET file bytes, copy them to OUR object store, and give the user THAT url. Never give end users the sandbox URL or the Bearer secret. After harvest, DELETE the session (or rely on idle TTL: 15 minutes after the last PUT/execute; GET does not refresh it).
- If the kernel/workspace was reaped, rehydrate: PUT previous artifact bytes back, then execute again. RAM variables are not restored.

## HTTP contract

Base: `$SANDBOX_URL` (no trailing slash).

1. `PUT /v1/sessions/{sessionId}/files/{path}` — raw body = file bytes. Relative path only. 200 `{ path, size, mime }`. 413 if file > 20MB or workspace > ~200MB.
2. `POST /v1/sessions/{sessionId}/execute` — JSON `{ "code", "timeoutMs"? }` (default 30000, max 120000). 200 `{ exitCode, stdout, stderr, timedOut, files: [{ path, size, mime }] }`. `files` = created/changed by THIS run. Paths are relative (`report.xlsx`, not `/workspace/report.xlsx`).
3. `GET /v1/sessions/{sessionId}/files/{path}` — raw bytes.
4. `GET /v1/sessions/{sessionId}` → `{ files, alive }`.
5. `DELETE /v1/sessions/{sessionId}` → kill kernel + wipe workspace.

MCP is the same service at `$SANDBOX_URL/mcp`. Tools: `execute`, `write_file`, `read_file`, `list_files`, `delete_session`. Each tool takes `session_id`. This backend injects `session_id`; the model only sees `execute` with `{ "code": string }`.

Files enter the sandbox via PUT / `write_file` from the backend. There is no `pip`; extra packages are not installable at runtime.

## Tool the model sees

`execute` with `{ "code": string }`:

- cwd is `/workspace`; uploads are `/workspace/uploads/`
- write results into `/workspace`; they come back as download URLs from OUR backend
- installed packages depend on the image flavor (`zero` = stdlib; `small` = openpyxl, python-docx, reportlab, python-pptx, pandas, pypandoc, matplotlib, numpy; `gpt` = ChatGPT-style scientific stack including scipy/sklearn/torch CPU)
- do not import anything else; there is no pip

Backend loop: rehydrate files if needed → POST execute → GET each new file into OUR blob store → return stdout/stderr/exitCode plus `{ name, url, path, mime, size }` → persist those URLs on the chat message.

## Do not

- Let the model or the mobile app talk to the sandbox.
- Treat sandbox `files[].path` as a user-facing URL.
- Share one container across backends.
- Execute shell, install packages at runtime, or fetch URLs from Python.
````
