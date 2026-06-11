"""In-memory administrator authentication state."""

from __future__ import annotations

import secrets
import threading
import time


class AdminAuthService:
    def __init__(self, session_ttl: int = 86400, csrf_ttl: int = 3600):
        self.session_ttl = session_ttl
        self.csrf_ttl = csrf_ttl
        self._sessions: dict[str, float] = {}
        self._csrf_tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def _cleanup(self) -> None:
        now = time.time()
        with self._lock:
            self._sessions = {
                token: expiry
                for token, expiry in self._sessions.items()
                if expiry >= now
            }
            self._csrf_tokens = {
                token: expiry
                for token, expiry in self._csrf_tokens.items()
                if expiry >= now
            }

    def create_session(self) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            self._sessions[token] = time.time() + self.session_ttl
        return token

    def is_valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        self._cleanup()
        with self._lock:
            expiry = self._sessions.get(token)
        return expiry is not None and expiry > time.time()

    def create_csrf_token(self) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._csrf_tokens[token] = time.time() + self.csrf_ttl
        return token

    def verify_csrf_token(self, token: str | None) -> bool:
        if not token:
            return False
        self._cleanup()
        with self._lock:
            expiry = self._csrf_tokens.pop(token, None)
        return expiry is not None and expiry > time.time()
