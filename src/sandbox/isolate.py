from __future__ import annotations

import os
from pathlib import Path

_SAFE_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LANGUAGE",
    "TZ",
    "LD_LIBRARY_PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "PYTHONHOME",
)


def worker_env(*, workspace: str, pythonpath: str, result_fd: int) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SAFE_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value
    tmp = Path(workspace) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": workspace,
            "TMPDIR": str(tmp),
            "SANDBOX_WORKSPACE": workspace,
            "SANDBOX_RESULT_FD": str(result_fd),
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": pythonpath,
        }
    )
    return env


def isolate_self(_workspace: str) -> None:
    """Drop credentials from the kernel process. No rlimits / Landlock / nproc clamp."""
    os.environ.pop("SANDBOX_SECRET", None)
