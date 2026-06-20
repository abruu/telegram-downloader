#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   Telegram Download Bot v7.0 — Radarr/Sonarr Style          ║
║   Integrates media_classifier.py into v6.2 bot              ║
║                                                              ║
║   NEW IN v7.0:                                               ║
║   • Auto Movie/TV classification                             ║
║   • TMDB metadata lookup                                     ║
║   • Smart folder structure                                   ║
║   • Season/Episode auto-mapping                             ║
║   • Fuzzy series matching                                    ║
║   • Telegram approval for low-confidence files              ║
║   • Full classification DB                                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, re, sys, time, math, json, signal, asyncio
import logging, gc, hashlib, traceback, sqlite3
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any, Set, Tuple, List

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeFilename, PeerChannel
from telethon.tl.functions.channels import JoinChannelRequest

from telegram import (
    Update, Bot, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import RetryAfter, TimedOut, NetworkError
from config.settings import settings
from config.vpn import VpnManager
# Import our classifier module
from media_classifier import (
    MediaClassifier, MediaInfo, parse_filename,
    format_classification_msg, kb_classify,
    AUTO_CONFIRM_THRESHOLD, LOW_CONFIDENCE,
    MOVIES_FOLDER, TV_FOLDER, TMDB_IMG_BASE
)

# ══════════════════════════════════════════════════════════════
#  ★  YOUR SETTINGS  ★
# ══════════════════════════════════════════════════════════════

BOT_TOKEN     = settings.BOT_TOKEN
API_ID        = settings.API_ID
API_HASH      = settings.API_HASH
YOUR_USER_ID  = settings.YOUR_USER_ID

# Base media path — Movies and TV subfolders created automatically

MEDIA_PATH     = settings.MEDIA_PATH
DOWNLOAD_PATH  = settings.DOWNLOAD_PATH
SESSION_PATH   = settings.SESSION_PATH
LOG_FILE       = settings.LOG_FILE
USERS_FILE     = settings.USERS_FILE
REQUEST_LOG_DB = settings.REQUEST_LOG_DB
MEDIA_DB       = settings.MEDIA_DB

# Optional: get free key at https://www.themoviedb.org/settings/api
TMDB_API_KEY   = settings.TMDB_API_KEY

KNOWN_MOVIE_CHANNELS: List[int] = settings.KNOWN_MOVIE_CHANNELS

# ── Speed / behaviour ─────────────────────────────────────────
PARALLEL_CONNECTIONS  = settings.PARALLEL_CONNECTIONS
CHUNK_SIZE            = settings.CHUNK_SIZE
FLUSH_EVERY           = settings.FLUSH_EVERY
MAX_QUEUE             = settings.MAX_QUEUE
MAX_QUEUE_PER_USER    = settings.MAX_QUEUE_PER_USER
MAX_RETRIES           = settings.MAX_RETRIES
RETRY_BASE            = settings.RETRY_BASE
RETRY_CAP             = settings.RETRY_CAP
PROGRESS_EVERY        = settings.PROGRESS_EVERY
WATCHDOG_TIMEOUT      = settings.WATCHDOG_TIMEOUT
SMALL_FILE_LIMIT      = settings.SMALL_FILE_LIMIT
DISK_HEADROOM         = settings.DISK_HEADROOM
HISTORY_MAX           = settings.HISTORY_MAX
ENTITY_CACHE_TTL      = settings.ENTITY_CACHE_TTL
CHANNEL_SCAN_LIMIT    = settings.CHANNEL_SCAN_LIMIT
STRICT_SIZE_THRESHOLD = settings.STRICT_SIZE_THRESHOLD
PENDING_FORCE_TTL     = settings.PENDING_FORCE_TTL

_req_counter: int = 0

# ── Logging ───────────────────────────────────────────────────
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("dlbot")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ══════════════════════════════════════════════════════════════
#  HELPERS (same as v6.2)
# ══════════════════════════════════════════════════════════════

def human_size(b: int) -> str:
    if not b or b <= 0: return "0 B"
    units = ["B","KB","MB","GB","TB"]
    i = min(int(math.floor(math.log(max(b,1), 1024))), 4)
    return f"{b/math.pow(1024,i):.2f} {units[i]}"

def human_time(s: float) -> str:
    s = max(0, int(s))
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"

def progress_bar(pct: float, width: int = 16) -> str:
    pct = max(0.0, min(100.0, pct))
    f   = int(width * pct / 100)
    return "█" * f + "░" * (width - f)

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "download"

def parse_link(url: str):
    m = re.match(r"https?://t\.me/([^/c][^/]*)/(\d+)", url)
    if m: return m.group(1), int(m.group(2))
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", url)
    if m: return int(m.group(1)), int(m.group(2))
    return None, None

def get_filename(msg) -> str:
    media = msg.document or msg.video or msg.audio
    if media and hasattr(media, "attributes"):
        for a in media.attributes:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                return sanitize_filename(a.file_name)
    if msg.document:
        ext = (msg.document.mime_type or "bin").split("/")[-1]
        return f"file_{msg.id}.{ext}"
    if msg.video:  return f"video_{msg.id}.mp4"
    if msg.audio:  return f"audio_{msg.id}.mp3"
    if msg.photo:  return f"photo_{msg.id}.jpg"
    return f"media_{msg.id}"

def get_filesize(msg) -> int:
    if msg.document: return msg.document.size or 0
    if msg.video:    return getattr(msg.video, "size", 0) or 0
    return 0

def get_caption(msg) -> str:
    return (getattr(msg, "message", None) or
            getattr(msg, "caption", None) or "").strip()

def escape_md(text: str) -> str:
    return re.sub(r'([*_`\[])', r'\\\1', str(text))

def extract_channel_tags(filename: str) -> List[str]:
    return re.findall(r'@([\w]{3,})', filename)

_disk_cache: Dict[str, Any] = {"ts": 0, "free": 0, "total": 0}
def _refresh_disk():
    now = time.time()
    if now - _disk_cache["ts"] > 2:
        try:
            s = os.statvfs(MEDIA_PATH)
            _disk_cache["free"]  = s.f_bavail * s.f_frsize
            _disk_cache["total"] = s.f_blocks * s.f_frsize
            _disk_cache["ts"]    = now
        except Exception: pass

def disk_free()  -> int: _refresh_disk(); return _disk_cache["free"]
def disk_total() -> int: _refresh_disk(); return _disk_cache["total"]
def disk_used()  -> int: return disk_total() - disk_free()

_TG_LIMIT = 4096
def split_message(text: str) -> list:
    if len(text) <= _TG_LIMIT: return [text]
    chunks, current, current_len = [], [], 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > _TG_LIMIT and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line); current_len += line_len
    if current: chunks.append("\n".join(current))
    return chunks

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Cancel", callback_data="cancel"),
        InlineKeyboardButton("📊 Status", callback_data="status"),
    ]])

def kb_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📁 Queue",   callback_data="queue"),
        InlineKeyboardButton("💿 Storage", callback_data="storage"),
    ]])

def kb_approve(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny_{user_id}"),
    ]])

def kb_force(job_id: str, filename: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Force Re-download", callback_data=f"force_{job_id}"),
        InlineKeyboardButton("📁 Queue",             callback_data="queue"),
    ]])


# ══════════════════════════════════════════════════════════════
#  REQUEST AUDIT LOG (same as v6.2)
# ══════════════════════════════════════════════════════════════

class RequestAuditLog:
    _CREATE = """
    CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        username TEXT, ptb_msg_id INTEGER, requested_filename TEXT,
        ptb_filesize INTEGER, ptb_type TEXT, resolved_filename TEXT,
        resolved_filesize INTEGER, queue_result TEXT, job_id TEXT,
        outcome TEXT DEFAULT 'pending', dest_path TEXT,
        created_at TEXT NOT NULL, completed_at TEXT,
        error_details TEXT, validation_passed INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_user_id ON requests(user_id);
    CREATE INDEX IF NOT EXISTS idx_job_id  ON requests(job_id);
    CREATE INDEX IF NOT EXISTS idx_created ON requests(created_at);
    """

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._CREATE)
        self._conn.commit()
        global _req_counter
        try:
            today = datetime.now().strftime("%Y%m%d")
            cur   = self._conn.execute(
                "SELECT COUNT(*) FROM requests WHERE created_at LIKE ?",
                (today + "%",))
            _req_counter = cur.fetchone()[0]
        except Exception:
            _req_counter = 0

    def next_id(self) -> str:
        global _req_counter
        _req_counter += 1
        return f"REQ-{datetime.now().strftime('%Y%m%d')}-{_req_counter:06d}"

    def create(self, request_id, user_id, username, ptb_msg_id,
               requested_filename, ptb_filesize, ptb_type):
        try:
            self._conn.execute(
                """INSERT OR IGNORE INTO requests
                   (request_id,user_id,username,ptb_msg_id,
                    requested_filename,ptb_filesize,ptb_type,
                    created_at,outcome)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (request_id, user_id, username, ptb_msg_id,
                 requested_filename, ptb_filesize, ptb_type,
                 datetime.now().isoformat(timespec="seconds"), "pending"))
            self._conn.commit()
        except Exception as e:
            log.error(f"[audit] create: {e}")

    def set_resolved(self, rid, filename, filesize, result, job_id=""):
        try:
            self._conn.execute(
                """UPDATE requests SET resolved_filename=?,resolved_filesize=?,
                   queue_result=?,job_id=? WHERE request_id=?""",
                (filename, filesize, result, job_id, rid))
            self._conn.commit()
        except Exception as e:
            log.error(f"[audit] set_resolved: {e}")

    def set_resolve_failed(self, rid, error):
        try:
            self._conn.execute(
                """UPDATE requests SET queue_result='resolve_failed',
                   outcome='failed',error_details=?,completed_at=?
                   WHERE request_id=?""",
                (error, datetime.now().isoformat(timespec="seconds"), rid))
            self._conn.commit()
        except Exception as e:
            log.error(f"[audit] set_resolve_failed: {e}")

    def complete(self, rid, outcome, dest_path="",
                 error_details="", validation_passed=None):
        try:
            self._conn.execute(
                """UPDATE requests SET outcome=?,dest_path=?,completed_at=?,
                   error_details=?,validation_passed=? WHERE request_id=?""",
                (outcome, dest_path,
                 datetime.now().isoformat(timespec="seconds"),
                 error_details, validation_passed, rid))
            self._conn.commit()
        except Exception as e:
            log.error(f"[audit] complete: {e}")

    def close(self):
        try: self._conn.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════
#  USER MANAGER (same as v6.2)
# ══════════════════════════════════════════════════════════════

class UserManager:
    def __init__(self, path: str):
        self._path = path
        self._data: Dict[str, Any] = {"approved": {}, "pending": {}}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self._data["approved"] = loaded.get("approved", {})
                    self._data["pending"]  = loaded.get("pending",  {})
        except Exception as e:
            log.warning(f"[users] load: {e}")

    def _save(self):
        try:
            p   = Path(self._path); p.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(p) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(p))
        except Exception as e:
            log.warning(f"[users] save: {e}")

    def is_admin(self, uid):    return YOUR_USER_ID != 0 and uid == YOUR_USER_ID
    def is_approved(self, uid): return str(uid) in self._data["approved"]
    def is_pending(self, uid):  return str(uid) in self._data["pending"]
    def can_use(self, uid):     return self.is_admin(uid) or self.is_approved(uid)

    def add_pending(self, uid, name):
        self._data["pending"][str(uid)] = {
            "name": name,
            "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        self._save()

    def approve(self, uid):
        key  = str(uid)
        info = self._data["pending"].pop(key, None) or {"name": "unknown"}
        self._data["approved"][key] = {
            "name": info.get("name","unknown"),
            "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        self._save(); return True

    def revoke(self, uid):
        key     = str(uid)
        existed = key in self._data["approved"]
        self._data["approved"].pop(key, None)
        self._data["pending"].pop(key, None)
        self._save(); return existed

    def deny(self, uid):
        self._data["pending"].pop(str(uid), None); self._save()

    def user_name(self, uid):
        key  = str(uid)
        info = self._data["approved"].get(key) or self._data["pending"].get(key, {})
        return info.get("name", str(uid))

    def approved_list(self): return [{"id":int(k),**v} for k,v in self._data["approved"].items()]
    def pending_list(self):  return [{"id":int(k),**v} for k,v in self._data["pending"].items()]


# ══════════════════════════════════════════════════════════════
#  SPEED TRACKER
# ══════════════════════════════════════════════════════════════

class SpeedTracker:
    def __init__(self, window=10.0):
        self._window  = window
        self._samples = deque()
        self._peak    = 0.0

    def add(self, b: int):
        now = time.time()
        self._samples.append((now, b))
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def speed(self):
        if len(self._samples) < 2: return 0.0
        total   = sum(b for _, b in self._samples)
        elapsed = self._samples[-1][0] - self._samples[0][0]
        spd     = total / elapsed if elapsed > 0 else 0.0
        if spd > self._peak: self._peak = spd
        return spd

    @property
    def peak(self): return self._peak
    def reset(self): self._samples.clear(); self._peak = 0.0


# ══════════════════════════════════════════════════════════════
#  DOWNLOAD JOB
# ══════════════════════════════════════════════════════════════

class DownloadJob:
    def __init__(self, chat_id, user_id, tl_msg, filename, filesize,
                 status_id, dest_folder, caption="", ptb_msg_id=0,
                 request_id="", media_info: Optional[MediaInfo] = None):
        self.chat_id     = chat_id
        self.user_id     = user_id
        self.tl_msg      = tl_msg
        self.filename    = filename
        self.filesize    = filesize
        self.status_id   = status_id
        self.user_folder = dest_folder
        self.caption     = caption
        self.ptb_msg_id  = ptb_msg_id
        self.request_id  = request_id
        self.media_info  = media_info

        self.cancel      = asyncio.Event()
        self.started     = time.time()
        self.speed       = SpeedTracker()
        self._bytes_done = 0

        channel_id = getattr(getattr(tl_msg,"peer_id",None),"channel_id",0) or \
                     getattr(tl_msg,"chat_id",0) or 0
        self.job_id = hashlib.md5(
            f"{chat_id}:{channel_id}:{tl_msg.id}:{filename}:{ptb_msg_id}".encode()
        ).hexdigest()[:8]
        self.identity: Tuple = (channel_id, tl_msg.id, user_id, ptb_msg_id)
        self._delivery_stem  = Path(filename).stem.lower()
        self._delivery_tl_id = tl_msg.id

    @property
    def dest(self) -> str:
        return os.path.join(self.user_folder, self.filename)

    @property
    def tmp(self) -> str:
        return self.dest + ".part"


# ══════════════════════════════════════════════════════════════
#  PENDING CLASSIFICATION — holds jobs waiting for user input
# ══════════════════════════════════════════════════════════════

class PendingClassification:
    def __init__(self, request_id, chat_id, user_id, tl_msg,
                 filename, filesize, status_id, ptb_msg_id,
                 caption, parsed_info: MediaInfo):
        self.request_id  = request_id
        self.chat_id     = chat_id
        self.user_id     = user_id
        self.tl_msg      = tl_msg
        self.filename    = filename
        self.filesize    = filesize
        self.status_id   = status_id
        self.ptb_msg_id  = ptb_msg_id
        self.caption     = caption
        self.parsed_info = parsed_info
        self.created_at  = time.time()


# ══════════════════════════════════════════════════════════════
#  DOWNLOAD BOT v7
# ══════════════════════════════════════════════════════════════

class DownloadBot:
    def __init__(self):
        Path(MEDIA_PATH).mkdir(parents=True, exist_ok=True)

        self.client = TelegramClient(
            SESSION_PATH, API_ID, API_HASH,
            connection_retries=None, request_retries=10,
            flood_sleep_threshold=60, auto_reconnect=True,
            receive_updates=False)

        self.bot    = Bot(token=BOT_TOKEN)
        self._app: Optional[Application] = None
        self.users  = UserManager(USERS_FILE)
        self.audit  = RequestAuditLog(REQUEST_LOG_DB)

        # Media classifier — Radarr/Sonarr style
        self.classifier = MediaClassifier(
            base_path = MEDIA_PATH,
            db_path   = MEDIA_DB,
            tmdb_key  = TMDB_API_KEY)

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._active: Optional[DownloadJob] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._history: deque = deque(maxlen=HISTORY_MAX)
        self.stats = {"queued":0,"done":0,"failed":0,"cancelled":0,"bytes":0}

        self._entity_cache: Dict[int, tuple] = {}
        self._force_ids: Set[str] = set()
        self._pending_force: Dict[str, Tuple[DownloadJob, float]] = {}
        self._last_completed: Dict[int, DownloadJob] = {}
        self._last_progress  = time.time()
        self._known_channels: List[int] = list(KNOWN_MOVIE_CHANNELS)
        self._joined_channels: Set[str] = set()

        # Pending classification: request_id → PendingClassification
        self._pending_cls: Dict[str, PendingClassification] = {}

    # ── Helpers ───────────────────────────────────────────────

    def user_queue_count(self, uid):
        n  = 1 if (self._active and self._active.user_id == uid) else 0
        n += sum(1 for j in list(self._queue._queue) if j.user_id == uid)
        return n

    async def _tg_call(self, coro_fn, retries=3):
        """
        Retry a python-telegram-bot API call with exponential backoff.
        coro_fn must be a zero-argument callable that returns a fresh coroutine
        each time it is called (e.g. lambda: bot.send_message(...)).
        A coroutine object can only be awaited once; passing the same coroutine
        on retry causes "cannot reuse already awaited coroutine".
        Attempt 1 → wait 5s
        Attempt 2 → wait 15s
        Attempt 3 → wait 30s
        Logs request type, retry count, and failure reason.
        """
        _WAIT = [5, 15, 30]
        for i in range(retries):
            t0 = time.time()
            try:
                result = await coro_fn()
                return result
            except RetryAfter as e:
                wait = e.retry_after + 1
                log.warning(f"[tg_call] FloodWait retry {i+1}/{retries}: "
                            f"wait={wait}s")
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError) as e:
                wait = _WAIT[min(i, len(_WAIT)-1)]
                elapsed = int((time.time()-t0)*1000)
                log.warning(f"[tg_call] {type(e).__name__} retry {i+1}/{retries}: "
                            f"elapsed={elapsed}ms wait={wait}s reason={e}")
                if i == retries - 1:
                    log.error(f"[tg_call] Exhausted {retries} retries: {e}")
                    raise
                await asyncio.sleep(wait)
            except Exception as e:
                log.warning(f"[tg_call] Unexpected error retry {i+1}/{retries}: {e}")
                if i == retries - 1: raise
                await asyncio.sleep(_WAIT[min(i, len(_WAIT)-1)])
        return None

    async def send(self, chat_id, text, markup=None) -> Optional[int]:
        try:
            m = await self._tg_call(
                lambda: self.bot.send_message(chat_id=chat_id, text=text,
                                              parse_mode="Markdown", reply_markup=markup))
            return m.message_id if m else None
        except Exception as e:
            log.warning(f"send: {e}"); return None

    async def edit(self, chat_id, msg_id, text, markup=None):
        if not msg_id: return
        try:
            await self._tg_call(
                lambda: self.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=text, parse_mode="Markdown", reply_markup=markup))
        except Exception as e:
            if "not modified" not in str(e).lower():
                log.debug(f"edit: {e}")

    async def notify(self, chat_id, text, markup=None):
        await self.send(chat_id, text, markup)

    async def notify_admin(self, text, markup=None):
        if YOUR_USER_ID:
            await self.send(YOUR_USER_ID, text, markup)

    # ── Classification pipeline ───────────────────────────────

    async def classify_and_enqueue(self, chat_id, user_id, tl_msg,
                                   filename, filesize, status_id,
                                   ptb_msg_id, caption, request_id):
        """
        Always ask user to confirm Movie or TV before downloading.
        Metadata (TMDB) is fetched after the user taps a button.
        """
        # Parse filename for title/year hints only — no auto-classify
        parsed = parse_filename(filename)
        info   = MediaInfo(
            media_type      = parsed.media_type,
            canonical_title = parsed.clean_title,
            year            = parsed.year or 0,
            season          = parsed.season,
            episodes        = parsed.episodes,
            confidence      = parsed.confidence,
            source          = "pending",
        )

        pend = PendingClassification(
            request_id  = request_id,
            chat_id     = chat_id,
            user_id     = user_id,
            tl_msg      = tl_msg,
            filename    = filename,
            filesize    = filesize,
            status_id   = status_id,
            ptb_msg_id  = ptb_msg_id,
            caption     = caption,
            parsed_info = info,
        )
        self._pending_cls[request_id] = pend
        self.classifier.db.add_pending(request_id, chat_id, filename, parsed)

        short = Path(filename).stem[:45]
        year  = f" ({info.year})" if info.year else ""
        title = info.canonical_title or short

        # Show season/episode hint if detected
        ep_hint = ""
        if parsed.season and parsed.episodes:
            ep_hint = (f"\n� Detected: "
                       f"`S{parsed.season:02d}E"
                       f"{'E'.join(f'{e:02d}' for e in parsed.episodes)}`")
        elif parsed.season:
            ep_hint = f"\n🎞 Detected: `Season {parsed.season:02d}`"

        await self.edit(chat_id, status_id,
            f"📂 *Is this a Movie or TV Show?*\n\n"
            f"📄 `{short}`\n"
            f"🎯 Title: *{title}{year}*{ep_hint}\n"
            f"💾 `{human_size(filesize)}`\n\n"
            f"Tap below — metadata will be fetched automatically:",
            markup=kb_classify(request_id, filename))

        log.info(f"[classify] [{request_id}] Awaiting user type selection for '{filename}'")

    async def _enqueue_job(self, chat_id, user_id, tl_msg, filename,
                           filesize, status_id, dest_folder, caption,
                           ptb_msg_id, request_id,
                           media_info: Optional[MediaInfo] = None):
        """Actually enqueue a DownloadJob after folder is decided."""
        job  = DownloadJob(chat_id, user_id, tl_msg, filename, filesize,
                           status_id, dest_folder, caption, ptb_msg_id,
                           request_id, media_info)

        # Duplicate check
        if self._active and self._active.identity == job.identity:
            await self.edit(chat_id, status_id,
                f"⚠️ *Already downloading!*\n📄 `{filename}`\n🔖 `{request_id}`")
            return

        if self.user_queue_count(user_id) >= MAX_QUEUE_PER_USER:
            await self.edit(chat_id, status_id,
                f"⚠️ *Your queue is full!*\n🔖 `{request_id}`")
            self.audit.set_resolved(request_id, filename, filesize, "user_full")
            return

        try:
            self._queue.put_nowait(job)
            self.stats["queued"] += 1
            self.audit.set_resolved(request_id, filename, filesize, "ok", job.job_id)
            qpos = self._queue.qsize() + (1 if self._active else 0)
            log.info(f"[queue] [{request_id}] Enqueued #{qpos} '{filename}'")
        except asyncio.QueueFull:
            await self.edit(chat_id, status_id,
                f"❌ *Queue full!*\n🔖 `{request_id}`")
            self.audit.set_resolved(request_id, filename, filesize, "full")

    # ── Entity resolution ──────────────────────────────────────

    async def resolve_entity(self, channel_id):
        cached = self._entity_cache.get(channel_id)
        if cached:
            entity, ts = cached
            if time.time() - ts < ENTITY_CACHE_TTL: return entity
        try:
            entity = await self.client.get_entity(PeerChannel(channel_id))
            self._entity_cache[channel_id] = (entity, time.time())
            return entity
        except Exception as e:
            log.warning(f"resolve_entity({channel_id}): {e}")
            return None

    # ── Validation ─────────────────────────────────────────────

    def _validate_tl_msg(self, tl_msg, ptb_filesize, ptb_type,
                         ptb_filename="") -> bool:
        if not tl_msg or not tl_msg.media: return False
        tl_size = get_filesize(tl_msg)
        if ptb_filesize > 0 and tl_size > 0:
            ratio = abs(tl_size - ptb_filesize) / ptb_filesize
            if ratio > STRICT_SIZE_THRESHOLD:
                log.warning(f"[validate] REJECTED size ratio={ratio:.1%}")
                return False
        if ptb_filename and not ptb_filename.startswith(
                ("media_","video_","audio_","photo_","file_")):
            ptb_stem = Path(ptb_filename).stem.lower()
            tl_stem  = Path(get_filename(tl_msg)).stem.lower()
            if ptb_stem and tl_stem and ptb_stem != tl_stem:
                log.warning(f"[validate] REJECTED filename mismatch")
                return False
        return True

    # ── Auto-join ──────────────────────────────────────────────

    async def _autojoin_channel(self, tag, rid):
        if tag in self._joined_channels: return True
        try:
            await self.client.get_entity(tag)
            self._joined_channels.add(tag); return True
        except Exception: pass
        try:
            await self.client(JoinChannelRequest(tag))
            self._joined_channels.add(tag)
            log.info(f"[autojoin] [{rid}] Joined @{tag}")
            await asyncio.sleep(2); return True
        except Exception as e:
            log.warning(f"[autojoin] [{rid}] Failed @{tag}: {e}"); return False

    async def _scan_channel(self, channel, ptb_filesize, ptb_type,
                            ptb_filename, rid, label=""):
        scanned = 0
        try:
            async for m in self.client.iter_messages(
                    channel, limit=CHANNEL_SCAN_LIMIT):
                if not m.media: continue
                scanned += 1
                if self._validate_tl_msg(m, ptb_filesize, ptb_type, ptb_filename):
                    log.info(f"[fetch] [{rid}] {label}: ✅ scanned={scanned}")
                    return m
        except Exception as e:
            log.warning(f"[fetch] [{rid}] {label} scan failed: {e}")
        return None

    async def get_telethon_message(self, ptb_msg, ptb_filesize=0,
                                   ptb_type="unknown", ptb_filename="",
                                   request_id=""):
        origin = getattr(ptb_msg, "forward_origin", None)
        log.info(f"[fetch] [{request_id}] origin={type(origin).__name__ if origin else 'None'}")

        # S0: Saved Messages
        if ptb_filename:
            ptb_stem = re.sub(r'[\s._\-]+',' ',
                       Path(ptb_filename).stem.lower()).strip()
            try:
                scanned = 0
                async for m in self.client.iter_messages("me", limit=200):
                    if not m.media: continue
                    scanned += 1
                    tl_stem = re.sub(r'[\s._\-]+',' ',
                              Path(get_filename(m)).stem.lower()).strip()
                    if ptb_stem and not ptb_stem.startswith(
                            ("media_","video_","audio_","photo_","file_")):
                        if ptb_stem != tl_stem: continue
                    if self._validate_tl_msg(m, ptb_filesize, ptb_type, ptb_filename):
                        log.info(f"[fetch] [{request_id}] S0: ✅ scanned={scanned}")
                        return m
            except Exception as e:
                log.warning(f"[fetch] [{request_id}] S0: {e}")

        # S0b+S0c: auto-join @tag
        for tag in extract_channel_tags(ptb_filename):
            if await self._autojoin_channel(tag, request_id):
                m = await self._scan_channel(tag, ptb_filesize, ptb_type,
                                             ptb_filename, request_id,
                                             f"S0c(@{tag})")
                if m: return m

        # S0d: known channels
        for chan_id in self._known_channels:
            entity = await self.resolve_entity(chan_id)
            if not entity: continue
            m = await self._scan_channel(entity, ptb_filesize, ptb_type,
                                         ptb_filename, request_id,
                                         f"S0d({chan_id})")
            if m: return m

        # S1a/S1b: forward_origin
        if origin and hasattr(origin,"chat") and origin.chat and \
                getattr(origin,"message_id",None):
            chat = origin.chat
            mid  = origin.message_id
            src  = getattr(chat,"username",None) or chat.id
            try:
                m = await self.client.get_messages(src, ids=mid)
                if m and m.media and self._validate_tl_msg(
                        m, ptb_filesize, ptb_type, ptb_filename):
                    log.info(f"[fetch] [{request_id}] S1a: ✅")
                    return m
            except Exception as e:
                log.info(f"[fetch] [{request_id}] S1a: {e}")

            cid = getattr(chat,"id",None)
            if cid:
                entity = await self.resolve_entity(cid)
                if entity:
                    try:
                        m = await self.client.get_messages(entity, ids=mid)
                        if m and m.media and self._validate_tl_msg(
                                m, ptb_filesize, ptb_type, ptb_filename):
                            log.info(f"[fetch] [{request_id}] S1b: ✅")
                            return m
                    except Exception as e:
                        log.info(f"[fetch] [{request_id}] S1b: {e}")

        # S2: Saved Messages by message_id
        fwd_msg_id  = getattr(origin,"message_id",None) if origin else None
        fwd_chan_id = getattr(getattr(origin,"chat",None),"id",None) if origin else None
        if fwd_msg_id:
            try:
                async for m in self.client.iter_messages("me", limit=100):
                    if not (m.media and m.fwd_from): continue
                    src_id  = getattr(m.fwd_from,"from_id",None)
                    src_chan = getattr(src_id,"channel_id",None) if src_id else None
                    orig_id = getattr(m.fwd_from,"channel_post",None) or \
                              getattr(m.fwd_from,"saved_from_msg_id",None)
                    matched = (src_chan == fwd_chan_id and orig_id == fwd_msg_id) \
                              if (fwd_chan_id and src_chan and orig_id) \
                              else (orig_id == fwd_msg_id if orig_id else False)
                    if matched and self._validate_tl_msg(
                            m, ptb_filesize, ptb_type, ptb_filename):
                        log.info(f"[fetch] [{request_id}] S2: ✅")
                        return m
            except Exception as e:
                log.warning(f"[fetch] [{request_id}] S2: {e}")

        log.warning(f"[fetch] [{request_id}] ❌ All strategies failed")
        return None

    # ── Core downloader ────────────────────────────────────────

    async def _turbo_download(self, job: DownloadJob) -> bool:
        n, total, chunk, tmp = PARALLEL_CONNECTIONS, job.filesize, CHUNK_SIZE, job.tmp
        if not os.path.exists(tmp):
            with open(tmp,"ab") as f: f.seek(total-1); f.write(b"\x00")
        job._bytes_done = 0
        region_size = math.ceil(math.ceil(total/chunk)/n)*chunk
        regions = [(i,i*region_size,min((i+1)*region_size,total))
                   for i in range(n) if i*region_size < total]
        downloaded = [0]*n; errors = [None]*n
        write_lock = asyncio.Lock(); edit_lock = asyncio.Lock()
        start_time = time.time(); last_edit = [time.time()]

        async def worker(idx, start, end):
            rbytes = end-start; offset = start; buf = bytearray(); recv = 0
            count  = math.ceil(rbytes/chunk)
            try:
                async for data in self.client.iter_download(
                        job.tl_msg.media, offset=start, stride=chunk,
                        limit=count, chunk_size=chunk):
                    if job.cancel.is_set(): return
                    rem = rbytes-recv
                    if rem <= 0: break
                    data = data[:rem]; buf.extend(data)
                    recv += len(data); downloaded[idx] = recv
                    job.speed.add(len(data))
                    job._bytes_done = min(sum(downloaded), total)
                    self._last_progress = time.time()
                    now = time.time()
                    if now-last_edit[0] >= PROGRESS_EVERY and not edit_lock.locked():
                        async with edit_lock:
                            if time.time()-last_edit[0] >= PROGRESS_EVERY:
                                last_edit[0] = time.time()
                                dl  = min(sum(downloaded), total)
                                pct = min(dl/total*100, 100.0)
                                spd = job.speed.speed
                                eta = (total-dl)/spd if spd > 0 else 0
                                await self.edit(job.chat_id, job.status_id,
                                    f"⬇️ *Downloading* ⚡ ×{n} streams\n\n"
                                    f"`{progress_bar(pct)}`  `{pct:.1f}%`\n\n"
                                    f"📦 `{human_size(dl)}` / `{human_size(total)}`\n"
                                    f"🚀 `{human_size(spd)}/s`  📈 Peak: `{human_size(job.speed.peak)}/s`\n"
                                    f"⏱ ETA: `{human_time(eta)}`  💿 Free: `{human_size(disk_free())}`\n"
                                    f"📄 `{job.filename}`\n🔖 `{job.request_id}`",
                                    markup=kb_cancel())
                    if len(buf) >= FLUSH_EVERY:
                        async with write_lock:
                            with open(tmp,"r+b") as f: f.seek(offset); f.write(buf)
                        offset += len(buf); buf = bytearray(); gc.collect()
                    if recv >= rbytes: break
                if buf and not job.cancel.is_set():
                    async with write_lock:
                        with open(tmp,"r+b") as f: f.seek(offset); f.write(buf)
            except asyncio.CancelledError: pass
            except Exception as e: errors[idx]=e; log.warning(f"[stream {idx}] {e}")

        await asyncio.gather(*[asyncio.create_task(worker(i,s,e))
                                for i,s,e in regions], return_exceptions=True)
        if job.cancel.is_set(): return False
        if any(e is not None for e in errors):
            raise RuntimeError(f"Stream errors: {[str(e) for e in errors if e]}")
        got = sum(downloaded)
        if got < total*0.99:
            raise RuntimeError(f"Incomplete: {human_size(got)} / {human_size(total)}")
        return True

    async def _standard_download(self, job: DownloadJob):
        tmp = job.tmp
        resume_from = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        job._bytes_done = resume_from
        start_time = time.time(); last_edit=[time.time()]; last_bytes=[0]; last_ts=[start_time]

        async def progress(current, total):
            if job.cancel.is_set(): raise asyncio.CancelledError()
            now = time.time()
            if now-last_edit[0] < PROGRESS_EVERY: return
            last_edit[0] = now
            total_dl = resume_from+current
            total_sz = (resume_from+total) if total else job.filesize
            pct  = min((total_dl/total_sz*100) if total_sz else 0, 100.0)
            dt   = now-last_ts[0]
            spd  = (current-last_bytes[0])/dt if dt>0 else 0
            last_bytes[0]=current; last_ts[0]=now
            job.speed.add(int(abs(spd)*dt))
            self._last_progress = now; job._bytes_done = total_dl
            eta = max((total_sz-total_dl)/job.speed.speed,0) if job.speed.speed>0 else 0
            await self.edit(job.chat_id, job.status_id,
                f"⬇️ *Downloading*\n\n"
                f"`{progress_bar(pct)}`  `{pct:.1f}%`\n\n"
                f"📦 `{human_size(total_dl)}` / `{human_size(total_sz)}`\n"
                f"🚀 `{human_size(job.speed.speed)}/s`  📈 Peak: `{human_size(job.speed.peak)}/s`\n"
                f"⏱ ETA: `{human_time(eta)}`  💿 Free: `{human_size(disk_free())}`\n"
                f"📄 `{job.filename}`\n🔖 `{job.request_id}`",
                markup=kb_cancel())
            gc.collect()

        await self.client.download_media(job.tl_msg, file=tmp,
                                         progress_callback=progress)

    def _validate_delivery(self, job) -> Tuple[bool, str]:
        if not os.path.exists(job.tmp): return False, "part missing"
        if os.path.getsize(job.tmp) == 0: return False, "part empty"
        if getattr(job.tl_msg,"id",None) != job._delivery_tl_id:
            return False, "tl_msg.id mismatch"
        if Path(job.dest).stem.lower() != job._delivery_stem:
            return False, "filename stem changed"
        return True, "ok"

    async def _run_job(self, job: DownloadJob):
        dest = job.dest; tmp = job.tmp; rid = job.request_id
        force = job.job_id in self._force_ids

        # Ensure destination folder exists
        Path(job.user_folder).mkdir(parents=True, exist_ok=True)

        log.info(f"[job] [{rid}] '{job.filename}' → {job.user_folder}")

        if not force and os.path.exists(dest):
            existing = os.path.getsize(dest)
            if job.filesize == 0 or existing == job.filesize:
                self._pending_force[job.job_id] = (job, time.time())
                await self.edit(job.chat_id, job.status_id,
                    f"✅ *Already on disk!*\n\n"
                    f"📄 `{job.filename}`\n💾 `{human_size(existing)}`\n"
                    f"📁 `{job.user_folder}`\n🔖 `{rid}`\n\n"
                    f"Press *Force Re-download* to re-download.",
                    markup=kb_force(job.job_id, job.filename))
                self._finish_job(job,"done",existing)
                self.audit.complete(rid,"done",dest_path=dest,validation_passed=1)
                return

        self._force_ids.discard(job.job_id)
        self._pending_force.pop(job.job_id, None)

        if job.filesize and disk_free() < job.filesize + DISK_HEADROOM:
            await self.edit(job.chat_id, job.status_id,
                f"❌ *Not enough disk space!*\n\n"
                f"📦 Need: `{human_size(job.filesize+DISK_HEADROOM)}`\n"
                f"💿 Free: `{human_size(disk_free())}`\n🔖 `{rid}`")
            self._finish_job(job,"failed",0)
            self.audit.complete(rid,"failed",error_details="Not enough disk space")
            return

        use_turbo = job.filesize >= SMALL_FILE_LIMIT and job.tl_msg.document is not None
        mode_str  = f"⚡ Turbo ×{PARALLEL_CONNECTIONS}" if use_turbo else "Standard"
        resume    = os.path.exists(tmp) and os.path.getsize(tmp) > 0
        resume_str = f"\n♻️ Resuming from `{human_size(os.path.getsize(tmp))}`" if resume else ""

        # Show classification info in start message
        mi = job.media_info
        cls_line = ""
        if mi:
            icon  = "🎬" if mi.media_type=="movie" else "📺"
            year  = f" ({mi.year})" if mi.year else ""
            ep    = ""
            if mi.season and mi.episodes:
                ep = f" `S{mi.season:02d}E{'E'.join(f'{e:02d}' for e in mi.episodes)}`"
            cls_line = f"\n{icon} *{mi.canonical_title}{year}*{ep}"

        await self.edit(job.chat_id, job.status_id,
            f"📋 *Download Starting*\n\n"
            f"📄 `{job.filename}`\n"
            f"💾 `{human_size(job.filesize)}`\n"
            f"⚡ Mode: `{mode_str}`"
            f"{cls_line}\n"
            f"📁 `{job.user_folder}`\n"
            f"🔖 `{rid}`{resume_str}\n\n"
            f"⬇️ *Connecting...*",
            markup=kb_cancel())

        start_time = time.time(); success = cancelled = False

        for attempt in range(1, MAX_RETRIES+1):
            if job.cancel.is_set(): cancelled=True; break
            try:
                if use_turbo:
                    ok = await self._turbo_download(job)
                    if ok is False: cancelled=True; break
                else:
                    await self._standard_download(job)

                ok, reason = self._validate_delivery(job)
                if not ok:
                    await self.notify_admin(
                        f"🚨 *Delivery validation FAILED*\n🔖 `{rid}`\n`{reason}`")
                    await self.edit(job.chat_id, job.status_id,
                        f"❌ *Validation error*\n🔖 `{rid}`\nForward again.")
                    self._finish_job(job,"failed",0)
                    self.audit.complete(rid,"failed",
                                        error_details=reason, validation_passed=0)
                    return

                self.audit.complete(rid,"done",validation_passed=1,dest_path=dest)
                os.replace(tmp, dest)
                success=True; break

            except asyncio.CancelledError: cancelled=True; break
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds+5)
            except Exception as e:
                log.warning(f"[{job.job_id}] attempt {attempt}: {e}")
                if job.cancel.is_set(): cancelled=True; break
                if attempt >= MAX_RETRIES: break
                wait = min(RETRY_BASE*attempt, RETRY_CAP)
                part = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                await self.edit(job.chat_id, job.status_id,
                    f"⚠️ *Retrying* `{attempt}/{MAX_RETRIES}`\n"
                    f"⏳ Wait: `{wait}s`  ♻️ Saved: `{human_size(part)}`\n"
                    f"📄 `{job.filename}`\n🔖 `{rid}`",
                    markup=kb_cancel())
                if time.time()-self._last_progress > WATCHDOG_TIMEOUT:
                    try:
                        await self.client.disconnect()
                        await asyncio.sleep(5)
                        await self.client.connect()
                    except Exception: pass
                await asyncio.sleep(wait)

        elapsed = time.time()-start_time

        if cancelled:
            part = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            await self.edit(job.chat_id, job.status_id,
                f"🛑 *Cancelled*\n\n📄 `{job.filename}`\n"
                f"♻️ Partial: `{human_size(part)}`\n🔖 `{rid}`\n"
                f"Forward again to resume.",
                markup=kb_done())
            self._finish_job(job,"cancelled",part)
            self.audit.complete(rid,"cancelled")
        elif success:
            final_size = os.path.getsize(dest)
            avg_speed  = final_size/elapsed if elapsed > 0 else 0
            self._last_completed[job.user_id] = job

            mi = job.media_info
            cls_done = ""
            if mi:
                icon = "🎬" if mi.media_type=="movie" else "📺"
                year = f" ({mi.year})" if mi.year else ""
                cls_done = f"\n{icon} *{mi.canonical_title}{year}*"

            await self.edit(job.chat_id, job.status_id,
                f"✅ *Download Complete!*\n\n"
                f"📄 `{job.filename}`\n"
                f"💾 `{human_size(final_size)}`\n"
                f"🚀 Avg: `{human_size(avg_speed)}/s`  📈 Peak: `{human_size(job.speed.peak)}/s`\n"
                f"⏱ Time: `{human_time(elapsed)}`"
                f"{cls_done}\n"
                f"📁 `{job.user_folder}`\n"
                f"💿 Free: `{human_size(disk_free())}`\n"
                f"🔖 `{rid}`\n\n🎬 *Ready to watch!*",
                markup=kb_done())
            self._finish_job(job,"done",final_size)
        else:
            part = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            self.audit.complete(rid,"failed",
                                error_details=f"Exhausted {MAX_RETRIES} retries")
            await self.edit(job.chat_id, job.status_id,
                f"❌ *Download Failed*\n\n📄 `{job.filename}`\n"
                f"🔄 Tried `{MAX_RETRIES}` times\n"
                f"♻️ Partial: `{human_size(part)}`\n🔖 `{rid}`\n"
                f"Forward again to resume.",
                markup=kb_done())
            self._finish_job(job,"failed",part)
        gc.collect()

    def _finish_job(self, job, outcome, size):
        self.stats[outcome] += 1
        self.stats["bytes"] += size
        self._history.appendleft({
            "ts":      datetime.now().strftime("%H:%M %d/%m"),
            "name":    job.filename,
            "size":    human_size(size),
            "outcome": outcome,
            "elapsed": human_time(time.time()-job.started),
            "peak":    human_size(job.speed.peak)+"/s",
            "folder":  job.user_folder,
            "rid":     job.request_id,
        })

    # ── Queue worker ───────────────────────────────────────────

    async def _worker(self):
        log.info("Queue worker started")
        while True:
            try:
                job: DownloadJob = await self._queue.get()
                self._active = job
                self._last_progress = time.time()
                job.speed.reset()
                try:
                    await self._run_job(job)
                except Exception as e:
                    log.error(f"Job error [{job.request_id}]: "
                              f"{e}\n{traceback.format_exc()}")
                    self.audit.complete(job.request_id,"failed",error_details=str(e))
                finally:
                    self._queue.task_done()
                    self._active = None
                    gc.collect()
            except asyncio.CancelledError: break
            except Exception as e:
                log.error(f"Worker loop: {e}"); await asyncio.sleep(5)

    # ── Cancel ─────────────────────────────────────────────────

    def cancel_current(self, chat_id, user_id) -> bool:
        if self._active and (self._active.chat_id==chat_id or
                             self.users.is_admin(user_id)):
            self._active.cancel.set()
            self._pending_force.pop(self._active.job_id, None)
            return True
        return False

    def cancel_all(self, admin=False, user_id=0) -> int:
        count = 0
        if self._active and (admin or self._active.user_id==user_id):
            self._active.cancel.set(); count+=1
        old=[]
        while not self._queue.empty():
            try: old.append(self._queue.get_nowait())
            except: break
        new_q = asyncio.Queue(maxsize=MAX_QUEUE)
        for job in old:
            if admin or job.user_id==user_id: count+=1
            else:
                try: new_q.put_nowait(job)
                except: pass
        self._queue = new_q
        return count

    # ── User approval ──────────────────────────────────────────

    async def request_access(self, chat_id, user_id, full_name):
        if self.users.is_pending(user_id):
            await self.notify(chat_id, "⏳ *Request already pending.*")
            return
        self.users.add_pending(user_id, full_name)
        await self.notify(chat_id,
            f"👋 Hi *{escape_md(full_name)}*!\n\n"
            f"Access request sent to admin.\nYour ID: `{user_id}`")
        await self.notify_admin(
            f"🔔 *New Access Request*\n\n"
            f"👤 *{escape_md(full_name)}*\n🆔 `{user_id}`",
            markup=kb_approve(user_id))

    async def approve_user(self, admin_chat, target_id):
        self.users.approve(target_id)
        await self.notify(admin_chat,
            f"✅ *Approved* `{target_id}`")
        try:
            await self.notify(target_id,
                "🎉 *Access Granted!*\n\nForward media or send `t.me` links.\n/start")
        except Exception: pass

    async def revoke_user(self, admin_chat, target_id):
        existed = self.users.revoke(target_id)
        await self.notify(admin_chat,
            f"{'🚫 Revoked' if existed else 'ℹ️ Not found'} `{target_id}`")

    # ── Link download ──────────────────────────────────────────

    async def download_by_link(self, chat_id, user_id, url, ptb_msg_id=0):
        channel, msg_id = parse_link(url.strip())
        if not channel:
            await self.notify(chat_id,
                "❌ *Invalid link!*\n`https://t.me/channel/123`"); return

        rid = self.audit.next_id()
        self.audit.create(rid, user_id, self.users.user_name(user_id),
                          ptb_msg_id, url, 0, "link")
        status_id = await self.send(chat_id,
            f"🔍 *Fetching...*\n🔖 `{rid}`")
        try:
            if isinstance(channel, int):
                entity = await self.resolve_entity(channel)
                if not entity:
                    await self.edit(chat_id, status_id,
                        f"❌ *Cannot access channel*\n🔖 `{rid}`")
                    self.audit.set_resolve_failed(rid,"Cannot access channel")
                    return
                msg = await self.client.get_messages(entity, ids=msg_id)
            else:
                msg = await self.client.get_messages(channel, ids=msg_id)
        except Exception as e:
            await self.edit(chat_id, status_id,
                f"❌ *Failed*\n`{e}`\n🔖 `{rid}`")
            self.audit.set_resolve_failed(rid, str(e)); return

        if not msg or not msg.media:
            await self.edit(chat_id, status_id,
                f"❌ *No media found*\n🔖 `{rid}`")
            self.audit.set_resolve_failed(rid,"No media"); return

        filename = get_filename(msg)
        filesize = get_filesize(msg)
        caption  = get_caption(msg)

        await self.classify_and_enqueue(
            chat_id, user_id, msg, filename, filesize,
            status_id, ptb_msg_id, caption, rid)

    # ── Commands ──────────────────────────────────────────────

    async def cmd_start(self, chat_id, user_id, full_name):
        if self.users.can_use(user_id):
            movies = os.path.join(MEDIA_PATH, "Movies")
            tv     = os.path.join(MEDIA_PATH, "TV Shows")
            await self.notify(chat_id,
                f"👋 *Telegram Download Bot v7.0* 🚀\n\n"
                f"*How to download:*\n"
                f"1️⃣ Forward any media message here\n"
                f"2️⃣ Send a `t.me` or `t.me/c/` link\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎬 Movies → `{movies}`\n"
                f"📺 TV Shows → `{tv}`\n\n"
                f"⚡ Streams: `{PARALLEL_CONNECTIONS}× parallel`\n"
                f"🤖 Auto-classify: `enabled`\n"
                f"💿 Free: `{human_size(disk_free())}`\n\n"
                f"/help — all commands")
        else:
            await self.request_access(chat_id, user_id, full_name)

    async def cmd_status(self, chat_id):
        active_str = "📥 *Downloading:* _None_\n"
        if self._active:
            job  = self._active
            pct  = min((job._bytes_done/job.filesize*100) if job.filesize else 0, 100.0)
            active_str = (
                f"📥 *Downloading:*\n"
                f"  📄 `{job.filename}`\n"
                f"  `{progress_bar(pct,14)}`  `{pct:.1f}%`\n"
                f"  💾 `{human_size(job._bytes_done)}` / `{human_size(job.filesize)}`\n"
                f"  🚀 `{human_size(job.speed.speed)}/s`\n"
                f"  📁 `{job.user_folder}`\n"
                f"  🔖 `{job.request_id}`\n")
        q_items = list(self._queue._queue)
        q_str   = ""
        if q_items:
            q_str = "📋 *Queued:*\n" + "\n".join(
                f"  `{i+1}.` `{j.filename}` ({human_size(j.filesize)})"
                for i,j in enumerate(q_items)) + "\n\n"
        await self.notify(chat_id,
            f"📊 *Bot Status — v7.0*\n\n{active_str}\n{q_str}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Done: `{self.stats['done']}`  "
            f"❌ Failed: `{self.stats['failed']}`  "
            f"🛑 Cancelled: `{self.stats['cancelled']}`\n\n"
            f"💿 Free: `{human_size(disk_free())}`")

    async def cmd_storage(self, chat_id):
        used_pct = (disk_used()/disk_total()*100) if disk_total() else 0
        movies_count = len(list(Path(os.path.join(MEDIA_PATH,"Movies")).rglob("*.mkv"))) + \
                       len(list(Path(os.path.join(MEDIA_PATH,"Movies")).rglob("*.mp4")))
        tv_count     = len(list(Path(os.path.join(MEDIA_PATH,"TV Shows")).rglob("*.mkv"))) + \
                       len(list(Path(os.path.join(MEDIA_PATH,"TV Shows")).rglob("*.mp4")))
        await self.notify(chat_id,
            f"💿 *Storage*\n\n"
            f"`{progress_bar(used_pct,20)}` `{used_pct:.1f}%`\n\n"
            f"✅ Free:  `{human_size(disk_free())}`\n"
            f"📦 Used:  `{human_size(disk_used())}`\n"
            f"💾 Total: `{human_size(disk_total())}`\n\n"
            f"🎬 Movies:   `{movies_count}` files\n"
            f"📺 TV Shows: `{tv_count}` files\n\n"
            f"📁 `{MEDIA_PATH}`")

    async def cmd_library(self, chat_id):
        """Show media library summary."""
        movies  = self.classifier.db._conn.execute(
            "SELECT COUNT(*) FROM movies").fetchone()[0]
        series  = self.classifier.db._conn.execute(
            "SELECT COUNT(*) FROM series").fetchone()[0]
        recent_movies = self.classifier.db._conn.execute(
            "SELECT canonical_title,year FROM movies "
            "ORDER BY added_at DESC LIMIT 5").fetchall()
        recent_tv = self.classifier.db._conn.execute(
            "SELECT canonical_title FROM series "
            "ORDER BY added_at DESC LIMIT 5").fetchall()

        movie_lines = "\n".join(
            f"  🎬 *{r[0]}* ({r[1]})" for r in recent_movies) or "  _None yet_"
        tv_lines    = "\n".join(
            f"  📺 *{r[0]}*" for r in recent_tv) or "  _None yet_"

        await self.notify(chat_id,
            f"📚 *Media Library*\n\n"
            f"🎬 Movies:   `{movies}` titles\n"
            f"📺 TV Shows: `{series}` series\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Recently added movies:*\n{movie_lines}\n\n"
            f"*Recently added TV:*\n{tv_lines}")

    async def cmd_history(self, chat_id):
        if not self._history:
            await self.notify(chat_id, "📭 *No history yet.*"); return
        icons = {"done":"✅","failed":"❌","cancelled":"🛑"}
        lines = []
        for h in list(self._history)[:20]:
            icon   = icons.get(h["outcome"],"❓")
            folder = Path(h.get("folder","")).name
            lines.append(
                f"{icon} `{h['name'][:28]}` "
                f"`{h['size']}` `{h['elapsed']}` "
                f"📁`{folder}` `{h['ts']}`")
        for chunk in split_message("📜 *Recent Downloads*\n\n" + "\n".join(lines)):
            await self.notify(chat_id, chunk)

    async def cmd_cancel(self, chat_id, user_id):
        if self.cancel_current(chat_id, user_id):
            await self.notify(chat_id, "🛑 *Cancelling...*")
        else:
            await self.notify(chat_id, "ℹ️ *No active download.*")

    async def cmd_cancelall(self, chat_id, user_id):
        n = self.cancel_all(admin=self.users.is_admin(user_id), user_id=user_id)
        await self.notify(chat_id,
            f"🛑 *Cancelled {n}*" if n else "ℹ️ *Nothing to cancel.*")

    async def cmd_addchannel(self, chat_id, user_id, args):
        if not self.users.is_admin(user_id):
            await self.notify(chat_id, "⛔ Admin only."); return
        if not args:
            await self.notify(chat_id, "Usage: `/addchannel <channel_id>`"); return
        try:
            cid = int(args[0])
        except ValueError:
            await self.notify(chat_id, "❌ Must be a number."); return
        if cid not in self._known_channels:
            self._known_channels.append(cid)
        await self.notify(chat_id,
            f"✅ Channel `{cid}` added.\nTotal: `{len(self._known_channels)}`")

    async def cmd_users(self, chat_id):
        approved = self.users.approved_list()
        pending  = self.users.pending_list()
        lines    = [f"👥 *Users* ({len(approved)} approved)\n"]
        for u in approved:
            lines.append(f"  ✅ `{u['id']}` — {escape_md(u['name'])}")
        for u in pending:
            lines.append(f"  ⏳ `{u['id']}` — {escape_md(u['name'])}")
        for chunk in split_message("\n".join(lines)):
            await self.notify(chat_id, chunk)

    async def cmd_test_metadata(self, chat_id):
        """Run provider health checks and report via Telegram (/test-metadata)."""
        await self.notify(chat_id, "🔍 *Running metadata provider checks...*")
        await self.classifier.startup_health_check()
        report = self.classifier.health_mon.build_status_report()
        await self.notify(chat_id,
            f"🩺 *Metadata Provider Status*\n\n{report}")

    async def cmd_refresh_metadata(self, chat_id, args):
        """
        /refresh-metadata [tmdb_id] [movie|tv]
        Refreshes metadata and images for a specific title.
        If called with no args, refreshes the 5 most recently added entries.
        """
        if args and len(args) >= 2:
            try:
                tmdb_id    = int(args[0])
                media_type = args[1].lower()
                if media_type not in ("movie", "tv"):
                    await self.notify(chat_id,
                        "❌ Usage: `/refresh-metadata <tmdb_id> <movie|tv>`")
                    return
                info = await self.classifier.refresh_metadata(tmdb_id, media_type)
                if info:
                    year_s = f" ({info.year})" if info.year else ""
                    await self.notify(chat_id,
                        f"✅ *Refreshed*\n"
                        f"🎯 *{info.canonical_title}{year_s}*\n"
                        f"🖼 Poster: `{'✅' if info.poster_path else '—'}`\n"
                        f"🌅 Backdrop: `{'✅' if info.backdrop_path else '—'}`")
                else:
                    await self.notify(chat_id,
                        f"❌ Refresh failed for TMDB ID `{tmdb_id}`")
                return
            except ValueError:
                await self.notify(chat_id,
                    "❌ Usage: `/refresh-metadata <tmdb_id> <movie|tv>`")
                return

        # No args — refresh recent entries
        await self.notify(chat_id, "🔄 *Refreshing recent entries...*")
        db    = self.classifier.db
        lines = []
        for row in db._conn.execute(
                "SELECT tmdb_id, 'movie' AS mt, canonical_title, year "
                "FROM movies WHERE tmdb_id > 0 ORDER BY added_at DESC LIMIT 5"
        ).fetchall():
            info = await self.classifier.refresh_metadata(row[0], "movie")
            lines.append(f"🎬 `{row[2]} ({row[3]})` {'✅' if info else '❌'}")
        for row in db._conn.execute(
                "SELECT tmdb_id, 'tv' AS mt, canonical_title "
                "FROM series WHERE tmdb_id > 0 ORDER BY added_at DESC LIMIT 5"
        ).fetchall():
            info = await self.classifier.refresh_metadata(row[0], "tv")
            lines.append(f"📺 `{row[2]}` {'✅' if info else '❌'}")
        if lines:
            await self.notify(chat_id,
                "✅ *Metadata Refresh Complete*\n\n" + "\n".join(lines))
        else:
            await self.notify(chat_id, "ℹ️ No entries with TMDB IDs found.")

    async def cmd_metrics(self, chat_id):
        """Show classifier metrics."""
        await self.notify(chat_id, self.classifier.get_metrics_report())


# ══════════════════════════════════════════════════════════════
#  GLOBAL INSTANCE
# ══════════════════════════════════════════════════════════════

dbot = DownloadBot()


# ══════════════════════════════════════════════════════════════
#  PTB HANDLERS
# ══════════════════════════════════════════════════════════════

def _uid(u: Update) -> int:
    return u.effective_user.id if u.effective_user else 0

def _uname(u: Update) -> str:
    user = u.effective_user
    if not user: return "unknown"
    return (user.full_name or user.username or str(user.id)).strip()


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    cid  = q.message.chat_id
    uid  = _uid(update)
    data = q.data or ""

    # ── Classification callbacks ───────────────────────────────
    if data.startswith("cls_movie_") or data.startswith("cls_tv_"):
        parts     = data.split("_", 2)
        cls_type  = parts[1]          # "movie" or "tv"
        rid       = parts[2]
        pend      = dbot._pending_cls.get(rid)
        if not pend:
            await q.edit_message_text("⚠️ Session expired. Forward again.",
                                      parse_mode="Markdown"); return

        dbot._pending_cls.pop(rid, None)

        icon = "🎬" if cls_type == "movie" else "📺"
        kind = "Movie" if cls_type == "movie" else "TV Series"

        # Show "fetching metadata" while we query TMDB
        await q.edit_message_text(
            f"{icon} *{kind} selected*\n\n"
            f"📄 `{pend.filename}`\n\n"
            f"🌐 *Fetching metadata...*",
            parse_mode="Markdown")

        # Fetch full metadata (TMDB + TVMaze) then build folder
        info = await dbot.classifier.classify_with_type(
            pend.filename, cls_type, rid)
        dbot.classifier.ensure_folder(info.folder_path)

        ep = ""
        if info.season and info.episodes:
            ep = f"\n🎞 `S{info.season:02d}E{'E'.join(f'{e:02d}' for e in info.episodes)}`"
        year_str  = f" ({info.year})" if info.year else ""
        src_badge = f"\n🔍 Source: `{info.source}`" if info.source != "user" else ""
        poster    = f"\n🖼 Poster: ✅" if info.poster_path else ""

        await q.edit_message_text(
            f"{icon} *{kind} — Confirmed*\n\n"
            f"📄 `{pend.filename}`\n"
            f"🎯 *{info.canonical_title}{year_str}*{ep}\n"
            f"📁 `{info.folder_path}`\n"
            f"🔖 `{rid}`"
            f"{src_badge}{poster}\n\n"
            f"⬇️ *Starting download...*",
            parse_mode="Markdown", reply_markup=kb_cancel())

        asyncio.create_task(dbot._enqueue_job(
            pend.chat_id, pend.user_id, pend.tl_msg,
            pend.filename, pend.filesize, q.message.message_id,
            info.folder_path, pend.caption, pend.ptb_msg_id, rid, info))
        return

    # ── Approval callbacks ─────────────────────────────────────
    if data.startswith("approve_"):
        if not dbot.users.is_admin(uid): return
        await dbot.approve_user(cid, int(data.split("_",1)[1]))
        await q.edit_message_text("✅ *Approved.*", parse_mode="Markdown"); return

    if data.startswith("deny_"):
        if not dbot.users.is_admin(uid): return
        dbot.users.deny(int(data.split("_",1)[1]))
        await q.edit_message_text("❌ *Denied.*", parse_mode="Markdown"); return

    if data.startswith("force_"):
        job_id = data.split("_",1)[1]
        entry  = dbot._pending_force.get(job_id)
        if not entry:
            await dbot.notify(cid, "⚠️ Expired. Forward again."); return
        original, _ = entry
        status_id   = await dbot.send(cid, f"🔁 *Re-queuing...*\n📄 `{original.filename}`")
        rid         = dbot.audit.next_id()
        dbot.audit.create(rid, uid, dbot.users.user_name(uid),
                          original.ptb_msg_id, original.filename,
                          original.filesize, "redownload")
        dbot._force_ids.add(original.job_id)
        dbot._pending_force.pop(job_id, None)
        await dbot._enqueue_job(
            cid, uid, original.tl_msg, original.filename,
            original.filesize, status_id, original.user_folder,
            original.caption, original.ptb_msg_id, rid, original.media_info)
        return

    if not dbot.users.can_use(uid): return

    if   data == "cancel":  dbot.cancel_current(cid, uid)
    elif data == "status":  await dbot.cmd_status(cid)
    elif data == "queue":   await dbot.cmd_status(cid)
    elif data == "storage": await dbot.cmd_storage(cid)


async def on_forwarded(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    uid  = _uid(update); cid = msg.chat_id; name = _uname(update)

    if not dbot.users.can_use(uid):
        await dbot.request_access(cid, uid, name); return

    if not (msg.video or msg.document or msg.audio or msg.photo):
        await msg.reply_text("⚠️ No downloadable media."); return

    ptb_filename = f"media_{msg.message_id}"
    ptb_filesize = 0; ptb_type = "unknown"
    if msg.document:
        ptb_filename = msg.document.file_name or f"file_{msg.message_id}.bin"
        ptb_filesize = msg.document.file_size or 0; ptb_type = "document"
    elif msg.video:
        ptb_filename = getattr(msg.video,"file_name",None) or f"video_{msg.message_id}.mp4"
        ptb_filesize = msg.video.file_size or 0; ptb_type = "video"
    elif msg.audio:
        ptb_filename = getattr(msg.audio,"file_name",None) or f"audio_{msg.message_id}.mp3"
        ptb_filesize = msg.audio.file_size or 0; ptb_type = "audio"
    elif msg.photo:
        ptb_filename = f"photo_{msg.message_id}.jpg"; ptb_type = "photo"

    rid = dbot.audit.next_id()
    dbot.audit.create(rid, uid, name, msg.message_id,
                      ptb_filename, ptb_filesize, ptb_type)

    status_id = await dbot.send(cid,
        f"🔍 *Resolving...*\n📄 `{ptb_filename}`\n"
        f"💾 `{human_size(ptb_filesize)}`\n🔖 `{rid}`")

    tl_msg = await dbot.get_telethon_message(
        msg, ptb_filesize=ptb_filesize, ptb_type=ptb_type,
        ptb_filename=ptb_filename, request_id=rid)

    if not tl_msg or not tl_msg.media:
        dbot.audit.set_resolve_failed(rid, "All strategies failed")
        await dbot.edit(cid, status_id,
            f"❌ *Could not access file*\n\n"
            f"Forward to *Saved Messages* first, then forward here.\n\n"
            f"Or paste the `t.me/c/...` link directly.\n🔖 `{rid}`")
        return

    filename = get_filename(tl_msg)
    filesize = get_filesize(tl_msg) or ptb_filesize
    caption  = get_caption(tl_msg) or (msg.caption or "").strip()

    asyncio.create_task(dbot.classify_and_enqueue(
        cid, uid, tl_msg, filename, filesize,
        status_id, msg.message_id, caption, rid))


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text: return
    uid  = _uid(update); cid = msg.chat_id; name = _uname(update)
    text = msg.text.strip()
    cmd  = text.split()[0].lower().split("@")[0]
    args = text.split()[1:]

    if cmd == "/start":
        await dbot.cmd_start(cid, uid, name); return

    if not dbot.users.can_use(uid):
        await dbot.request_access(cid, uid, name); return

    if   cmd == "/help":
        await dbot.notify(cid,
            f"📖 *Commands — v8.0*\n\n"
            f"/start  /status  /queue\n"
            f"/cancel  /cancelall\n"
            f"/history  /storage\n"
            f"/library  — media library\n"
            f"/metrics  — classifier metrics\n"
            f"/test-metadata  — check provider health\n"
            f"/refresh-metadata [tmdb\\_id] [movie|tv]\n"
            f"/addchannel  /users\n\n"
            f"*Forward* media or send a `t.me` link to download.")
    elif cmd == "/status":     await dbot.cmd_status(cid)
    elif cmd == "/queue":      await dbot.cmd_status(cid)
    elif cmd == "/storage":    await dbot.cmd_storage(cid)
    elif cmd == "/history":    await dbot.cmd_history(cid)
    elif cmd == "/library":    await dbot.cmd_library(cid)
    elif cmd == "/cancel":     await dbot.cmd_cancel(cid, uid)
    elif cmd == "/cancelall":  await dbot.cmd_cancelall(cid, uid)
    elif cmd == "/metrics":    await dbot.cmd_metrics(cid)
    elif cmd in ("/test-metadata", "/testmetadata"):
        await dbot.cmd_test_metadata(cid)
    elif cmd in ("/refresh-metadata", "/refreshmetadata"):
        await dbot.cmd_refresh_metadata(cid, args)
    elif cmd == "/addchannel":
        if dbot.users.is_admin(uid): await dbot.cmd_addchannel(cid, uid, args)
    elif cmd == "/users":
        if dbot.users.is_admin(uid): await dbot.cmd_users(cid)
    elif "t.me/" in text:
        await msg.reply_text("✅ *Link received!*", parse_mode="Markdown")
        asyncio.create_task(
            dbot.download_by_link(cid, uid, text, ptb_msg_id=msg.message_id))
    else:
        await msg.reply_text(
            "📎 *Forward* media or send a `t.me` link!\n\n"
            "/help  /status  /storage  /library",
            parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN" or API_ID == 0:
        print("\n❌  Set BOT_TOKEN, API_ID, API_HASH in the script!\n")
        sys.exit(1)

    log.info("═"*60)
    log.info("  Telegram Download Bot v8.0 — Radarr/Sonarr Style")
    log.info("═"*60)

    # ── VPN ───────────────────────────────────────────────────────
    vpn = VpnManager()
    if vpn.is_enabled():
        log.info("[VPN] Enabled — connecting before Telegram…")
        try:
            connected = await vpn.start()
            if not connected:
                log.error("[VPN] Could not establish VPN connection. Aborting.")
                sys.exit(1)
        except RuntimeError as exc:
            log.error(str(exc))
            sys.exit(1)
    else:
        log.info("[VPN] Disabled.")

    await dbot.client.start()
    me = await dbot.client.get_me()
    log.info(f"[Telegram] ✅ Connected Successfully (user={me.first_name} id={me.id})")
    log.info(f"📁 Media path: {MEDIA_PATH}")
    log.info(f"🎬 Movies: {os.path.join(MEDIA_PATH, MOVIES_FOLDER)}")
    log.info(f"📺 TV:     {os.path.join(MEDIA_PATH, TV_FOLDER)}")
    log.info(f"💿 Free: {human_size(disk_free())}")

    # ── Startup API health validation ─────────────────────────
    health = await dbot.classifier.startup_health_check(
        media_path=MEDIA_PATH, bot_token=BOT_TOKEN)
    # Mark Telegram as healthy since we just connected
    dbot.classifier.health_mon.telegram_health.available = True

    dbot._worker_task = asyncio.create_task(dbot._worker())

    if YOUR_USER_ID:
        try:
            tmdb_ok  = "✅" if health.get("tmdb")     else "❌"
            db_ok    = "✅" if health.get("database") else "❌"
            stor_ok  = "✅" if health.get("storage")  else "❌"
            auto_cls = "✅ enabled" if dbot.classifier.health_mon.auto_classify_enabled \
                       else "⚠️ DISABLED (manual approval mode)"
            vpn_ok   = "🔒 " + vpn.status_line() if vpn.is_enabled() else "🔓 disabled"
            await dbot.bot.send_message(YOUR_USER_ID,
                f"🟢 *Bot v8.0 Online!*\n\n"
                f"⚡ Streams: `{PARALLEL_CONNECTIONS}×`\n"
                f"🎬 Movies: `{os.path.join(MEDIA_PATH, MOVIES_FOLDER)}`\n"
                f"📺 TV:     `{os.path.join(MEDIA_PATH, TV_FOLDER)}`\n"
                f"💿 Free: `{human_size(disk_free())}`\n\n"
                f"🩺 *Health:*\n"
                f"  TMDB:     {tmdb_ok}\n"
                f"  Database: {db_ok}\n"
                f"  Storage:  {stor_ok}\n\n"
                f"🌐 VPN:     {vpn_ok}\n"
                f"🤖 Auto-classify: `{auto_cls}`\n"
                f"🕐 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
                parse_mode="Markdown")
        except Exception: pass

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.VIDEO | filters.Document.ALL |
                             filters.AUDIO | filters.PHOTO),
        on_forwarded))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.COMMAND, on_message))

    dbot._app = app
    await app.initialize(); await app.start()
    await app.updater.start_polling(drop_pending_updates=True, timeout=30)
    log.info("🤖 Bot v7.0 running!")

    stop_event = asyncio.Event()
    loop       = asyncio.get_running_loop()
    def _stop(): stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, _stop)
        except NotImplementedError: pass

    await stop_event.wait()
    log.info("Shutting down...")
    dbot.cancel_all(admin=True)
    if dbot._worker_task:
        dbot._worker_task.cancel()
        try: await dbot._worker_task
        except asyncio.CancelledError: pass

    if YOUR_USER_ID:
        try:
            await dbot.bot.send_message(YOUR_USER_ID,
                f"🔴 *Bot v7.0 Stopped*\n\n"
                f"✅ Done: `{dbot.stats['done']}`  "
                f"❌ Failed: `{dbot.stats['failed']}`\n"
                f"💾 Total: `{human_size(dbot.stats['bytes'])}`",
                parse_mode="Markdown")
        except Exception: pass

    await dbot.classifier.close()
    dbot.audit.close()
    await app.updater.stop(); await app.stop(); await app.shutdown()
    await dbot.client.disconnect()
    await vpn.stop()
    log.info("✅ Shutdown complete")


def _parse_vpn_flag() -> None:
    """
    Check for --vpn=true / --vpn=false / --vpn=1 / --vpn=0 in sys.argv
    and inject the result into the environment so VpnManager picks it up.
    The flag overrides whatever VPN_ENABLED is set to in .env.
    """
    for arg in sys.argv[1:]:
        if arg.startswith("--vpn="):
            val = arg.split("=", 1)[1].lower()
            if val in ("true", "1", "yes"):
                os.environ["VPN_ENABLED"] = "true"
                log.info("[CLI] --vpn=true → VPN forced ON")
            elif val in ("false", "0", "no"):
                os.environ["VPN_ENABLED"] = "false"
                log.info("[CLI] --vpn=false → VPN forced OFF")
            else:
                print(f"\n❌  Unknown --vpn value: '{val}'. Use --vpn=true or --vpn=false\n")
                sys.exit(1)
            break


if __name__ == "__main__":
    _parse_vpn_flag()
    asyncio.run(main())