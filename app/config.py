"""Application configuration loaded once at process startup."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_TEMPLATES_DIR = PROJECT_ROOT / "templates"
DEFAULT_BIGRAM_DB = DEFAULT_DATA_DIR / "bigram_index.db"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _database_info(path: Path) -> tuple[str, float] | None:
    if not path.exists():
        return None
    latest = ""
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "select max(nullif(create_time, '')) from posts"
            ).fetchone()
            latest = str(row[0] or "")
    except sqlite3.Error:
        pass
    return latest, path.stat().st_mtime


def choose_posts_db(explicit_path: str | Path | None = None) -> Path:
    """Resolve the posts database while preserving the historical precedence."""
    if explicit_path:
        return Path(explicit_path)
    env_path = os.environ.get("POSTS_DB_PATH") or os.environ.get("SQLITE_DB")
    if env_path:
        return Path(env_path)
    candidates = [DEFAULT_DATA_DIR / "posts.db"]
    available = [
        (path, info)
        for path in candidates
        if (info := _database_info(path)) is not None
    ]
    if not available:
        return candidates[0]
    return max(available, key=lambda item: item[1])[0]


def choose_bigram_db(
    explicit_path: str | Path | None = None,
) -> Path | None:
    """Use an explicit/env sidecar, otherwise auto-detect the local default."""
    if explicit_path is not None:
        return Path(explicit_path) if str(explicit_path).strip() else None
    env_path = os.environ.get("BIGRAM_DB_PATH") or os.environ.get("BIGRAM_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_BIGRAM_DB if DEFAULT_BIGRAM_DB.exists() else None


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    templates_dir: Path
    posts_db: Path
    bigram_db: Path | None
    ai_db: Path
    admin_password_file: Path
    ai_key_file: Path
    host: str
    port: int
    ai_enabled_setting: str
    ai_model: str
    ai_fallback_model: str
    ai_moderation_model: str
    ai_base_url: str
    ai_max_concurrent: int
    ai_prompt_char_limit: int
    ai_context_post_limit: int
    ai_max_output_tokens: int
    ai_request_timeout: int
    ai_network_retries: int
    ai_moderation_timeout: int
    ai_moderation_retries: int

    @classmethod
    def from_env(
        cls,
        *,
        posts_db: str | Path | None = None,
        bigram_db: str | Path | None = None,
    ) -> "AppConfig":
        ai_db = os.environ.get("AI_DB_PATH", str(DEFAULT_DATA_DIR / "ai.db"))
        return cls(
            project_root=PROJECT_ROOT,
            data_dir=DEFAULT_DATA_DIR,
            templates_dir=DEFAULT_TEMPLATES_DIR,
            posts_db=choose_posts_db(posts_db),
            bigram_db=choose_bigram_db(bigram_db),
            ai_db=Path(ai_db),
            admin_password_file=DEFAULT_DATA_DIR / "admin_password.txt",
            ai_key_file=DEFAULT_DATA_DIR / "deepseek_key.txt",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_env_int("PORT", 8080),
            ai_enabled_setting=os.environ.get("AI_ENABLED", "").strip(),
            ai_model=os.environ.get("AI_MODEL", "deepseek-v4-pro"),
            ai_fallback_model=os.environ.get(
                "AI_FALLBACK_MODEL", "deepseek-v4-flash"
            ),
            ai_moderation_model=os.environ.get(
                "AI_MODERATION_MODEL", "deepseek-v4-flash"
            ),
            ai_base_url=os.environ.get("AI_BASE_URL", "https://api.deepseek.com"),
            ai_max_concurrent=_env_int("AI_MAX_CONCURRENT", 1),
            ai_prompt_char_limit=_env_int("AI_PROMPT_CHAR_LIMIT", 6000),
            ai_context_post_limit=_env_int("AI_CONTEXT_POST_LIMIT", 16),
            ai_max_output_tokens=_env_int("AI_MAX_OUTPUT_TOKENS", 1024),
            ai_request_timeout=_env_int("AI_REQUEST_TIMEOUT", 120),
            ai_network_retries=_env_int("AI_NETWORK_RETRIES", 1),
            ai_moderation_timeout=_env_int("AI_MODERATION_TIMEOUT", 30),
            ai_moderation_retries=_env_int("AI_MODERATION_RETRIES", 2),
        )
