"""Project-scoped, cross-process advisory writer lock."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class WriterLockTimeout(TimeoutError):
    pass


@contextmanager
def project_write_lock(project_root: Path, timeout: float = 10.0):
    if timeout < 0:
        raise ValueError("writer lock timeout must be nonnegative")
    lock_path = Path(project_root) / "data" / ".uniflow-writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise WriterLockTimeout(f"Timed out waiting for UniFlow writer lock: {lock_path}")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
