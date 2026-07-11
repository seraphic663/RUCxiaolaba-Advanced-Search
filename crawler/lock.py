"""Cross-process lock protecting the SQLite writer."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from crawler.config import (
    DEFAULT_LOCK_TIMEOUT,
    LEGACY_LOCK_MAX_AGE_SECONDS,
    LOCK_HEARTBEAT_SECONDS,
    LOCK_LEASE_SECONDS,
)

_LOCK_VERSION = 2


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def _lock_payload(owner: dict) -> bytes:
    return json.dumps(owner, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _read_lock(lock_path: Path) -> dict:
    """Read either a v2 JSON lease or the legacy plain-PID marker."""
    try:
        text = lock_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError:
        text = ""
    try:
        stat = lock_path.stat()
        modified_at = stat.st_mtime
    except FileNotFoundError:
        return {"kind": "missing"}

    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict) and value.get("token"):
        return {
            "kind": "lease",
            "token": str(value.get("token") or ""),
            "hostname": str(value.get("hostname") or ""),
            "pid": _safe_int(value.get("pid")),
            "created_at": _safe_float(value.get("created_at")),
            "heartbeat_at": _safe_float(value.get("heartbeat_at")),
            "modified_at": modified_at,
            "raw": text,
        }
    try:
        legacy_pid = int(text)
    except (TypeError, ValueError):
        legacy_pid = 0
    return {
        "kind": "legacy" if legacy_pid > 0 else "corrupt",
        "pid": legacy_pid,
        "modified_at": modified_at,
        "raw": text,
    }


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _heartbeat_age(snapshot: dict, now: float) -> float:
    latest = _safe_float(snapshot.get("modified_at"))
    if snapshot.get("kind") == "lease":
        latest = max(latest, _safe_float(snapshot.get("heartbeat_at")))
    return max(0.0, now - latest)


def _same_lock(left: dict, right: dict) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    if left.get("kind") == "lease":
        return bool(left.get("token")) and left.get("token") == right.get("token")
    return left.get("raw") == right.get("raw")


def _expired(snapshot: dict, *, now: float, lease_seconds: float) -> bool:
    if snapshot.get("kind") == "missing":
        return True
    age = _heartbeat_age(snapshot, now)
    if (
        snapshot.get("kind") == "legacy"
        and _pid_alive(_safe_int(snapshot.get("pid")))
        and age < LEGACY_LOCK_MAX_AGE_SECONDS
    ):
        # A legacy owner in this PID namespace has no heartbeat.  Retain the
        # old liveness behaviour briefly, but do not let PID reuse block all
        # future crawls forever after a container replacement.
        return False
    return age >= lease_seconds


def _refresh_heartbeat(lock_path: Path, owner: dict) -> bool:
    """Refresh only the inode that still contains this owner's token."""
    try:
        descriptor = os.open(str(lock_path), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        current = os.read(descriptor, 65536).decode("ascii", errors="replace")
        try:
            current_value = json.loads(current)
        except ValueError:
            return False
        if not isinstance(current_value, dict) or current_value.get("token") != owner["token"]:
            return False
        owner["heartbeat_at"] = time.time()
        payload = _lock_payload(owner)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _release_owned_lock(lock_path: Path, token: str) -> bool:
    """Delete the marker only when it still belongs to this context."""
    try:
        descriptor = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        return False
    matches = False
    try:
        opened_stat = os.fstat(descriptor)
        text = os.read(descriptor, 65536).decode("ascii", errors="replace")
        try:
            value = json.loads(text)
        except ValueError:
            return False
        if not isinstance(value, dict) or value.get("token") != token:
            return False
        try:
            path_stat = lock_path.stat()
        except FileNotFoundError:
            return False
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            return False
        matches = True
    finally:
        os.close(descriptor)
    if not matches:
        return False
    # Windows does not allow unlinking an open file. Re-read after closing so
    # a replacement owner observed in the meantime is never removed.
    current = _read_lock(lock_path)
    if current.get("kind") != "lease" or current.get("token") != token:
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def database_write_lock(
    db_path: str | Path,
    timeout: int = DEFAULT_LOCK_TIMEOUT,
    *,
    lease_seconds: float = LOCK_LEASE_SECONDS,
    heartbeat_interval: float = LOCK_HEARTBEAT_SECONDS,
):
    lock_path = Path(str(db_path) + ".crawler.lock")
    deadline = time.time() + timeout
    lease_seconds = max(0.01, float(lease_seconds))
    heartbeat_interval = max(
        0.01,
        min(float(heartbeat_interval), lease_seconds / 3),
    )
    token = uuid.uuid4().hex
    hostname = socket.gethostname()
    now = time.time()
    owner = {
        "version": _LOCK_VERSION,
        "token": token,
        "hostname": hostname,
        "pid": os.getpid(),
        "created_at": now,
        "heartbeat_at": now,
    }
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    acquired = False
    wait_logged = False

    def remove_stale() -> bool:
        snapshot = _read_lock(lock_path)
        checked_at = time.time()
        if snapshot.get("kind") == "missing":
            return True
        if not _expired(
            snapshot,
            now=checked_at,
            lease_seconds=lease_seconds,
        ):
            return False
        # Confirm the identity and expiry immediately before unlinking so a
        # concurrent heartbeat or replacement owner wins the race.
        confirmed = _read_lock(lock_path)
        if not _same_lock(snapshot, confirmed) or not _expired(
            confirmed,
            now=time.time(),
            lease_seconds=lease_seconds,
        ):
            return False
        try:
            lock_path.unlink()
            print(
                f"[lock] removed stale lock kind={confirmed.get('kind')} "
                f"pid={confirmed.get('pid', 0)} "
                f"host={confirmed.get('hostname', '') or '?'} "
                f"age={int(_heartbeat_age(confirmed, time.time()))}s "
                f"path={lock_path}",
                flush=True,
            )
            return True
        except FileNotFoundError:
            return True

    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                _write_all(descriptor, _lock_payload(owner))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            if remove_stale():
                continue
            if not wait_logged:
                current = _read_lock(lock_path)
                print(
                    f"[lock] waiting kind={current.get('kind')} "
                    f"pid={current.get('pid', 0)} "
                    f"host={current.get('hostname', '') or '?'} "
                    f"age={int(_heartbeat_age(current, time.time()))}s "
                    f"path={lock_path}",
                    flush=True,
                )
                wait_logged = True
            if time.time() >= deadline:
                raise TimeoutError(f"crawler lock timeout: {lock_path}")
            remaining = max(0.01, deadline - time.time())
            time.sleep(min(2.0, lease_seconds / 3, remaining))

    def heartbeat() -> None:
        while not heartbeat_stop.wait(heartbeat_interval):
            if not _refresh_heartbeat(lock_path, owner):
                print(
                    f"[lock] heartbeat lost token={token[:8]} path={lock_path}",
                    flush=True,
                )
                return

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        daemon=True,
        name=f"crawler-lock-{token[:8]}",
    )
    heartbeat_thread.start()
    print(
        f"[lock] acquired token={token[:8]} pid={owner['pid']} "
        f"host={hostname} lease={lease_seconds:g}s path={lock_path}",
        flush=True,
    )
    try:
        yield
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=max(1.0, heartbeat_interval + 1.0))
        released = acquired and _release_owned_lock(lock_path, token)
        print(
            f"[lock] {'released' if released else 'release-skipped'} "
            f"token={token[:8]} path={lock_path}",
            flush=True,
        )
