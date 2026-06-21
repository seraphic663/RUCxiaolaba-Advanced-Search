"""Crawler constants and default paths."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB = DATA_DIR / "posts.db"
DEFAULT_CONFIG = DATA_DIR / "config.txt"
DEFAULT_LOCK_TIMEOUT = 180
STALE_LOCK_SECONDS = 6 * 60 * 60
BASE_URL = "https://ys.qimiaoyuanfen.com"
COMMUNITY_ID = 4
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 MicroMessenger/7.0.20.1781 "
        "MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": (
        "https://servicewechat.com/"
        "wxe23b94e06f71e89a/141/page-frame.html"
    ),
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}
