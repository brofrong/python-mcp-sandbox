from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sandbox.ops import SandboxOpError

T = TypeVar("T")


def configure_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def extras(**values: object) -> str:
    return "".join(f" {key}={value}" for key, value in values.items() if value is not None)


def log_execute_result(logger: logging.Logger, kind: str, session_id: str, result: object) -> None:
    if not isinstance(result, dict):
        logger.info("%s execute ok session_id=%s", kind, session_id)
        return
    exit_code = result.get("exitCode")
    timed_out = result.get("timedOut")
    if timed_out or exit_code not in (0, None):
        stderr = str(result.get("stderr", ""))
        if len(stderr) > 500:
            stderr = stderr[:500] + "…"
        logger.warning(
            "%s execute error session_id=%s exitCode=%s timedOut=%s stderr=%s",
            kind,
            session_id,
            exit_code,
            timed_out,
            stderr,
        )
        return
    logger.info("%s execute ok session_id=%s exitCode=%s", kind, session_id, exit_code)


async def logged_call(
    logger: logging.Logger,
    kind: str,
    name: str,
    session_id: str,
    op: Callable[[], Awaitable[T]],
    *,
    anticipated: tuple[type[BaseException], ...] = (),
    **extra: object,
) -> T:
    logger.info("%s %s start session_id=%s%s", kind, name, session_id, extras(**extra))
    try:
        result = await op()
    except SandboxOpError as error:
        logger.warning("%s %s failed session_id=%s: %s", kind, name, session_id, error.detail)
        raise
    except anticipated as error:
        logger.warning("%s %s failed session_id=%s: %s", kind, name, session_id, error)
        raise
    except Exception:
        logger.exception("%s %s crashed session_id=%s", kind, name, session_id)
        raise
    if name == "execute":
        log_execute_result(logger, kind, session_id, result)
    else:
        logger.info("%s %s ok session_id=%s", kind, name, session_id)
    return result
