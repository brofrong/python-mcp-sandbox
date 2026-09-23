"""PID 1. Reaps zombies. SANDBOX_SECRET is not in this process's initial environment."""

from __future__ import annotations

import os
import signal
import sys

_MAX_SECRET_BYTES = 1 << 20


def main() -> None:
    if len(sys.argv) == 1:
        _reexec_without_secret()
    secret = _read_fd(int(sys.argv[1]))
    _supervise(secret)


def _reexec_without_secret() -> None:
    secret = os.environ.get("SANDBOX_SECRET", "")
    if secret == "":
        print("SANDBOX_SECRET is required", file=sys.stderr)
        raise SystemExit(1)
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    os.write(write_fd, secret.encode("utf-8"))
    os.close(write_fd)
    env = {key: value for key, value in os.environ.items() if key != "SANDBOX_SECRET"}
    os.execve(sys.executable, [sys.executable, __file__, str(read_fd)], env)
    raise SystemExit(127)


def _read_fd(fd: int) -> str:
    try:
        return os.read(fd, _MAX_SECRET_BYTES).decode("utf-8")
    finally:
        os.close(fd)


def _supervise(secret: str) -> None:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    os.write(write_fd, secret.encode("utf-8"))
    os.close(write_fd)
    del secret

    pid = os.fork()
    if pid == 0:
        env = os.environ.copy()
        env.pop("SANDBOX_SECRET", None)
        env["SANDBOX_SECRET_FD"] = str(read_fd)
        try:
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "sandbox.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8090",
                ],
                env,
            )
        except OSError as error:
            print(error, file=sys.stderr)
            os._exit(127)

    os.close(read_fd)

    def forward(signum: int, _frame: object) -> None:
        try:
            os.kill(pid, signum)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    status = 0
    while True:
        try:
            waited, status = os.waitpid(-1, 0)
        except ChildProcessError:
            raise SystemExit(0) from None
        except InterruptedError:
            continue
        if waited == pid:
            break
    if os.WIFEXITED(status):
        raise SystemExit(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        raise SystemExit(128 + os.WTERMSIG(status))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
