from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_WORKSPACE_BYTES = int(os.environ.get("SANDBOX_MAX_WORKSPACE_BYTES", str(200 * 1024 * 1024)))
SKIP_DIR_NAMES = {"__pycache__", ".matplotlib", ".cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

MIME_BY_SUFFIX = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".html": "text/html",
    ".py": "text/x-python",
}


def data_root() -> Path:
    raw = os.environ.get("SANDBOX_DATA", "/data")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_dir(session_id: str, *, create: bool = True) -> Path:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise ValueError("invalid session id")
    path = data_root() / "workspaces" / session_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_relative(workspace: Path, relative: str) -> Path:
    raw = relative.strip().replace("\\", "/")
    if raw.startswith("/"):
        raw = raw.lstrip("/")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() == "":
        raise ValueError("invalid path")
    full = (workspace / candidate).resolve()
    try:
        full.relative_to(workspace.resolve())
    except ValueError as error:
        raise ValueError("invalid path") from error
    return full


def mime_for(path: Path, fallback: str = "application/octet-stream") -> str:
    suffix = path.suffix.lower()
    mapped = MIME_BY_SUFFIX.get(suffix)
    if mapped is not None:
        return mapped
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed if guessed is not None and len(guessed) > 0 else fallback


def should_skip(path: Path, workspace: Path) -> bool:
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return True
    if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in relative.parts[:-1]):
        return True
    if path.name.startswith("."):
        return True
    return path.suffix.lower() in SKIP_SUFFIXES


def list_files(workspace: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    if not workspace.exists():
        return files
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or should_skip(path, workspace):
            continue
        relative = path.relative_to(workspace).as_posix()
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "mime": mime_for(path),
            }
        )
    return files


def snapshot(workspace: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    if not workspace.exists():
        return result
    for path in workspace.rglob("*"):
        if not path.is_file() or should_skip(path, workspace):
            continue
        stat = path.stat()
        relative = path.relative_to(workspace).as_posix()
        result[relative] = (stat.st_size, stat.st_mtime_ns)
    return result


def changed_files(
    workspace: Path,
    before: dict[str, tuple[int, int]],
) -> list[dict[str, object]]:
    after = snapshot(workspace)
    files: list[dict[str, object]] = []
    for relative, stamp in after.items():
        if before.get(relative) == stamp:
            continue
        if stamp[0] > MAX_FILE_BYTES:
            continue
        path = workspace / relative
        files.append(
            {
                "path": relative,
                "size": stamp[0],
                "mime": mime_for(path),
            }
        )
    return files


def workspace_size(workspace: Path) -> int:
    if not workspace.exists():
        return 0
    total = 0
    for path in workspace.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total
