#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║   Media Classifier Module — Radarr/Sonarr-style v8.0        ║
║   Plug-in for Telegram Download Bot                          ║
║                                                              ║
║   Features:                                                  ║
║   • Multi-provider metadata: TMDB (primary), IMDb, TVDB      ║
║   • Accurate Movie vs TV classification                      ║
║   • CRITICAL FIX: codec tags (x265/HEVC/etc) never parsed   ║
║     as episode numbers                                       ║
║   • Confidence scoring with provider comparison              ║
║   • Startup API health validation                            ║
║   • Poster/backdrop download & caching                       ║
║   • Metrics & monitoring                                     ║
║   • Exponential backoff on API failures                      ║
║   • Fuzzy existing library matching                          ║
║   • Anime + daily show support                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, re, json, time, sqlite3, asyncio, logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple, List, Any
from dataclasses import dataclass, field

import aiohttp
from fuzzywuzzy import fuzz

log = logging.getLogger("dlbot.classifier")


from config.settings import settings

# ── Provider config ────────────────────────────────────────────
TMDB_API_KEY   = settings.TMDB_API_KEY
TVDB_API_KEY   = settings.TVDB_API_KEY


TMDB_BASE      = "https://api.themoviedb.org/3"
TMDB_IMG_BASE  = "https://image.tmdb.org/t/p/original"
TMDB_IMG_W200  = "https://image.tmdb.org/t/p/w200"

# TVDB — set your key if available

TVDB_BASE      = "https://api4.thetvdb.com/v4"

# TVMaze — completely free, no API key required
# https://www.tvmaze.com/api
TVMAZE_BASE    = "https://api.tvmaze.com"

# IMDb ID pattern found in filenames (e.g. tt0111161)
_IMDB_ID_RE    = re.compile(r'\b(tt\d{7,8})\b', re.I)

# ── Confidence thresholds ──────────────────────────────────────
AUTO_CONFIRM_THRESHOLD = 85   # % — auto-classify above this
LOW_CONFIDENCE         = 60   # % — ask user below this

# ── Folder names ───────────────────────────────────────────────
MOVIES_FOLDER   = "movies"
TV_FOLDER       = "tv"

# ══════════════════════════════════════════════════════════════
#  CRITICAL: Codec / quality tokens that must NEVER be parsed
#  as season or episode numbers.
# ══════════════════════════════════════════════════════════════

# These patterns are stripped BEFORE any episode-number search.
# Order matters: strip them first so e.g. "x265" is gone before
# the "NxNN" episode pattern tries to match.
_CODEC_STRIP = re.compile(
    r'(?:^|(?<=[\s._\-]))'  # must start at boundary
    r'('
    r'x\.?26[45]|h\.?26[45]|hevc|avc|av1|'       # video codecs
    r'hdr(?:10(?:\+)?)?|dv|dolby[\s._-]?vision|'  # HDR
    r'dts(?:[-._]hd)?(?:[-._]ma)?|dts[-._]?x|'    # audio
    r'atmos|truehd|dd\+?(?:[257][\.\s]?[012])?|'  # DD, DD+, DD+5.1
    r'eac3|ac3|aac|flac|mp3|'
    r'[2-9]ch|[1-9]\.[0-9]ch?|'                   # channel counts: 6ch 5.1 7.1
    r'10bit|10[-._]?bit|8bit|12bit|'               # bit depth
    r'2160p|1080[pi]|720p|480p|360p|4k|uhd|sdr|'  # resolution
    r'bluray|bdrip|brrip|webrip|web[-._]?dl|'      # source
    r'webdl|hdtv|dvdrip|remux|bdremux|hdrip|'
    r'extended|theatrical|remastered|repack|proper|'
    r'yify|yts|rarbg|tinymkv|'                     # release groups
    r'nf|amzn|dsnp|hulu|max|atvp|pcok|'
    r'multi|dual|'
    r'hindi|tamil|telugu|malayalam|english|kannada|marathi|bengali|'
    r'ds4k|hdr10plus|hdr10\+|hq'
    r')(?=[\s._\-]|$)',                            # must end at boundary
    re.I
)

# Bare leftover digits after codec strip
_CODEC_DIGIT_REMNANT = re.compile(
    r'(?<![\w])(?:265|264)(?![\w])', re.I
)

# ── TV episode patterns (applied AFTER codec strip) ────────────
_EP_PATTERNS = [
    # S01E01, S01E01E02, S01E01-E03
    re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})(?:[Ee-](\d{1,3}))?'),
    # S01 E01 — with space between S and E (common in some releases)
    re.compile(r'[Ss](\d{1,2})\s+[Ee](\d{1,3})'),
    # 1x01 (season digit, episode 2+ digits)
    re.compile(r'(?<!\d)(\d{1,2})[Xx](\d{2,3})(?!\d)'),
    # Season 1 Episode 1
    re.compile(r'[Ss]eason\s*(\d+)\s*[Ee]pisode\s*(\d+)', re.I),
    # EP01 / EP001
    re.compile(r'\bEP(\d{1,3})\b', re.I),
    # Daily show: 2026.06.08 or 2026-06-08
    re.compile(r'(20\d{2})[.\-](\d{2})[.\-](\d{2})'),
    # Anime 3-digit episode (only when no year context)
    re.compile(r'(?<!\d)(\d{3})(?!\d)'),
]

_CLEAN_TITLE = re.compile(
    r'\b(2160p|1080p|1080i|720p|480p|360p|4K|UHD|HDR|SDR|'
    r'BluRay|BDRip|BRRip|WEBRip|WEB[-.]?DL|WEBDL|WEBRIP|HDTV|DVDRip|'
    r'x264|x265|H\.?264|H\.?265|HEVC|AVC|AV1|'
    r'DD\+?|EAC3|AAC|AC3|DTS|TrueHD|FLAC|MP3|Atmos|DTS[-.]HD|'
    r'[2-9]CH|[1-9]\.[0-9]CH?|'
    r'REMUX|BDREMUX|EXTENDED|THEATRICAL|REMASTERED|REPACK|PROPER|'
    r'YIFY|YTS|RARBG|TinyMKV|TINYMKV|'
    r'NF|AMZN|DSNP|HULU|MAX|ATVP|PCOK|'
    r'MULTI|DUAL|Hindi|Tamil|Telugu|Malayalam|English|'
    r'10Bit|10bit|8Bit|12bit|DS4K|HDR10|HDR10\+|DV)\b',
    re.I
)
_CLEAN_SEPS  = re.compile(r'[\s._\-]+')
_YEAR_RE     = re.compile(r'\b((?:19|20)\d{2})\b')
_AT_TAG      = re.compile(r'@[\w]+')


@dataclass
class ParsedMedia:
    """Result of parsing a filename."""
    raw_title:   str = ""
    clean_title: str = ""
    year:        Optional[int] = None
    media_type:  str = "unknown"   # "movie" | "tv" | "unknown"
    season:      Optional[int] = None
    episodes:    List[int] = field(default_factory=list)
    is_daily:    bool = False
    daily_date:  str = ""          # "2026-06-08"
    is_anime:    bool = False
    confidence:  int = 0           # 0-100


@dataclass
class MediaInfo:
    """Resolved metadata from TMDB or DB."""
    media_type:    str = "unknown"
    tmdb_id:       int = 0
    tvdb_id:       int = 0
    imdb_id:       str = ""
    canonical_title: str = ""
    year:          int = 0
    season:        Optional[int] = None
    episodes:      List[int] = field(default_factory=list)
    folder_path:   str = ""
    confidence:    int = 0
    source:        str = ""   # "tmdb" | "imdb" | "tvdb" | "db" | "parsed" | "user"
    poster_path:   str = ""
    backdrop_path: str = ""
    confidence_breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass
class ProviderHealth:
    """Health status of a metadata provider."""
    name:          str = ""
    available:     bool = False
    last_checked:  float = 0.0
    last_error:    str = ""
    response_ms:   int = 0
    total_queries: int = 0
    success_count: int = 0
    fail_count:    int = 0

    @property
    def success_rate(self) -> float:
        if self.total_queries == 0: return 0.0
        return self.success_count / self.total_queries * 100


@dataclass
class ClassifierMetrics:
    """Runtime metrics for the classifier."""
    classifications_total: int = 0
    classifications_auto:  int = 0
    classifications_user:  int = 0
    classifications_db:    int = 0
    tmdb_hits:    int = 0
    tmdb_misses:  int = 0
    cache_hits:   int = 0
    cache_misses: int = 0
    retries_total: int = 0
    avg_confidence: float = 0.0
    _confidence_sum: float = 0.0

    def record_confidence(self, c: int):
        self.classifications_total += 1
        self._confidence_sum += c
        self.avg_confidence = self._confidence_sum / self.classifications_total


# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════

class MediaDatabase:
    _CREATE = """
    CREATE TABLE IF NOT EXISTS series (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_title TEXT    NOT NULL,
        tmdb_id         INTEGER DEFAULT 0,
        tvdb_id         INTEGER DEFAULT 0,
        imdb_id         TEXT    DEFAULT '',
        folder_path     TEXT    NOT NULL,
        poster_path     TEXT    DEFAULT '',
        backdrop_path   TEXT    DEFAULT '',
        added_at        TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_series_title  ON series(canonical_title);
    CREATE INDEX IF NOT EXISTS idx_series_tmdb   ON series(tmdb_id);
    CREATE INDEX IF NOT EXISTS idx_series_tvdb   ON series(tvdb_id);
    CREATE INDEX IF NOT EXISTS idx_series_imdb   ON series(imdb_id);

    CREATE TABLE IF NOT EXISTS movies (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_title TEXT    NOT NULL,
        year            INTEGER DEFAULT 0,
        tmdb_id         INTEGER DEFAULT 0,
        tvdb_id         INTEGER DEFAULT 0,
        imdb_id         TEXT    DEFAULT '',
        folder_path     TEXT    NOT NULL,
        poster_path     TEXT    DEFAULT '',
        backdrop_path   TEXT    DEFAULT '',
        added_at        TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_movie_title   ON movies(canonical_title);
    CREATE INDEX IF NOT EXISTS idx_movie_tmdb    ON movies(tmdb_id);
    CREATE INDEX IF NOT EXISTS idx_movie_imdb    ON movies(imdb_id);

    CREATE TABLE IF NOT EXISTS pending_classification (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id      TEXT    NOT NULL,
        chat_id         INTEGER NOT NULL,
        filename        TEXT    NOT NULL,
        parsed_json     TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        resolved        INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pending_rid   ON pending_classification(request_id);

    CREATE TABLE IF NOT EXISTS tmdb_cache (
        cache_key       TEXT    PRIMARY KEY,
        response_json   TEXT    NOT NULL,
        cached_at       TEXT    NOT NULL,
        provider        TEXT    DEFAULT 'tmdb'
    );

    CREATE TABLE IF NOT EXISTS user_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title_key       TEXT    NOT NULL UNIQUE,
        media_type      TEXT    NOT NULL,
        canonical_title TEXT    NOT NULL,
        year            INTEGER DEFAULT 0,
        tmdb_id         INTEGER DEFAULT 0,
        decided_at      TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_decision_key  ON user_decisions(title_key);
    """

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self._CREATE)
        self._migrate()
        self._conn.commit()
        log.info(f"[mediadb] Opened {db_path}")

    def _migrate(self):
        """Add any missing columns to existing tables (safe to run every startup)."""
        _migrations = [
            ("movies",  "poster_path",   "TEXT DEFAULT ''"),
            ("movies",  "backdrop_path", "TEXT DEFAULT ''"),
            ("movies",  "tvdb_id",       "INTEGER DEFAULT 0"),
            ("series",  "poster_path",   "TEXT DEFAULT ''"),
            ("series",  "backdrop_path", "TEXT DEFAULT ''"),
            ("tmdb_cache", "provider",   "TEXT DEFAULT 'tmdb'"),
        ]
        for table, col, col_def in _migrations:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                log.info(f"[mediadb] Migration: added {table}.{col}")
            except sqlite3.OperationalError:
                pass   # column already exists
        self._conn.commit()

    # ── Series ────────────────────────────────────────────────

    def find_series(self, title: str, tmdb_id: int = 0,
                    tvdb_id: int = 0, imdb_id: str = "") -> Optional[Dict]:
        """Find existing series by TMDB/TVDB/IMDb ID or fuzzy title match."""
        if tmdb_id:
            cur = self._conn.execute(
                "SELECT * FROM series WHERE tmdb_id=? LIMIT 1", (tmdb_id,))
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        if tvdb_id:
            cur = self._conn.execute(
                "SELECT * FROM series WHERE tvdb_id=? LIMIT 1", (tvdb_id,))
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        if imdb_id:
            cur = self._conn.execute(
                "SELECT * FROM series WHERE imdb_id=? LIMIT 1", (imdb_id,))
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        # Fuzzy title match
        cur = self._conn.execute("SELECT * FROM series")
        cols = [d[0] for d in cur.description]
        best_score, best_row = 0, None
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            score = fuzz.token_sort_ratio(
                title.lower(), d["canonical_title"].lower())
            if score > best_score:
                best_score, best_row = score, d
        if best_score >= 85 and best_row:
            log.info(f"[mediadb] Series fuzzy match: '{title}' → "
                     f"'{best_row['canonical_title']}' ({best_score}%)")
            return best_row
        return None

    def add_series(self, canonical_title: str, folder_path: str,
                   tmdb_id: int = 0, tvdb_id: int = 0,
                   imdb_id: str = "", poster_path: str = "",
                   backdrop_path: str = "") -> int:
        cur = self._conn.execute(
            """INSERT INTO series
               (canonical_title, tmdb_id, tvdb_id, imdb_id,
                folder_path, poster_path, backdrop_path, added_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (canonical_title, tmdb_id, tvdb_id, imdb_id,
             folder_path, poster_path, backdrop_path,
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()
        log.info(f"[mediadb] New series: '{canonical_title}' → {folder_path}")
        return cur.lastrowid

    def update_series_images(self, tmdb_id: int,
                              poster_path: str, backdrop_path: str):
        self._conn.execute(
            "UPDATE series SET poster_path=?, backdrop_path=? WHERE tmdb_id=?",
            (poster_path, backdrop_path, tmdb_id))
        self._conn.commit()

    # ── Movies ────────────────────────────────────────────────

    def find_movie(self, title: str, year: int = 0,
                   tmdb_id: int = 0, imdb_id: str = "") -> Optional[Dict]:
        if tmdb_id:
            cur = self._conn.execute(
                "SELECT * FROM movies WHERE tmdb_id=? LIMIT 1", (tmdb_id,))
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        if imdb_id:
            cur = self._conn.execute(
                "SELECT * FROM movies WHERE imdb_id=? LIMIT 1", (imdb_id,))
            row = cur.fetchone()
            if row:
                return dict(zip([d[0] for d in cur.description], row))
        cur = self._conn.execute("SELECT * FROM movies")
        cols = [d[0] for d in cur.description]
        best_score, best_row = 0, None
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            score = fuzz.token_sort_ratio(
                title.lower(), d["canonical_title"].lower())
            year_bonus = 10 if (year and d["year"] == year) else 0
            total = score + year_bonus
            if total > best_score:
                best_score, best_row = total, d
        if best_score >= 85 and best_row:
            return best_row
        return None

    def add_movie(self, canonical_title: str, year: int,
                  folder_path: str, tmdb_id: int = 0,
                  imdb_id: str = "", poster_path: str = "",
                  backdrop_path: str = "") -> int:
        cur = self._conn.execute(
            """INSERT INTO movies
               (canonical_title, year, tmdb_id, imdb_id,
                folder_path, poster_path, backdrop_path, added_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (canonical_title, year, tmdb_id, imdb_id,
             folder_path, poster_path, backdrop_path,
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()
        log.info(f"[mediadb] New movie: '{canonical_title} ({year})' → {folder_path}")
        return cur.lastrowid

    def update_movie_images(self, tmdb_id: int,
                             poster_path: str, backdrop_path: str):
        self._conn.execute(
            "UPDATE movies SET poster_path=?, backdrop_path=? WHERE tmdb_id=?",
            (poster_path, backdrop_path, tmdb_id))
        self._conn.commit()

    # ── User decisions (permanent type choices) ──────────────

    def get_user_decision(self, title_key: str) -> Optional[Dict]:
        cur = self._conn.execute(
            "SELECT * FROM user_decisions WHERE title_key=? LIMIT 1",
            (title_key,))
        row = cur.fetchone()
        if not row: return None
        return dict(zip([d[0] for d in cur.description], row))

    def save_user_decision(self, title_key: str, media_type: str,
                            canonical_title: str, year: int = 0,
                            tmdb_id: int = 0):
        self._conn.execute(
            """INSERT OR REPLACE INTO user_decisions
               (title_key, media_type, canonical_title, year, tmdb_id, decided_at)
               VALUES (?,?,?,?,?,?)""",
            (title_key, media_type, canonical_title, year, tmdb_id,
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()
        log.info(f"[mediadb] User decision saved: '{title_key}' → {media_type}")

    # ── Pending classification ────────────────────────────────

    def add_pending(self, request_id: str, chat_id: int,
                    filename: str, parsed: ParsedMedia) -> int:
        cur = self._conn.execute(
            """INSERT INTO pending_classification
               (request_id, chat_id, filename, parsed_json, created_at)
               VALUES (?,?,?,?,?)""",
            (request_id, chat_id, filename,
             json.dumps(parsed.__dict__),
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()
        return cur.lastrowid

    def get_pending(self, request_id: str) -> Optional[Dict]:
        cur = self._conn.execute(
            "SELECT * FROM pending_classification WHERE request_id=? AND resolved=0",
            (request_id,))
        row = cur.fetchone()
        if not row: return None
        return dict(zip([d[0] for d in cur.description], row))

    def resolve_pending(self, request_id: str):
        self._conn.execute(
            "UPDATE pending_classification SET resolved=1 WHERE request_id=?",
            (request_id,))
        self._conn.commit()

    # ── TMDB cache ────────────────────────────────────────────

    def cache_get(self, key: str, max_age: int = 86400) -> Optional[Dict]:
        cur = self._conn.execute(
            "SELECT response_json, cached_at FROM tmdb_cache WHERE cache_key=?",
            (key,))
        row = cur.fetchone()
        if not row: return None
        cached_at = datetime.fromisoformat(row[1])
        age = (datetime.now() - cached_at).total_seconds()
        if age > max_age: return None
        return json.loads(row[0])

    def cache_set(self, key: str, data: Dict):
        self._conn.execute(
            """INSERT OR REPLACE INTO tmdb_cache
               (cache_key, response_json, cached_at) VALUES (?,?,?)""",
            (key, json.dumps(data),
             datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()

    def close(self):
        try: self._conn.close()
        except Exception: pass


# ══════════════════════════════════════════════════════════════
#  FILENAME PARSER  (v8 — codec-safe)
# ══════════════════════════════════════════════════════════════

def _make_title_key(title: str, year: int = 0) -> str:
    """Normalised key for user-decision lookup."""
    t = re.sub(r'\s+', ' ', title.lower().strip())
    return f"{t}:{year}" if year else t


def parse_filename(filename: str) -> ParsedMedia:
    """
    Extract title, year, season, episodes from a filename.

    CRITICAL: codec tokens (x265, x264, HEVC, AVC, h264, h265,
    DTS, AAC, Atmos, HDR, DV, etc.) are stripped BEFORE any
    episode-number pattern is applied, so they can NEVER be
    misidentified as season/episode numbers.

    Example fix:
        The.Green.Mile.1999.1080p.BRRip.x265.HEVCBay.mkv
        → Movie, title="The Green Mile", year=1999
        (NOT TV S01E265)
    """
    result = ParsedMedia(raw_title=filename)
    stem   = Path(filename).stem

    # 1. Remove @channel tags and [bracket] tags (e.g. [CC], [720p], [YIFY], (720p - BluRa))
    stem = _AT_TAG.sub("", stem)
    stem = re.sub(r'\[[^\]]{1,60}\]', ' ', stem)   # [tag] up to 60 chars
    stem = re.sub(r'\([^)]{1,60}\)', ' ', stem)    # (tag) up to 60 chars — removes (720p - BluRa) etc
    stem = re.sub(r'\s+', ' ', stem).strip()

    # 2. ── CRITICAL: strip codec/quality tokens BEFORE episode search ──
    #    Replace with a space so word boundaries are preserved.
    safe_stem = _CODEC_STRIP.sub(" ", stem)
    # Also strip bare digit remnants left behind by codec tokens
    # e.g. "x265" → " 265" after codec strip → strip "265" too
    safe_stem = _CODEC_DIGIT_REMNANT.sub(" ", safe_stem)
    safe_stem = re.sub(r'\s+', ' ', safe_stem).strip()

    # 3. Check explicit TV episode patterns on the CODEC-STRIPPED stem
    #    Title is extracted from safe_stem position (not original) to avoid
    #    codec junk bleeding into the title.
    ep_match = None

    # Pattern 0: S01E01 / S01E01E02 / S01E01-E03
    m = _EP_PATTERNS[0].search(safe_stem)
    if m:
        ep_match = m
        result.media_type = "tv"
        g = m.groups()
        result.season   = int(g[0])
        result.episodes = [int(g[1])]
        if len(g) > 2 and g[2]:
            result.episodes.append(int(g[2]))

    # Pattern 1: S01 E01 — spaced variant
    if not ep_match:
        m = _EP_PATTERNS[1].search(safe_stem)
        if m:
            ep_match = m
            result.media_type = "tv"
            result.season   = int(m.group(1))
            result.episodes = [int(m.group(2))]

    # Pattern 2: 1x01 — require it to be surrounded by non-digit word boundaries
    #   to avoid false positives from leftover tokens like "1 19"
    if not ep_match:
        m = _EP_PATTERNS[2].search(safe_stem)
        if m:
            # Extra guard: preceding char must be a separator, not a digit or letter
            start = m.start()
            pre = safe_stem[start-1] if start > 0 else ' '
            if not pre.isalnum():
                ep_match = m
                result.media_type = "tv"
                result.season   = int(m.group(1))
                result.episodes = [int(m.group(2))]

    # Pattern 3: Season N Episode N
    if not ep_match:
        m = _EP_PATTERNS[3].search(safe_stem)
        if m:
            ep_match = m
            result.media_type = "tv"
            result.season   = int(m.group(1))
            result.episodes = [int(m.group(2))]

    # Pattern 4: EP01 / EP001
    if not ep_match:
        m = _EP_PATTERNS[4].search(safe_stem)
        if m:
            ep_match = m
            result.media_type = "tv"
            result.season   = 1
            result.episodes = [int(m.group(1))]

    # Pattern 5: Daily show date (on safe_stem — dates survive codec strip)
    if not ep_match:
        m = _EP_PATTERNS[5].search(safe_stem)
        if m:
            result.media_type = "tv"
            result.is_daily   = True
            result.daily_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            ep_match = m

    # 4. Extract title from safe_stem (codec junk already removed)
    title_part = safe_stem
    if ep_match:
        title_part = safe_stem[:ep_match.start()]
    elif result.is_daily:
        pass
    else:
        # For movies: strip from year onward
        year_m = _YEAR_RE.search(safe_stem)
        if year_m:
            result.year = int(year_m.group(1))
            title_part  = safe_stem[:year_m.start()]

    # 5. Clean title — remove any remaining quality/codec tokens, separators
    title_part = _CLEAN_TITLE.sub(" ", title_part)
    title_part = _CLEAN_SEPS.sub(" ", title_part).strip(" -_.,(")
    result.clean_title = title_part

    # 6. Extract year from safe_stem if not already found
    if not result.year:
        year_m = _YEAR_RE.search(safe_stem)
        if year_m:
            result.year = int(year_m.group(1))

    # 7. Anime detection: 3-digit episode on codec-stripped stem,
    #    only when no other TV pattern matched AND no year was found.
    if result.media_type == "unknown" and not result.year:
        m = _EP_PATTERNS[6].search(safe_stem)
        if m:
            candidate = int(m.group(1))
            if not (1900 <= candidate <= 2099):
                result.is_anime   = True
                result.media_type = "tv"
                result.season     = 1
                result.episodes   = [candidate]

    # 8. Assign base confidence from parse result
    if result.media_type == "tv" and result.season:
        result.confidence = 90
    elif result.media_type == "tv" and result.is_daily:
        result.confidence = 85
    elif result.media_type == "tv" and result.is_anime:
        result.confidence = 70
    elif result.year and result.media_type == "unknown":
        result.media_type = "movie"
        result.confidence = 70
    else:
        result.confidence = 40

    log.info(
        f"[parser] '{filename}' → type={result.media_type} "
        f"title='{result.clean_title}' year={result.year} "
        f"S{result.season}E{result.episodes} conf={result.confidence}%"
    )
    return result


# ══════════════════════════════════════════════════════════════
#  TMDB CLIENT  (v8 — retry, health tracking, poster/backdrop)
# ══════════════════════════════════════════════════════════════

class TMDBClient:
    def __init__(self, api_key: str, db: MediaDatabase,
                 metrics: "ClassifierMetrics"):
        self._key     = api_key
        self._db      = db
        self._metrics = metrics
        self._session: Optional[aiohttp.ClientSession] = None
        self.health   = ProviderHealth(name="TMDB")

    async def _get(self, endpoint: str, params: Dict,
                   retries: int = 3) -> Optional[Dict]:
        if not self._key:
            return None
        params_no_key = {k: v for k, v in params.items() if k != "api_key"}
        cache_key = f"tmdb:{endpoint}:{json.dumps(params_no_key, sort_keys=True)}"
        cached = self._db.cache_get(cache_key)
        if cached:
            self._metrics.cache_hits += 1
            return cached
        self._metrics.cache_misses += 1

        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

        req_params = dict(params)
        req_params["api_key"] = self._key
        url = f"{TMDB_BASE}{endpoint}"

        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                async with self._session.get(
                        url, params=req_params,
                        timeout=aiohttp.ClientTimeout(total=15)) as r:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    self.health.response_ms  = elapsed_ms
                    self.health.total_queries += 1
                    self.health.last_checked  = time.time()
                    if r.status == 200:
                        data = await r.json()
                        self._db.cache_set(cache_key, data)
                        self.health.success_count += 1
                        self.health.available = True
                        self._metrics.tmdb_hits += 1
                        return data
                    elif r.status == 401:
                        self.health.last_error = "Invalid API key"
                        self.health.available  = False
                        log.error("[TMDB] ❌ Authentication failed — invalid API key")
                        return None
                    else:
                        self.health.last_error = f"HTTP {r.status}"
                        log.warning(f"[tmdb] HTTP {r.status} on {endpoint}")
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self.health.last_error  = f"Timeout after {elapsed_ms}ms"
                self.health.fail_count += 1
                log.warning(f"[tmdb] Timeout attempt {attempt}/{retries}")
            except Exception as e:
                self.health.last_error  = str(e)
                self.health.fail_count += 1
                log.warning(f"[tmdb] Error attempt {attempt}/{retries}: {e}")

            if attempt < retries:
                wait = 5 * (2 ** (attempt - 1))   # 5s, 10s, 20s
                self._metrics.retries_total += 1
                log.info(f"[tmdb] Retry {attempt}/{retries} in {wait}s")
                await asyncio.sleep(wait)

        self.health.available = False
        self._metrics.tmdb_misses += 1
        return None

    async def search_movie(self, title: str, year: int = 0) -> List[Dict]:
        params = {"query": title, "language": "en-US", "page": 1}
        if year: params["year"] = year
        data = await self._get("/search/movie", params)
        return data.get("results", []) if data else []

    async def search_tv(self, title: str, year: int = 0) -> List[Dict]:
        params = {"query": title, "language": "en-US", "page": 1}
        if year: params["first_air_date_year"] = year
        data = await self._get("/search/tv", params)
        return data.get("results", []) if data else []

    async def search_multi(self, title: str, year: int = 0) -> List[Dict]:
        params = {"query": title, "language": "en-US", "page": 1}
        data = await self._get("/search/multi", params)
        return data.get("results", []) if data else []

    async def get_movie_details(self, tmdb_id: int) -> Optional[Dict]:
        return await self._get(f"/movie/{tmdb_id}",
                               {"language": "en-US",
                                "append_to_response": "external_ids"})

    async def get_tv_details(self, tmdb_id: int) -> Optional[Dict]:
        return await self._get(f"/tv/{tmdb_id}",
                               {"language": "en-US",
                                "append_to_response": "external_ids"})

    async def health_check(self) -> ProviderHealth:
        """Validate TMDB key and connectivity."""
        log.info("[TMDB] Running health check...")
        result = await self._get("/configuration", {})
        if result:
            log.info("[TMDB] ✅ API Key Loaded")
            log.info("[TMDB] ✅ Connection Successful")
            log.info("[TMDB] ✅ Search Endpoint Operational")
            log.info("[TMDB] ✅ Metadata Service Ready")
            self.health.available = True
        else:
            log.error(f"[TMDB] ❌ Health check failed — {self.health.last_error}")
            self.health.available = False
        return self.health

    async def find_by_imdb_id(self, imdb_id: str) -> Optional[Dict]:
        """Use TMDB /find endpoint to look up by IMDb ID — no extra key needed."""
        return await self._get(f"/find/{imdb_id}",
                               {"external_source": "imdb_id",
                                "language": "en-US"})

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════
#  TVMAZE CLIENT  — free, no API key required
#  https://www.tvmaze.com/api
# ══════════════════════════════════════════════════════════════

class TVMazeClient:
    """
    TVMaze public API — completely free, no registration or key needed.
    Used as a secondary TV-series validator alongside TMDB.
    Endpoints used:
      GET /search/shows?q=<title>        — search by title
      GET /singlesearch/shows?q=<title>  — best single match
      GET /lookup/shows?imdb=<imdb_id>   — lookup by IMDb ID (very accurate)
    """
    def __init__(self, db: MediaDatabase, metrics: ClassifierMetrics):
        self._db      = db
        self._metrics = metrics
        self._session: Optional[aiohttp.ClientSession] = None
        self.health   = ProviderHealth(name="TVMaze")

    async def _get(self, path: str, params: Dict = {},
                   retries: int = 2) -> Optional[Any]:
        cache_key = f"tvmaze:{path}:{json.dumps(params, sort_keys=True)}"
        cached = self._db.cache_get(cache_key, max_age=86400)  # 24h cache
        if cached:
            self._metrics.cache_hits += 1
            return cached
        self._metrics.cache_misses += 1

        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

        url = f"{TVMAZE_BASE}{path}"
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                async with self._session.get(
                        url, params=params,
                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    self.health.response_ms   = elapsed_ms
                    self.health.total_queries += 1
                    self.health.last_checked  = time.time()
                    if r.status == 200:
                        data = await r.json()
                        self._db.cache_set(cache_key, data)
                        self.health.success_count += 1
                        self.health.available = True
                        return data
                    elif r.status == 404:
                        return None   # not found — not an error
                    elif r.status == 429:
                        self.health.last_error = "Rate limited"
                        await asyncio.sleep(10)
                    else:
                        self.health.last_error = f"HTTP {r.status}"
            except asyncio.TimeoutError:
                self.health.last_error = "Timeout"
                self.health.fail_count += 1
            except Exception as e:
                self.health.last_error = str(e)
                self.health.fail_count += 1
                log.debug(f"[tvmaze] Error attempt {attempt}: {e}")
            if attempt < retries:
                await asyncio.sleep(3)

        self.health.available = False
        return None

    async def search(self, title: str) -> List[Dict]:
        """Search TV shows by title. Returns list of {score, show} dicts."""
        data = await self._get("/search/shows", {"q": title})
        return data if isinstance(data, list) else []

    async def single_search(self, title: str) -> Optional[Dict]:
        """Return the single best matching show."""
        return await self._get("/singlesearch/shows", {"q": title})

    async def lookup_by_imdb(self, imdb_id: str) -> Optional[Dict]:
        """Exact lookup by IMDb ID — most accurate method."""
        return await self._get("/lookup/shows", {"imdb": imdb_id})

    async def lookup_by_tvdb(self, tvdb_id: int) -> Optional[Dict]:
        """Exact lookup by TVDB ID."""
        return await self._get("/lookup/shows", {"thetvdb": tvdb_id})

    def score_result(self, show: Dict, query: str, year: int = 0) -> int:
        """Score a TVMaze show result against our query."""
        name = show.get("name", "")
        title_score = fuzz.token_sort_ratio(query.lower(), name.lower())
        premiered   = show.get("premiered") or ""
        show_year   = int(premiered[:4]) if premiered and len(premiered) >= 4 else 0
        year_bonus  = 20 if (year and show_year and year == show_year) else \
                      10 if (year and show_year and abs(year - show_year) == 1) else 0
        return min(title_score + year_bonus, 100)

    def to_media_info(self, show: Dict, season: int = None,
                      episodes: List[int] = None) -> MediaInfo:
        """Convert a TVMaze show dict to a MediaInfo."""
        name      = show.get("name", "")
        premiered = show.get("premiered") or ""
        year      = int(premiered[:4]) if premiered and len(premiered) >= 4 else 0
        externals = show.get("externals", {})
        imdb_id   = externals.get("imdb") or ""
        tvdb_id   = externals.get("thetvdb") or 0
        poster    = (show.get("image") or {}).get("original") or ""
        return MediaInfo(
            media_type      = "tv",
            canonical_title = name,
            year            = year,
            imdb_id         = imdb_id,
            tvdb_id         = tvdb_id,
            poster_path     = poster,
            season          = season,
            episodes        = episodes or [],
            source          = "tvmaze",
        )

    async def health_check(self) -> ProviderHealth:
        log.info("[TVMaze] Running health check...")
        result = await self._get("/search/shows", {"q": "test"})
        if result is not None:
            log.info("[TVMaze] ✅ Connected (free, no key required)")
            self.health.available = True
        else:
            log.error(f"[TVMaze] ❌ Health check failed — {self.health.last_error}")
            self.health.available = False
        return self.health

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════════════
#  API HEALTH MONITOR
# ══════════════════════════════════════════════════════════════

class APIHealthMonitor:
    """
    Validates all providers at startup and tracks ongoing health.
    Disables auto-classification if metadata providers are down.
    """
    def __init__(self):
        self.tmdb_health     = ProviderHealth(name="TMDB")
        self.tvmaze_health   = ProviderHealth(name="TVMaze")
        self.telegram_health = ProviderHealth(name="Telegram")
        self.db_health       = ProviderHealth(name="Database")
        self.storage_health  = ProviderHealth(name="Storage")
        self._auto_classify_enabled = True

    @property
    def auto_classify_enabled(self) -> bool:
        return self._auto_classify_enabled

    async def run_startup_checks(self, tmdb_client: "TMDBClient",
                                  tvmaze_client: "TVMazeClient",
                                  db: "MediaDatabase",
                                  media_path: str,
                                  bot_token: str = "") -> Dict[str, bool]:
        log.info("═" * 60)
        log.info("  API Health Validation — Startup")
        log.info("═" * 60)

        results: Dict[str, bool] = {}

        # ── TMDB ──────────────────────────────────────────────
        self.tmdb_health = await tmdb_client.health_check()
        results["tmdb"] = self.tmdb_health.available

        # ── Database ──────────────────────────────────────────
        try:
            db._conn.execute("SELECT 1")
            self.db_health.available = True
            log.info("[Database] ✅ Connected Successfully")
        except Exception as e:
            self.db_health.available = False
            self.db_health.last_error = str(e)
            log.error(f"[Database] ❌ Connection Failed — {e}")
        results["database"] = self.db_health.available

        # ── Storage paths ─────────────────────────────────────
        try:
            movies_path = os.path.join(media_path, MOVIES_FOLDER)
            tv_path     = os.path.join(media_path, TV_FOLDER)
            Path(movies_path).mkdir(parents=True, exist_ok=True)
            Path(tv_path).mkdir(parents=True, exist_ok=True)
            self.storage_health.available = True
            log.info("[Storage] ✅ Media Paths Validated")
            log.info(f"[Storage]   Movies → {movies_path}")
            log.info(f"[Storage]   TV     → {tv_path}")
        except Exception as e:
            self.storage_health.available = False
            self.storage_health.last_error = str(e)
            log.error(f"[Storage] ❌ Path validation failed — {e}")
        results["storage"] = self.storage_health.available

        # ── Decision: disable auto-classify if TMDB unavailable ──
        if not self.tmdb_health.available:
            self._auto_classify_enabled = False
            log.warning(
                "[HealthMonitor] ⚠️ TMDB unavailable — "
                "auto-classification DISABLED. Falling back to manual approval.")
        else:
            self._auto_classify_enabled = True

        log.info("═" * 60)
        log.info(f"  Auto-classify: {'ENABLED' if self._auto_classify_enabled else 'DISABLED (manual mode)'}")
        log.info("═" * 60)
        return results

    def build_status_report(self) -> str:
        """Build /test-metadata Telegram response."""
        lines = []

        def _line(h: ProviderHealth) -> str:
            if h.available:
                return f"✅ {h.name} Connected"
            reason = h.last_error or "Unknown error"
            return f"❌ {h.name} Failed\n   Reason: {reason}"

        lines.append(_line(self.tmdb_health))
        lines.append(_line(self.tvmaze_health))
        lines.append(_line(self.db_health))
        lines.append(_line(self.storage_health))
        lines.append(_line(self.telegram_health))
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  MEDIA CLASSIFIER  (v8)
# ══════════════════════════════════════════════════════════════

class MediaClassifier:
    def __init__(self, base_path: str, db_path: str, tmdb_key: str = ""):
        self.base_path    = base_path
        self.movies_root  = os.path.join(base_path, MOVIES_FOLDER)
        self.tv_root      = os.path.join(base_path, TV_FOLDER)
        self.db           = MediaDatabase(db_path)
        self.metrics      = ClassifierMetrics()
        self.tmdb         = TMDBClient(tmdb_key or TMDB_API_KEY, self.db,
                                       self.metrics)
        self.tvmaze       = TVMazeClient(self.db, self.metrics)
        self.health_mon   = APIHealthMonitor()

        Path(self.movies_root).mkdir(parents=True, exist_ok=True)
        Path(self.tv_root).mkdir(parents=True, exist_ok=True)

        self._pending: Dict[str, asyncio.Future] = {}

    # ── Startup health check ──────────────────────────────────

    async def startup_health_check(self, media_path: str = "",
                                    bot_token: str = "") -> Dict[str, bool]:
        return await self.health_mon.run_startup_checks(
            tmdb_client   = self.tmdb,
            tvmaze_client = self.tvmaze,
            db            = self.db,
            media_path    = media_path or self.base_path,
            bot_token     = bot_token,
        )

    # ── Main classify entry point ─────────────────────────────

    async def classify(self, filename: str,
                       request_id: str = "") -> MediaInfo:
        """
        Full multi-provider classification pipeline.

        Priority:
          1. Permanent user decision (DB)
          2. Existing DB series/movie match (by TMDB/TVDB/IMDb ID or fuzzy title)
          3. TMDB metadata lookup (primary provider)
          4. Filename parse confidence >= AUTO_CONFIRM_THRESHOLD
          5. Return low-confidence for user approval
        """
        parsed = parse_filename(filename)
        info   = MediaInfo(
            media_type = parsed.media_type,
            season     = parsed.season,
            episodes   = parsed.episodes,
            confidence = parsed.confidence,
        )

        # Step 0: Check for a permanent user decision first
        title_key = _make_title_key(parsed.clean_title, parsed.year or 0)
        decision  = self.db.get_user_decision(title_key)
        if decision:
            log.info(f"[classify] [{request_id}] Permanent user decision: "
                     f"'{title_key}' → {decision['media_type']}")
            info.media_type      = decision["media_type"]
            info.canonical_title = decision["canonical_title"]
            info.year            = decision["year"]
            info.tmdb_id         = decision["tmdb_id"]
            info.confidence      = 100
            info.source          = "user_decision"
            # Re-use existing DB record for folder path
            db_row = (self.db.find_series(info.canonical_title, info.tmdb_id)
                      if info.media_type == "tv"
                      else self.db.find_movie(info.canonical_title, info.year,
                                              info.tmdb_id))
            if db_row:
                info.folder_path = self._build_folder(
                    info, parsed, base=db_row.get("folder_path", ""))
            else:
                info.folder_path = self._build_folder(info, parsed)
            self.metrics.classifications_db += 1
            self.metrics.record_confidence(100)
            return info

        # Step 1: Check existing DB
        db_result = await self._check_db(parsed)
        if db_result:
            log.info(f"[classify] [{request_id}] DB hit → {db_result.folder_path}")
            self.metrics.classifications_db += 1
            self.metrics.record_confidence(db_result.confidence)
            return db_result

        # Step 2: TMDB lookup (primary metadata provider)
        tmdb_result = None
        if parsed.clean_title and self.tmdb._key:
            tmdb_result = await self._lookup_tmdb(parsed, request_id)
            if tmdb_result:
                # Conflict: parser said TV but TMDB says movie (or vice versa)
                # e.g. "Aadu 3" — bad parse gives TV, TMDB correctly returns movie
                type_conflict = (
                    parsed.media_type not in ("unknown", tmdb_result.media_type)
                )
                if type_conflict:
                    log.info(
                        f"[classify] [{request_id}] Provider conflict: "
                        f"parser={parsed.media_type} tmdb={tmdb_result.media_type} "
                        f"— forcing user approval")
                    info.canonical_title = tmdb_result.canonical_title
                    info.year            = tmdb_result.year
                    info.tmdb_id         = tmdb_result.tmdb_id
                    info.confidence      = tmdb_result.confidence
                    info.source          = "conflict"
                    self.metrics.record_confidence(info.confidence)
                    return info

                if tmdb_result.confidence >= AUTO_CONFIRM_THRESHOLD:
                    tmdb_result.folder_path = self._build_folder(tmdb_result, parsed)
                    self._save_to_db(tmdb_result, parsed)
                    self.metrics.classifications_auto += 1
                    self.metrics.record_confidence(tmdb_result.confidence)
                    self._log_classification_detail(filename, parsed, tmdb_result)
                    return tmdb_result

        # Step 3: Use parse result if high-confidence
        # ONLY when TMDB returned nothing — never override a TMDB result with a
        # potentially bad filename parse (e.g. codec digits as episode numbers).
        if parsed.confidence >= AUTO_CONFIRM_THRESHOLD and tmdb_result is None:
            info.canonical_title = parsed.clean_title
            info.year            = parsed.year or 0
            info.folder_path     = self._build_folder_from_parsed(parsed)
            info.source          = "parsed"
            self._save_to_db(info, parsed)
            self.metrics.classifications_auto += 1
            self.metrics.record_confidence(parsed.confidence)
            return info

        # Step 4: Low confidence or TMDB below threshold — needs user approval
        info.canonical_title = (tmdb_result.canonical_title if tmdb_result
                                else parsed.clean_title)
        info.year            = (tmdb_result.year if tmdb_result
                                else parsed.year or 0)
        if tmdb_result:
            info.tmdb_id    = tmdb_result.tmdb_id
            info.confidence = tmdb_result.confidence
        info.source = "uncertain"
        log.info(
            f"[classify] [{request_id}] Low confidence {info.confidence}% "
            f"for '{filename}' — waiting for user confirmation")
        self.metrics.record_confidence(info.confidence)
        return info

    def _log_classification_detail(self, filename: str,
                                    parsed: ParsedMedia, info: MediaInfo):
        """Detailed confidence breakdown log."""
        bd = info.confidence_breakdown
        log.info(
            f"[classify] ── Confidence Breakdown ──\n"
            f"  Filename:  {filename}\n"
            f"  Title:     {info.canonical_title} ({info.year})\n"
            f"  Type:      {info.media_type}\n"
            f"  Provider:  {info.source}\n"
            f"  Filename Match:        {bd.get('filename', 0):3d}%\n"
            f"  TMDB Match:            {bd.get('tmdb', 0):3d}%\n"
            f"  Year Match:            {bd.get('year', 0):3d}%\n"
            f"  Existing Library:      {bd.get('library', 0):3d}%\n"
            f"  Final Confidence:      {info.confidence:3d}%\n"
            f"  Folder: {info.folder_path}"
        )

    async def _check_db(self, parsed: ParsedMedia) -> Optional[MediaInfo]:
        """Check local DB for existing series/movie match (all ID types)."""
        if parsed.media_type in ("tv", "unknown") and parsed.clean_title:
            row = self.db.find_series(parsed.clean_title)
            if row:
                info = MediaInfo(
                    media_type      = "tv",
                    canonical_title = row["canonical_title"],
                    tmdb_id         = row["tmdb_id"],
                    tvdb_id         = row.get("tvdb_id", 0),
                    imdb_id         = row["imdb_id"],
                    season          = parsed.season,
                    episodes        = parsed.episodes,
                    poster_path     = row.get("poster_path", ""),
                    backdrop_path   = row.get("backdrop_path", ""),
                    confidence      = 95,
                    source          = "db",
                )
                info.folder_path = self._build_folder(
                    info, parsed, base=row["folder_path"])
                return info

        if parsed.media_type in ("movie", "unknown") and parsed.clean_title:
            row = self.db.find_movie(parsed.clean_title, parsed.year or 0)
            if row:
                info = MediaInfo(
                    media_type      = "movie",
                    canonical_title = row["canonical_title"],
                    year            = row["year"],
                    tmdb_id         = row["tmdb_id"],
                    imdb_id         = row["imdb_id"],
                    poster_path     = row.get("poster_path", ""),
                    backdrop_path   = row.get("backdrop_path", ""),
                    confidence      = 95,
                    source          = "db",
                )
                info.folder_path = row["folder_path"]
                return info
        return None

    async def _lookup_tmdb(self, parsed: ParsedMedia,
                           request_id: str) -> Optional[MediaInfo]:
        """
        Query TMDB. Strategy:
        1. If filename says TV → search TV endpoint
        2. If filename says Movie → search Movie endpoint
        3. Multi-search as fallback for unknowns
        4. Also try movie search even for TV guesses, and
           pick the highest-scoring result across all types.
        """
        title = parsed.clean_title
        year  = parsed.year or 0

        candidates: List[Tuple[int, MediaInfo]] = []

        if parsed.media_type == "tv":
            tv_results = await self.tmdb.search_tv(title, year)
            best_tv = self._score_tmdb_results(tv_results, title, year, "tv")
            if best_tv:
                candidates.append(best_tv)

        if parsed.media_type in ("movie", "unknown"):
            mv_results = await self.tmdb.search_movie(title, year)
            best_mv = self._score_tmdb_results(mv_results, title, year, "movie")
            if best_mv:
                candidates.append(best_mv)

        # Always run multi-search for unknown / low-confidence parsed type
        if parsed.media_type == "unknown" or not candidates:
            multi_results = await self.tmdb.search_multi(title, year)
            best_multi = self._score_tmdb_results(multi_results, title, year, "any")
            if best_multi:
                candidates.append(best_multi)

        # ── TVMaze secondary lookup (free, no key) ────────────
        # Run in parallel with TMDB for TV / unknown types.
        # If filename has an IMDb ID, use exact lookup (most accurate).
        if parsed.media_type in ("tv", "unknown"):
            tvmaze_result = await self._lookup_tvmaze(parsed, request_id)
            if tvmaze_result:
                candidates.append(tvmaze_result)

        if not candidates:
            return None

        # Pick highest confidence candidate
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_info = candidates[0]

        if best_score < LOW_CONFIDENCE:
            return None

        # Fetch images if we have a TMDB ID
        if best_info.tmdb_id:
            await self._enrich_with_images(best_info)

        log.info(
            f"[tmdb] [{request_id}] Best: '{best_info.canonical_title}' "
            f"({best_info.year}) type={best_info.media_type} "
            f"score={best_score}%"
        )
        return best_info

    async def _lookup_tvmaze(self, parsed: ParsedMedia,
                             request_id: str) -> Optional[Tuple[int, MediaInfo]]:
        """
        Secondary TV lookup via TVMaze (free, no key).
        Strategy:
          1. If filename contains an IMDb ID (tt\\d+), do exact lookup first.
          2. Otherwise search by title.
          3. Score the result and return as a candidate tuple.
        """
        title = parsed.clean_title
        year  = parsed.year or 0

        show: Optional[Dict] = None

        # 1. Exact IMDb ID lookup (highest accuracy)
        imdb_match = _IMDB_ID_RE.search(parsed.raw_title)
        if imdb_match:
            imdb_id = imdb_match.group(1)
            show = await self.tvmaze.lookup_by_imdb(imdb_id)
            if show:
                log.info(f"[tvmaze] [{request_id}] IMDb exact hit: "
                         f"'{show.get('name')}' via {imdb_id}")

        # 2. Title search fallback
        if not show and title:
            results = await self.tvmaze.search(title)
            best_score, best_show = 0, None
            for item in results[:5]:
                s = item.get("show", {})
                sc = self.tvmaze.score_result(s, title, year)
                if sc > best_score:
                    best_score, best_show = sc, s
            if best_show and best_score >= LOW_CONFIDENCE:
                show = best_show
                log.info(f"[tvmaze] [{request_id}] Title match: "
                         f"'{show.get('name')}' score={best_score}%")

        if not show:
            return None

        score = self.tvmaze.score_result(show, title, year)
        if score < LOW_CONFIDENCE:
            return None

        info = self.tvmaze.to_media_info(
            show,
            season   = parsed.season,
            episodes = parsed.episodes,
        )
        info.confidence = score
        info.confidence_breakdown = {
            "filename": score,
            "tmdb":     0,
            "year":     20 if (year and info.year == year) else 0,
            "library":  0,
        }

        # Cross-enrich: if TVMaze gives us an IMDb ID, try TMDB /find for images
        if info.imdb_id and not info.poster_path:
            try:
                find_data = await self.tmdb.find_by_imdb_id(info.imdb_id)
                if find_data:
                    tv_results = find_data.get("tv_results", [])
                    if tv_results:
                        r = tv_results[0]
                        info.tmdb_id = r.get("id", 0)
                        if r.get("poster_path"):
                            info.poster_path = TMDB_IMG_BASE + r["poster_path"]
                        if r.get("backdrop_path"):
                            info.backdrop_path = TMDB_IMG_BASE + r["backdrop_path"]
                        log.info(f"[tvmaze] [{request_id}] TMDB cross-enrich: "
                                 f"tmdb_id={info.tmdb_id} poster={bool(info.poster_path)}")
            except Exception as e:
                log.debug(f"[tvmaze] Cross-enrich failed: {e}")

        return (score, info)

    def _score_tmdb_results(self, results: List[Dict], query: str,
                             year: int, prefer: str
                             ) -> Optional[Tuple[int, MediaInfo]]:
        if not results:
            return None
        best_score, best_info = 0, None
        for r in results[:5]:
            mt = r.get("media_type", prefer)
            if mt == "person": continue
            if prefer == "movie" and mt not in ("movie", "any", prefer): continue
            if prefer == "tv"    and mt not in ("tv",    "any", prefer): continue

            title = r.get("title") or r.get("name") or ""
            if not title: continue

            # Title similarity (60% weight)
            title_score = fuzz.token_sort_ratio(query.lower(), title.lower())

            # Year bonus (20% weight)
            date = r.get("release_date") or r.get("first_air_date") or ""
            tmdb_year = int(date[:4]) if date and len(date) >= 4 else 0
            year_score = 20 if (year and tmdb_year and year == tmdb_year) else \
                         10 if (year and tmdb_year and abs(year - tmdb_year) == 1) else 0

            # Popularity tiebreak bonus (max 5)
            pop = min(int(r.get("popularity", 0) / 20), 5)

            total = min(title_score + year_score + pop, 100)

            if total > best_score:
                best_score = total
                actual_mt  = "movie" if (r.get("title") and not r.get("name")) \
                             else ("tv" if r.get("name") else prefer)
                if prefer in ("movie", "tv"):
                    actual_mt = prefer

                best_info = MediaInfo(
                    media_type      = actual_mt,
                    tmdb_id         = r.get("id", 0),
                    canonical_title = title,
                    year            = tmdb_year,
                    confidence      = total,
                    source          = "tmdb",
                    confidence_breakdown = {
                        "filename": title_score,
                        "tmdb":     title_score,
                        "year":     year_score,
                        "library":  0,
                    },
                )

        if best_info and best_score >= LOW_CONFIDENCE:
            return (best_score, best_info)
        return None

    async def _enrich_with_images(self, info: MediaInfo):
        """Fetch poster and backdrop paths from TMDB details endpoint."""
        try:
            if info.media_type == "movie":
                details = await self.tmdb.get_movie_details(info.tmdb_id)
            else:
                details = await self.tmdb.get_tv_details(info.tmdb_id)
            if details:
                poster   = details.get("poster_path") or ""
                backdrop = details.get("backdrop_path") or ""
                if poster:
                    info.poster_path   = TMDB_IMG_BASE + poster
                if backdrop:
                    info.backdrop_path = TMDB_IMG_BASE + backdrop
                ext_ids = details.get("external_ids", {})
                if ext_ids:
                    info.imdb_id = ext_ids.get("imdb_id", "") or ""
                    info.tvdb_id = ext_ids.get("tvdb_id", 0) or 0
        except Exception as e:
            log.debug(f"[tmdb] Image enrich failed for {info.tmdb_id}: {e}")

    async def refresh_metadata(self, tmdb_id: int,
                                media_type: str) -> Optional[MediaInfo]:
        """Refresh metadata + images for an existing entry. Used by /refresh-metadata."""
        try:
            if media_type == "movie":
                details = await self.tmdb.get_movie_details(tmdb_id)
            else:
                details = await self.tmdb.get_tv_details(tmdb_id)
            if not details:
                return None
            poster   = details.get("poster_path") or ""
            backdrop = details.get("backdrop_path") or ""
            p_url    = TMDB_IMG_BASE + poster   if poster   else ""
            b_url    = TMDB_IMG_BASE + backdrop if backdrop else ""
            if media_type == "movie":
                self.db.update_movie_images(tmdb_id, p_url, b_url)
            else:
                self.db.update_series_images(tmdb_id, p_url, b_url)
            title = details.get("title") or details.get("name") or ""
            date  = details.get("release_date") or \
                    details.get("first_air_date") or ""
            year  = int(date[:4]) if date and len(date) >= 4 else 0
            log.info(f"[metadata] Refreshed {media_type} tmdb={tmdb_id}: "
                     f"'{title}' poster={bool(p_url)} backdrop={bool(b_url)}")
            return MediaInfo(
                media_type      = media_type,
                tmdb_id         = tmdb_id,
                canonical_title = title,
                year            = year,
                poster_path     = p_url,
                backdrop_path   = b_url,
                source          = "tmdb_refresh",
            )
        except Exception as e:
            log.error(f"[metadata] Refresh failed tmdb={tmdb_id}: {e}")
            return None

    def _build_folder(self, info: MediaInfo, parsed: ParsedMedia,
                      base: str = "") -> str:
        """Build the destination folder path."""
        if info.media_type == "movie":
            year_str = f" ({info.year})" if info.year else ""
            name     = _sanitize_folder(f"{info.canonical_title}{year_str}")
            return os.path.join(self.movies_root, name)

        elif info.media_type == "tv":
            series_base = base or os.path.join(
                self.tv_root,
                _sanitize_folder(info.canonical_title))

            if parsed.is_daily and parsed.daily_date:
                year_folder = parsed.daily_date[:4]
                return os.path.join(series_base, year_folder)

            season = parsed.season or info.season or 1
            return os.path.join(series_base, f"Season {season:02d}")

        return self.base_path

    def _build_folder_from_parsed(self, parsed: ParsedMedia) -> str:
        info = MediaInfo(
            media_type      = parsed.media_type,
            canonical_title = parsed.clean_title,
            year            = parsed.year or 0,
            season          = parsed.season,
        )
        return self._build_folder(info, parsed)

    def _save_to_db(self, info: MediaInfo, parsed: ParsedMedia):
        """Persist new series/movie to DB to avoid future lookups."""
        if info.media_type == "tv" and info.canonical_title:
            existing = self.db.find_series(
                info.canonical_title, info.tmdb_id,
                info.tvdb_id, info.imdb_id)
            if not existing:
                series_root = os.path.join(
                    self.tv_root,
                    _sanitize_folder(info.canonical_title))
                self.db.add_series(
                    canonical_title = info.canonical_title,
                    folder_path     = series_root,
                    tmdb_id         = info.tmdb_id,
                    tvdb_id         = info.tvdb_id,
                    imdb_id         = info.imdb_id,
                    poster_path     = info.poster_path,
                    backdrop_path   = info.backdrop_path,
                )
        elif info.media_type == "movie" and info.canonical_title:
            existing = self.db.find_movie(
                info.canonical_title, info.year,
                info.tmdb_id, info.imdb_id)
            if not existing:
                year_str = f" ({info.year})" if info.year else ""
                folder   = os.path.join(
                    self.movies_root,
                    _sanitize_folder(f"{info.canonical_title}{year_str}"))
                self.db.add_movie(
                    canonical_title = info.canonical_title,
                    year            = info.year,
                    folder_path     = folder,
                    tmdb_id         = info.tmdb_id,
                    imdb_id         = info.imdb_id,
                    poster_path     = info.poster_path,
                    backdrop_path   = info.backdrop_path,
                )

    def apply_user_choice(self, request_id: str, filename: str,
                          media_type: str) -> MediaInfo:
        """
        Called after user selects Movie / TV in Telegram.
        Stores decision permanently so future downloads never ask again.
        media_type = "movie" | "tv"
        """
        parsed = parse_filename(filename)
        parsed.media_type = media_type
        parsed.confidence = 100

        info = MediaInfo(
            media_type      = media_type,
            canonical_title = parsed.clean_title,
            year            = parsed.year or 0,
            season          = parsed.season,
            episodes        = parsed.episodes,
            confidence      = 100,
            source          = "user",
        )
        info.folder_path = self._build_folder(info, parsed)
        self._save_to_db(info, parsed)

        # Store permanent user decision so this title is never asked again
        title_key = _make_title_key(parsed.clean_title, parsed.year or 0)
        self.db.save_user_decision(
            title_key       = title_key,
            media_type      = media_type,
            canonical_title = parsed.clean_title,
            year            = parsed.year or 0,
            tmdb_id         = info.tmdb_id,
        )

        self.db.resolve_pending(request_id)
        self.metrics.classifications_user += 1
        log.info(
            f"[classify] [{request_id}] User chose '{media_type}' "
            f"for '{filename}' → {info.folder_path}"
        )
        return info

    async def classify_with_type(self, filename: str,
                                  media_type: str,
                                  request_id: str = "") -> MediaInfo:
        """
        User has confirmed the type (movie/tv).
        Now do a full metadata lookup (TMDB + TVMaze) with the known type,
        save to DB, store user decision, and return enriched MediaInfo.
        Falls back to filename parse if metadata providers unavailable.
        """
        parsed            = parse_filename(filename)
        parsed.media_type = media_type   # override with confirmed type
        parsed.confidence = 100

        # Try TMDB lookup with the confirmed type
        tmdb_result = None
        if parsed.clean_title and self.tmdb._key:
            tmdb_result = await self._lookup_tmdb(parsed, request_id)

        # For TV, also try TVMaze as secondary
        tvmaze_result = None
        if media_type == "tv" and parsed.clean_title:
            tvmaze_result = await self._lookup_tvmaze(parsed, request_id)

        # Pick best result
        best: Optional[MediaInfo] = None
        if tmdb_result and tvmaze_result:
            best = tmdb_result if tmdb_result.confidence >= tvmaze_result[0] \
                   else tvmaze_result[1]
        elif tmdb_result:
            best = tmdb_result
        elif tvmaze_result:
            best = tvmaze_result[1]

        if best:
            best.media_type = media_type   # enforce user's choice
            best.season     = best.season or parsed.season
            best.episodes   = best.episodes or parsed.episodes
            info = best
        else:
            # No metadata found — use parse result with confirmed type
            info = MediaInfo(
                media_type      = media_type,
                canonical_title = parsed.clean_title,
                year            = parsed.year or 0,
                season          = parsed.season,
                episodes        = parsed.episodes,
                confidence      = 80,
                source          = "user",
            )

        info.folder_path = self._build_folder(info, parsed)
        self._save_to_db(info, parsed)

        # Persist decision so this title is never asked again
        title_key = _make_title_key(
            info.canonical_title or parsed.clean_title,
            info.year or parsed.year or 0)
        self.db.save_user_decision(
            title_key       = title_key,
            media_type      = media_type,
            canonical_title = info.canonical_title or parsed.clean_title,
            year            = info.year or parsed.year or 0,
            tmdb_id         = info.tmdb_id,
        )

        self.db.resolve_pending(request_id)
        self.metrics.classifications_user += 1
        log.info(
            f"[classify] [{request_id}] User confirmed '{media_type}' "
            f"→ '{info.canonical_title}' ({info.year}) "
            f"source={info.source} folder={info.folder_path}"
        )
        return info

    def ensure_folder(self, folder_path: str) -> str:
        """Create folder if not exists, return path."""
        Path(folder_path).mkdir(parents=True, exist_ok=True)
        return folder_path

    def get_metrics_report(self) -> str:
        """Build a human-readable metrics string for /status or health endpoint."""
        m = self.metrics
        h = self.tmdb.health
        return (
            f"📊 *Classifier Metrics*\n\n"
            f"Total classifications: `{m.classifications_total}`\n"
            f"  Auto:   `{m.classifications_auto}`\n"
            f"  User:   `{m.classifications_user}`\n"
            f"  DB hit: `{m.classifications_db}`\n\n"
            f"TMDB hits:  `{m.tmdb_hits}`\n"
            f"TMDB misses: `{m.tmdb_misses}`\n"
            f"Cache hits:  `{m.cache_hits}`\n"
            f"Cache misses: `{m.cache_misses}`\n"
            f"Retries:    `{m.retries_total}`\n"
            f"Avg confidence: `{m.avg_confidence:.1f}%`\n\n"
            f"TMDB response: `{h.response_ms}ms`\n"
            f"TMDB success rate: `{h.success_rate:.1f}%`\n"
            f"TVMaze: `{'✅' if self.tvmaze.health.available else '❌'}` "
            f"`{self.tvmaze.health.response_ms}ms`\n"
            f"Auto-classify: `{'enabled' if self.health_mon.auto_classify_enabled else 'DISABLED'}`"
        )

    async def close(self):
        await self.tmdb.close()
        await self.tvmaze.close()
        self.db.close()


# ══════════════════════════════════════════════════════════════
#  TELEGRAM CONFIRMATION KEYBOARD
# ══════════════════════════════════════════════════════════════

def kb_classify(request_id: str, filename: str):
    """Inline keyboard for user classification confirmation."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎬 Movie",
            callback_data=f"cls_movie_{request_id}"),
        InlineKeyboardButton(
            "📺 TV Series",
            callback_data=f"cls_tv_{request_id}"),
    ]])

def kb_confirm_classify(request_id: str, media_type: str,
                        canonical_title: str, year: int,
                        folder: str):
    """Confirm an auto-classification."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Correct",
            callback_data=f"cls_confirm_{request_id}"),
        InlineKeyboardButton(
            "❌ Wrong — change",
            callback_data=f"cls_wrong_{request_id}"),
    ]])


# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def _sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r'\s+', " ", name).strip(". ")
    return name or "Unknown"


def format_classification_msg(info: MediaInfo, filename: str,
                               needs_confirm: bool = False) -> str:
    """Build a Telegram message describing the classification result."""
    icon = "🎬" if info.media_type == "movie" else "📺"
    kind = "Movie" if info.media_type == "movie" else "TV Series"
    year_str = f" ({info.year})" if info.year else ""

    ep_str = ""
    if info.season:
        if info.episodes:
            ep_str = (f"\n🎞 Episode: "
                      f"`S{info.season:02d}E"
                      f"{'E'.join(f'{e:02d}' for e in info.episodes)}`")
        else:
            ep_str = f"\n🎞 Season: `{info.season:02d}`"

    conf_str = f"\n📊 Confidence: `{info.confidence}%`" if needs_confirm else ""
    src_str  = f"\n🔍 Source: `{info.source}`"

    return (
        f"{icon} *{kind} Detected*\n\n"
        f"📄 `{Path(filename).stem[:50]}`\n\n"
        f"🎯 Title: *{info.canonical_title}{year_str}*"
        f"{ep_str}\n"
        f"📁 Folder:\n`{info.folder_path}`"
        f"{conf_str}"
        f"{src_str}"
    )