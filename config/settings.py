"""
config/settings.py — Single source of truth for all configuration.

All other modules must import from here:
    from config.settings import settings

Never read os.environ directly outside this file.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (the directory containing this file's parent)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _require(key: str) -> str:
    """Read a required env var; abort with a clear message if missing."""
    val = os.getenv(key, "").strip()
    if not val:
        print(f"\n❌  Missing required setting: {key}")
        print(f"    Add it to your .env file.  Run ./setup.sh to reconfigure.\n")
        sys.exit(1)
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# ── Telegram ───────────────────────────────────────────────────
class _Settings:
    # --- Required ---
    BOT_TOKEN:    str = _require("TELEGRAM_BOT_TOKEN")
    API_ID:       int = int(_require("TELEGRAM_API_ID"))
    API_HASH:     str = _require("TELEGRAM_API_HASH")
    YOUR_USER_ID: int = int(_require("TELEGRAM_OWNER_ID"))

    # --- Metadata APIs ---
    TMDB_API_KEY: str = _optional("TMDB_API_KEY")
    TVDB_API_KEY: str = _optional("TVDB_API_KEY")

    # --- Paths ---
    BASE_DIR:      Path = _ROOT
    MEDIA_PATH:    str  = _optional("MEDIA_PATH",    str(_ROOT / "media"))
    DOWNLOAD_PATH: str  = _optional("DOWNLOAD_PATH", str(_ROOT / "downloads"))
    SESSION_PATH:  str  = _optional("SESSION_PATH",  str(_ROOT / "data" / "userbot_session"))
    LOG_FILE:      str  = _optional("LOG_FILE",      str(_ROOT / "logs" / "bot.log"))
    USERS_FILE:    str  = _optional("USERS_FILE",    str(_ROOT / "data" / "users.json"))
    REQUEST_LOG_DB: str = _optional("REQUEST_LOG_DB",str(_ROOT / "data" / "request_log.db"))
    MEDIA_DB:      str  = _optional("MEDIA_DB",      str(_ROOT / "data" / "media_library.db"))

    # --- Performance tuning ---
    PARALLEL_CONNECTIONS:  int = _int("PARALLEL_CONNECTIONS",  4)
    CHUNK_SIZE:            int = _int("CHUNK_SIZE",            524288)   # 512 KB
    FLUSH_EVERY:           int = _int("FLUSH_EVERY",           4194304)  # 4 MB
    MAX_QUEUE:             int = _int("MAX_QUEUE",             20)
    MAX_QUEUE_PER_USER:    int = _int("MAX_QUEUE_PER_USER",    3)
    MAX_RETRIES:           int = _int("MAX_RETRIES",           20)
    RETRY_BASE:            int = _int("RETRY_BASE",            8)
    RETRY_CAP:             int = _int("RETRY_CAP",             180)
    PROGRESS_EVERY:        int = _int("PROGRESS_EVERY",        5)
    WATCHDOG_TIMEOUT:      int = _int("WATCHDOG_TIMEOUT",      120)
    SMALL_FILE_LIMIT:      int = _int("SMALL_FILE_LIMIT",      10485760)  # 10 MB
    DISK_HEADROOM:         int = _int("DISK_HEADROOM",         209715200) # 200 MB
    HISTORY_MAX:           int = _int("HISTORY_MAX",           50)
    ENTITY_CACHE_TTL:      int = _int("ENTITY_CACHE_TTL",      3600)
    CHANNEL_SCAN_LIMIT:    int = _int("CHANNEL_SCAN_LIMIT",    1000)
    STRICT_SIZE_THRESHOLD: float = float(_optional("STRICT_SIZE_THRESHOLD", "0.03"))
    PENDING_FORCE_TTL:     int = _int("PENDING_FORCE_TTL",     86400)

    # --- Known movie channels (comma-separated list of channel IDs) ---
    @property
    def KNOWN_MOVIE_CHANNELS(self):
        raw = _optional("KNOWN_MOVIE_CHANNELS", "")
        if not raw:
            return []
        return [int(x.strip()) for x in raw.split(",") if x.strip()]

    # --- VPN ---
    @property
    def VPN_ENABLED(self) -> bool:
        return os.getenv("VPN_ENABLED", "false").strip().lower() in ("true", "1", "yes")

    VPN_CONFIG_FILE:  str = _optional("VPN_CONFIG_FILE")
    VPN_AUTH_FILE:    str = _optional("VPN_AUTH_FILE")

    @property
    def VPN_RECONNECT(self) -> bool:
        return os.getenv("VPN_RECONNECT", "true").strip().lower() not in ("false", "0", "no")

    VPN_MAX_RETRIES:  int = _int("VPN_MAX_RETRIES", 5)


settings = _Settings()
