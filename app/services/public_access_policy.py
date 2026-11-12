"""Persistent global access policy for unauthenticated website visitors."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


DEFAULT_PUBLIC_ACCESS = {
    "body_view": True,
    "body_search": True,
    "comments_view": True,
    "comments_search": True,
    "deleted_records": False,
}


class PublicAccessPolicy:
    """Read and atomically update the public-facing access switches."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _normalize(payload: object, *, base: dict | None = None) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("权限设置必须是 JSON 对象")
        unknown = set(payload) - set(DEFAULT_PUBLIC_ACCESS)
        if unknown:
            raise ValueError("包含未知权限项")
        values = dict(base or DEFAULT_PUBLIC_ACCESS)
        for key in DEFAULT_PUBLIC_ACCESS:
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须为布尔值")
            values[key] = value
        return values

    def load(self) -> dict:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return dict(DEFAULT_PUBLIC_ACCESS)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return dict(DEFAULT_PUBLIC_ACCESS)
            try:
                return self._normalize(payload)
            except ValueError:
                return dict(DEFAULT_PUBLIC_ACCESS)

    def save(self, payload: object) -> dict:
        with self._lock:
            current = self.load()
            settings = self._normalize(payload, base=current)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
            return settings
