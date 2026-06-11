"""Cross-process lock protecting the SQLite writer."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from crawler.config import DEFAULT_LOCK_TIMEOUT, STALE_LOCK_SECONDS


@contextmanager
def database_write_lock(
    db_path: str | Path,
    timeout: int = DEFAULT_LOCK_TIMEOUT,
):
    lock_path = Path(str(db_path) + ".crawler.lock")
    deadline = time.time() + timeout
    descriptor = None

    def remove_stale() -> bool:
        try:
            owner_pid = int(lock_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            owner_pid = 0
        owner_alive = False
        if owner_pid > 0:
            try:
                os.kill(owner_pid, 0)
                owner_alive = True
            except PermissionError:
                owner_alive = True
            except OSError:
                pass
        try:
            age = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return True
        if owner_alive or (owner_pid <= 0 and age < STALE_LOCK_SECONDS):
            return False
        try:
            lock_path.unlink()
            print(
                f"[lock] removed stale lock pid={owner_pid} age={int(age)}s "
                f"path={lock_path}",
                flush=True,
            )
            return True
        except FileNotFoundError:
            return True

    while True:
        try:
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            os.write(
                descriptor,
                str(os.getpid()).encode("ascii", errors="ignore"),
            )
            break
        except FileExistsError:
            if remove_stale():
                continue
            if time.time() >= deadline:
                raise TimeoutError(f"crawler lock timeout: {lock_path}")
            time.sleep(2)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
