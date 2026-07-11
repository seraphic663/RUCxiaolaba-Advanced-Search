from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from crawler.lock import database_write_lock


def lock_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".crawler.lock")


def lease(token: str, *, heartbeat_at: float, hostname: str = "other-host") -> dict:
    return {
        "version": 2,
        "token": token,
        "hostname": hostname,
        "pid": 99999999,
        "created_at": heartbeat_at,
        "heartbeat_at": heartbeat_at,
    }


def test_lock_writes_json_and_renews_heartbeat(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    with database_write_lock(
        db_path,
        timeout=1,
        lease_seconds=0.3,
        heartbeat_interval=0.03,
    ):
        first = json.loads(marker.read_text(encoding="ascii"))
        assert first["hostname"] == socket.gethostname()
        assert first["pid"] == os.getpid()
        assert first["token"]
        time.sleep(0.08)
        second = json.loads(marker.read_text(encoding="ascii"))
        assert second["token"] == first["token"]
        assert second["heartbeat_at"] > first["heartbeat_at"]
    assert not marker.exists()


def test_fresh_foreign_lease_is_not_stolen(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    marker.write_text(
        json.dumps(lease("foreign", heartbeat_at=time.time())),
        encoding="ascii",
    )
    with pytest.raises(TimeoutError):
        with database_write_lock(
            db_path,
            timeout=0.06,
            lease_seconds=0.3,
            heartbeat_interval=0.03,
        ):
            pass
    assert json.loads(marker.read_text(encoding="ascii"))["token"] == "foreign"


def test_expired_foreign_lease_is_replaced(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    marker.write_text(
        json.dumps(lease("expired", heartbeat_at=time.time() - 10)),
        encoding="ascii",
    )
    old = time.time() - 10
    os.utime(marker, (old, old))
    with database_write_lock(
        db_path,
        timeout=1,
        lease_seconds=0.1,
        heartbeat_interval=0.02,
    ):
        assert json.loads(marker.read_text(encoding="ascii"))["token"] != "expired"
    assert not marker.exists()


def test_old_owner_does_not_delete_replacement_token(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    replacement = lease("replacement", heartbeat_at=time.time())
    with database_write_lock(
        db_path,
        timeout=1,
        lease_seconds=0.3,
        heartbeat_interval=0.03,
    ):
        marker.write_text(json.dumps(replacement), encoding="ascii")
    assert json.loads(marker.read_text(encoding="ascii"))["token"] == "replacement"


def test_expired_legacy_dead_pid_lock_is_compatible(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    marker.write_text("99999999", encoding="ascii")
    old = time.time() - 10
    os.utime(marker, (old, old))
    with database_write_lock(
        db_path,
        timeout=1,
        lease_seconds=0.1,
        heartbeat_interval=0.02,
    ):
        assert json.loads(marker.read_text(encoding="ascii"))["version"] == 2
    assert not marker.exists()


def test_live_legacy_pid_lock_is_preserved(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    marker.write_text(str(os.getpid()), encoding="ascii")
    old = time.time() - 10
    os.utime(marker, (old, old))
    with pytest.raises(TimeoutError):
        with database_write_lock(
            db_path,
            timeout=0.06,
            lease_seconds=0.1,
            heartbeat_interval=0.02,
        ):
            pass
    assert marker.read_text(encoding="ascii") == str(os.getpid())


def test_very_old_legacy_lock_recovers_even_when_pid_is_reused(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    marker.write_text(str(os.getpid()), encoding="ascii")
    old = time.time() - 360
    os.utime(marker, (old, old))
    with database_write_lock(
        db_path,
        timeout=1,
        lease_seconds=0.1,
        heartbeat_interval=0.02,
    ):
        assert json.loads(marker.read_text(encoding="ascii"))["version"] == 2
    assert not marker.exists()


def test_owned_lock_is_released_after_exception(tmp_path):
    db_path = tmp_path / "posts.db"
    marker = lock_path(db_path)
    with pytest.raises(RuntimeError, match="boom"):
        with database_write_lock(
            db_path,
            timeout=1,
            lease_seconds=0.3,
            heartbeat_interval=0.03,
        ):
            raise RuntimeError("boom")
    assert not marker.exists()
