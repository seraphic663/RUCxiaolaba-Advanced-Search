"""Server-side AI configuration, separate from crawler credentials and posts."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from app.domain.ai_search import AiSearchError, AiSearchSettings, validate_api_url


class AiSearchConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            if not self.path.exists():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (OSError, ValueError) as exc:
            raise AiSearchError("config_unavailable", "AI 配置文件无法读取，请检查本地配置", 503) from exc

    @staticmethod
    def _settings(data: dict) -> AiSearchSettings:
        def setting(key, default="", env=""):
            value = data.get(key, os.environ.get(env, default) if env else default)
            if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
                raise AiSearchError("invalid_config", "接口配置必须是有效的单行文本")
            return value.strip()

        model = setting("model", "deepseek-flash", "AI_SEARCH_MODEL")
        if not model or len(model) > 160:
            raise AiSearchError("invalid_model", "请填写有效的模型名称")
        return AiSearchSettings(
            base_url=validate_api_url(setting("base_url", "https://api.deepseek.com", "AI_SEARCH_BASE_URL")),
            model=model,
            api_key=setting("api_key", os.environ.get("DEEPSEEK_API_KEY", ""), "AI_SEARCH_API_KEY"),
            search_url=validate_api_url(setting("search_url", "", "AI_SEARCH_RETRIEVAL_URL"), optional=True),
            search_api_key=setting("search_api_key", "", "AI_SEARCH_RETRIEVAL_KEY"),
        )

    def load(self) -> AiSearchSettings:
        with self._lock:
            return self._settings(self._read())

    def save(self, payload: dict) -> dict:
        with self._lock:
            data = self._read()
            for key in ("base_url", "model", "search_url"):
                if key in payload:
                    data[key] = payload[key]
            for key in ("api_key", "search_api_key"):
                if payload.get("clear_" + key) is True:
                    data[key] = ""
                elif payload.get(key):
                    data[key] = payload[key]
            settings = self._settings(data)
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    os.chmod(temporary, 0o600)
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except OSError as exc:
                raise AiSearchError("config_unavailable", "AI 配置保存失败，请检查配置目录权限", 503) from exc
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            return settings.public()
