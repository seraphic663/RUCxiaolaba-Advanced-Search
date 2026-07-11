"""Crawler constants and default paths."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB = DATA_DIR / "posts.db"
DEFAULT_CONFIG = DATA_DIR / "config.txt"
DEFAULT_LOCK_TIMEOUT = 180
# The write lock is a renewable lease rather than a permanent PID marker.  A
# replacement Railway container cannot inspect the old container's PID
# namespace, so a fresh heartbeat is the only cross-container liveness signal.
# Fifteen-second heartbeats leave ample slack while bounding crash recovery to
# roughly ninety seconds.
LOCK_LEASE_SECONDS = 90
LOCK_HEARTBEAT_SECONDS = 15
# Plain-PID markers from pre-lease deployments cannot prove cross-container
# liveness. Keep them for five minutes during migration, then allow recovery
# even if the PID happens to be reused in the new container.
LEGACY_LOCK_MAX_AGE_SECONDS = 5 * 60
# Compatibility name for code and tooling that still describes this value as
# the stale-lock threshold.
STALE_LOCK_SECONDS = LOCK_LEASE_SECONDS
BASE_URL = "https://ys.qimiaoyuanfen.com"
COMMUNITY_ID = 4
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 MicroMessenger/7.0.20.1781 "
        "MiniProgramEnv/Windows WindowsWechat/WMPF"
    ),
    "Referer": ("https://servicewechat.com/wxe23b94e06f71e89a/141/page-frame.html"),
    "Xweb-Xhr": "1",
    "Accept": "application/json",
}
