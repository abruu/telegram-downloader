#!/bin/bash
# ================================================================
#   Telegram Media Bot — One-Shot Setup
#   Clone → chmod +x setup.sh → ./setup.sh — that's it.
# ================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET}  $1"; }
warn() { echo -e "${YELLOW}⚠${RESET}  $1"; }
err()  { echo -e "${RED}✗${RESET}  $1"; }
hdr()  { echo -e "\n${BOLD}${CYAN}━━━  $1  ━━━${RESET}\n"; }
die()  { err "$1"; exit 1; }

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

clear
echo -e "${BOLD}"
cat << 'BANNER'
  ╔══════════════════════════════════════════════════════╗
  ║       Telegram Media Bot — Setup Wizard              ║
  ║                                                      ║
  ║  Downloads movies & TV from Telegram links,          ║
  ║  classifies them with TMDB metadata, and organises   ║
  ║  them for Jellyfin / Plex / Emby automatically.      ║
  ║                                                      ║
  ║  Perfect for Malayalam & regional content that       ║
  ║  Radarr/Sonarr indexers don't cover.                 ║
  ╚══════════════════════════════════════════════════════╝
BANNER
echo -e "${RESET}"


# ════════════════════════════════════════════════════════
#  STEP 1 — System prerequisites (auto-install if missing)
# ════════════════════════════════════════════════════════
hdr "Step 1 — System prerequisites"

# Auto-install python3 + pip if missing (Debian/Ubuntu only)
if ! command -v python3 &>/dev/null; then
    warn "python3 not found — attempting auto-install..."
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip \
        || die "Could not install python3. Please install it manually."
fi
ok "python3 $(python3 --version | cut -d' ' -f2)"

if ! command -v pip3 &>/dev/null; then
    warn "pip3 not found — attempting auto-install..."
    sudo apt-get install -y python3-pip 2>/dev/null \
        || python3 -m ensurepip --upgrade 2>/dev/null \
        || die "Could not install pip3. Please install python3-pip manually."
fi
ok "pip3 found"

# curl is needed for health checks
if ! command -v curl &>/dev/null; then
    warn "curl not found — attempting auto-install..."
    sudo apt-get install -y curl 2>/dev/null || warn "curl unavailable — API checks will be skipped"
fi
command -v curl &>/dev/null && ok "curl found"

PYVER=$(python3 -c 'import sys; print(sys.version_info >= (3,9))')
[[ "$PYVER" == "True" ]] && ok "Python ≥ 3.9 ✓" || warn "Python 3.9+ recommended"


# ════════════════════════════════════════════════════════
#  STEP 2 — Python dependencies
# ════════════════════════════════════════════════════════
hdr "Step 2 — Installing Python dependencies"

pip3 install -q \
    telethon \
    "python-telegram-bot>=21.0" \
    aiohttp \
    aiofiles \
    python-dotenv \
    fuzzywuzzy \
    python-Levenshtein \
    --break-system-packages
ok "All Python packages installed"


# ════════════════════════════════════════════════════════
#  STEP 3 — Directory structure
# ════════════════════════════════════════════════════════
hdr "Step 3 — Creating directories"

for d in logs data downloads media config; do
    mkdir -p "${INSTALL_DIR}/${d}"
    ok "./${d}/"
done


# ════════════════════════════════════════════════════════
#  STEP 4 — Patch Python source files
#  (inject config/settings.py import, remove hardcoded values)
# ════════════════════════════════════════════════════════
hdr "Step 4 — Patching source files"

# ── Patch tg_downloader_bot.py ──────────────────────────────────
BOT_FILE="${INSTALL_DIR}/tg_downloader_bot.py"

if [[ ! -f "$BOT_FILE" ]]; then
    die "tg_downloader_bot.py not found in ${INSTALL_DIR}"
fi

if grep -q "from config.settings import settings" "$BOT_FILE"; then
    ok "tg_downloader_bot.py — already patched"
else
    # 1. Add settings import after the classifier import block
    python3 - "$BOT_FILE" << 'PYEOF'
import sys, re

path = sys.argv[1]
text = open(path).read()

# Add settings import after classifier import block
old = "# Import our classifier module\nfrom media_classifier import ("
new = "# Import our classifier module\nfrom media_classifier import ("
insert_after = "    MOVIES_FOLDER, TV_FOLDER, TMDB_IMG_BASE\n)"
replacement = insert_after + "\n\n# Centralised config — all settings come from .env\nfrom config.settings import settings"
text = text.replace(insert_after, replacement, 1)

# 2. Replace the entire hardcoded settings block
hardcoded_block = re.search(
    r"# ═+\n#  ★  YOUR SETTINGS  ★.*?PENDING_FORCE_TTL\s*=\s*\d+",
    text, re.DOTALL
)
if hardcoded_block:
    new_block = (
        "# ══════════════════════════════════════════════════════════════\n"
        "#  ★  SETTINGS — loaded from .env via config/settings.py  ★\n"
        "# ══════════════════════════════════════════════════════════════\n\n"
        "BOT_TOKEN     = settings.BOT_TOKEN\n"
        "API_ID        = settings.API_ID\n"
        "API_HASH      = settings.API_HASH\n"
        "YOUR_USER_ID  = settings.YOUR_USER_ID\n\n"
        "MEDIA_PATH     = settings.MEDIA_PATH\n"
        "DOWNLOAD_PATH  = settings.DOWNLOAD_PATH\n"
        "SESSION_PATH   = settings.SESSION_PATH\n"
        "LOG_FILE       = settings.LOG_FILE\n"
        "USERS_FILE     = settings.USERS_FILE\n"
        "REQUEST_LOG_DB = settings.REQUEST_LOG_DB\n"
        "MEDIA_DB       = settings.MEDIA_DB\n\n"
        "TMDB_API_KEY   = settings.TMDB_API_KEY\n\n"
        "KNOWN_MOVIE_CHANNELS: List[int] = settings.KNOWN_MOVIE_CHANNELS\n\n"
        "# ── Speed / behaviour ─────────────────────────────────────────\n"
        "PARALLEL_CONNECTIONS  = settings.PARALLEL_CONNECTIONS\n"
        "CHUNK_SIZE            = settings.CHUNK_SIZE\n"
        "FLUSH_EVERY           = settings.FLUSH_EVERY\n"
        "MAX_QUEUE             = settings.MAX_QUEUE\n"
        "MAX_QUEUE_PER_USER    = settings.MAX_QUEUE_PER_USER\n"
        "MAX_RETRIES           = settings.MAX_RETRIES\n"
        "RETRY_BASE            = settings.RETRY_BASE\n"
        "RETRY_CAP             = settings.RETRY_CAP\n"
        "PROGRESS_EVERY        = settings.PROGRESS_EVERY\n"
        "WATCHDOG_TIMEOUT      = settings.WATCHDOG_TIMEOUT\n"
        "SMALL_FILE_LIMIT      = settings.SMALL_FILE_LIMIT\n"
        "DISK_HEADROOM         = settings.DISK_HEADROOM\n"
        "HISTORY_MAX           = settings.HISTORY_MAX\n"
        "ENTITY_CACHE_TTL      = settings.ENTITY_CACHE_TTL\n"
        "CHANNEL_SCAN_LIMIT    = settings.CHANNEL_SCAN_LIMIT\n"
        "STRICT_SIZE_THRESHOLD = settings.STRICT_SIZE_THRESHOLD\n"
        "PENDING_FORCE_TTL     = settings.PENDING_FORCE_TTL"
    )
    text = text[:hardcoded_block.start()] + new_block + text[hardcoded_block.end():]

open(path, 'w').write(text)
print("patched")
PYEOF
    ok "tg_downloader_bot.py — patched"
fi

# ── Patch media_classifier.py ───────────────────────────────────
CLF_FILE="${INSTALL_DIR}/media_classifier.py"

if [[ ! -f "$CLF_FILE" ]]; then
    die "media_classifier.py not found in ${INSTALL_DIR}"
fi

if grep -q "from config.settings import settings" "$CLF_FILE"; then
    ok "media_classifier.py — already patched"
else
    python3 - "$CLF_FILE" << 'PYEOF'
import sys

path = sys.argv[1]
text = open(path).read()

# Insert settings import + replace hardcoded keys
old1 = 'log = logging.getLogger("dlbot.classifier")\n\n# ── Provider config ────────────────────────────────────────────\nTMDB_API_KEY   = "3d912d66e20edee132b95e787c0278b2"'
new1 = 'log = logging.getLogger("dlbot.classifier")\n\nfrom config.settings import settings\n\n# ── Provider config ────────────────────────────────────────────\nTMDB_API_KEY   = settings.TMDB_API_KEY'
text = text.replace(old1, new1, 1)

old2 = '# TVDB — set your key if available\nTVDB_API_KEY   = "46d65058-bd37-4db2-b4a9-c10b8d9580ca"'
new2 = '# TVDB — set your key if available\nTVDB_API_KEY   = settings.TVDB_API_KEY'
text = text.replace(old2, new2, 1)

open(path, 'w').write(text)
print("patched")
PYEOF
    ok "media_classifier.py — patched"
fi


# ════════════════════════════════════════════════════════
#  STEP 5 — Write config/settings.py
# ════════════════════════════════════════════════════════
hdr "Step 5 — Writing config/settings.py"

touch "${INSTALL_DIR}/config/__init__.py"

cat > "${INSTALL_DIR}/config/settings.py" << 'PYEOF'
"""
config/settings.py — All configuration loaded from .env
Import with:  from config.settings import settings
"""
import os, sys
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

def _require(key):
    val = os.getenv(key, "").strip()
    if not val:
        print(f"\n❌  Missing required setting: {key}")
        print(f"    Add it to .env or re-run ./setup.sh\n")
        sys.exit(1)
    return val

def _opt(key, default=""):
    return os.getenv(key, default).strip()

def _int(key, default):
    try: return int(os.getenv(key, str(default)))
    except ValueError: return default

class _Settings:
    BOT_TOKEN    = _require("TELEGRAM_BOT_TOKEN")
    API_ID       = int(_require("TELEGRAM_API_ID"))
    API_HASH     = _require("TELEGRAM_API_HASH")
    YOUR_USER_ID = int(_require("TELEGRAM_OWNER_ID"))

    TMDB_API_KEY = _opt("TMDB_API_KEY")
    TVDB_API_KEY = _opt("TVDB_API_KEY")

    BASE_DIR      = _ROOT
    MEDIA_PATH    = _opt("MEDIA_PATH",     str(_ROOT / "media"))
    DOWNLOAD_PATH = _opt("DOWNLOAD_PATH",  str(_ROOT / "downloads"))
    SESSION_PATH  = _opt("SESSION_PATH",   str(_ROOT / "data" / "userbot_session"))
    LOG_FILE      = _opt("LOG_FILE",       str(_ROOT / "logs" / "bot.log"))
    USERS_FILE    = _opt("USERS_FILE",     str(_ROOT / "data" / "users.json"))
    REQUEST_LOG_DB = _opt("REQUEST_LOG_DB",str(_ROOT / "data" / "request_log.db"))
    MEDIA_DB      = _opt("MEDIA_DB",       str(_ROOT / "data" / "media_library.db"))

    PARALLEL_CONNECTIONS  = _int("PARALLEL_CONNECTIONS",  4)
    CHUNK_SIZE            = _int("CHUNK_SIZE",            524288)
    FLUSH_EVERY           = _int("FLUSH_EVERY",           4194304)
    MAX_QUEUE             = _int("MAX_QUEUE",             20)
    MAX_QUEUE_PER_USER    = _int("MAX_QUEUE_PER_USER",    3)
    MAX_RETRIES           = _int("MAX_RETRIES",           20)
    RETRY_BASE            = _int("RETRY_BASE",            8)
    RETRY_CAP             = _int("RETRY_CAP",             180)
    PROGRESS_EVERY        = _int("PROGRESS_EVERY",        5)
    WATCHDOG_TIMEOUT      = _int("WATCHDOG_TIMEOUT",      120)
    SMALL_FILE_LIMIT      = _int("SMALL_FILE_LIMIT",      10485760)
    DISK_HEADROOM         = _int("DISK_HEADROOM",         209715200)
    HISTORY_MAX           = _int("HISTORY_MAX",           50)
    ENTITY_CACHE_TTL      = _int("ENTITY_CACHE_TTL",      3600)
    CHANNEL_SCAN_LIMIT    = _int("CHANNEL_SCAN_LIMIT",    1000)
    STRICT_SIZE_THRESHOLD = float(_opt("STRICT_SIZE_THRESHOLD", "0.03"))
    PENDING_FORCE_TTL     = _int("PENDING_FORCE_TTL",     86400)

    @property
    def KNOWN_MOVIE_CHANNELS(self):
        raw = _opt("KNOWN_MOVIE_CHANNELS", "")
        if not raw: return []
        return [int(x.strip()) for x in raw.split(",") if x.strip()]

    @property
    def VPN_ENABLED(self):
        return os.getenv("VPN_ENABLED", "false").strip().lower() in ("true", "1", "yes")

    VPN_CONFIG_FILE = _opt("VPN_CONFIG_FILE")
    VPN_AUTH_FILE   = _opt("VPN_AUTH_FILE")

    @property
    def VPN_RECONNECT(self):
        return os.getenv("VPN_RECONNECT", "true").strip().lower() not in ("false", "0", "no")

    VPN_MAX_RETRIES = _int("VPN_MAX_RETRIES", 5)

settings = _Settings()
PYEOF
ok "config/settings.py written"


# ════════════════════════════════════════════════════════
#  STEP 6 — Configuration (.env)
# ════════════════════════════════════════════════════════
hdr "Step 6 — Configuration"

if [[ -f "${INSTALL_DIR}/.env" ]]; then
    echo -e "${YELLOW}Existing .env found.${RESET}"
    read -rp "  Overwrite it? [y/N]: " OVERWRITE
    if [[ "${OVERWRITE,,}" != "y" ]]; then
        ok "Keeping existing .env — skipping to health checks"
        # still need to read values for later steps
        source "${INSTALL_DIR}/.env" 2>/dev/null || true
        TMDB_KEY="${TMDB_API_KEY:-}"
        TVDB_KEY="${TVDB_API_KEY:-}"
        MEDIA_PATH_VAL="${MEDIA_PATH:-${INSTALL_DIR}/media}"
        DL_PATH_VAL="${DOWNLOAD_PATH:-${INSTALL_DIR}/downloads}"
        SKIP_ENV=1
    fi
fi

if [[ -z "${SKIP_ENV:-}" ]]; then
    echo ""
    echo -e "  ${BOLD}You will need:${RESET}"
    echo "   • Telegram Bot Token  → @BotFather → /newbot"
    echo "   • API ID + Hash       → https://my.telegram.org → API development tools"
    echo "   • Your Telegram ID    → message @userinfobot on Telegram"
    echo ""

    while true; do
        read -rp "  Telegram Bot Token: " BOT_TOKEN
        [[ "$BOT_TOKEN" =~ ^[0-9]{8,10}:[A-Za-z0-9_-]{35}$ ]] && break
        err "Invalid token format (expect: 123456789:ABCdef...XYZ)"
    done

    while true; do
        read -rp "  Telegram API ID (numbers only): " API_ID
        [[ "$API_ID" =~ ^[0-9]+$ ]] && break
        err "API ID must be a number"
    done

    while true; do
        read -rp "  Telegram API Hash (32 hex chars): " API_HASH
        [[ "$API_HASH" =~ ^[a-f0-9]{32}$ ]] && break
        err "API Hash must be 32 lowercase hex characters"
    done

    while true; do
        read -rp "  Your Telegram User ID: " OWNER_ID
        [[ "$OWNER_ID" =~ ^[0-9]+$ ]] && break
        err "Must be a number — get it from @userinfobot"
    done

    echo ""
    echo -e "  ${CYAN}TMDB API Key${RESET} — enables metadata & auto-classification"
    echo "  Free key: https://www.themoviedb.org/settings/api"
    read -rp "  TMDB API Key [blank to skip]: " TMDB_KEY

    read -rp "  TVDB API Key [blank to skip]: " TVDB_KEY

    echo ""
    echo "  Where should finished media be stored? (your Jellyfin/Plex library root)"
    read -rp "  Media path [default: ${INSTALL_DIR}/media]: " MEDIA_PATH_VAL
    MEDIA_PATH_VAL="${MEDIA_PATH_VAL:-${INSTALL_DIR}/media}"
    mkdir -p "$MEDIA_PATH_VAL"

    read -rp "  Download staging path [default: ${MEDIA_PATH_VAL}/downloads]: " DL_PATH_VAL
    DL_PATH_VAL="${DL_PATH_VAL:-${MEDIA_PATH_VAL}/downloads}"
    mkdir -p "$DL_PATH_VAL"

    echo ""
    echo -e "  ${CYAN}VPN (OpenVPN)${RESET} — route all traffic through a VPN tunnel"
    read -rp "  Enable VPN? [y/N]: " VPN_CHOICE
    VPN_ENABLED_VAL="false"
    VPN_CONFIG_VAL=""
    VPN_AUTH_VAL=""
    VPN_RECONNECT_VAL="true"
    VPN_MAX_RETRIES_VAL="5"
    if [[ "${VPN_CHOICE,,}" == "y" ]]; then
        # Check openvpn is installed
        if ! command -v openvpn &>/dev/null; then
            warn "'openvpn' not found — attempting to install..."
            sudo apt-get install -y openvpn 2>/dev/null \
                && ok "openvpn installed" \
                || warn "Could not auto-install openvpn. Install it manually: sudo apt install openvpn"
        else
            ok "openvpn found ($(openvpn --version 2>&1 | head -1))"
        fi
        while true; do
            read -rp "  Path to your .ovpn config file: " VPN_CONFIG_VAL
            [[ -f "$VPN_CONFIG_VAL" ]] && break
            err "File not found: $VPN_CONFIG_VAL — try again (absolute path)"
        done
        read -rp "  Path to credentials file (user/pass, leave blank if not needed): " VPN_AUTH_VAL
        if [[ -n "$VPN_AUTH_VAL" && ! -f "$VPN_AUTH_VAL" ]]; then
            warn "Credentials file not found: $VPN_AUTH_VAL — you can set it later in .env"
        fi
        read -rp "  Auto-reconnect on tunnel drop? [Y/n]: " VPN_RECONNECT_CHOICE
        [[ "${VPN_RECONNECT_CHOICE,,}" == "n" ]] && VPN_RECONNECT_VAL="false"
        read -rp "  Max reconnect attempts (0=unlimited) [default: 5]: " VPN_MAX_RETRIES_INPUT
        VPN_MAX_RETRIES_VAL="${VPN_MAX_RETRIES_INPUT:-5}"
        VPN_ENABLED_VAL="true"
        ok "VPN configured (${VPN_CONFIG_VAL})"
    else
        ok "VPN skipped — you can enable it later by editing .env"
    fi

    hdr "Writing .env"

    cat > "${INSTALL_DIR}/.env" << EOF
# Generated by setup.sh on $(date)
# Re-run ./setup.sh to reconfigure

TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_API_ID=${API_ID}
TELEGRAM_API_HASH=${API_HASH}
TELEGRAM_OWNER_ID=${OWNER_ID}

TMDB_API_KEY=${TMDB_KEY}
TVDB_API_KEY=${TVDB_KEY}

MEDIA_PATH=${MEDIA_PATH_VAL}
DOWNLOAD_PATH=${DL_PATH_VAL}
SESSION_PATH=${INSTALL_DIR}/data/userbot_session
REQUEST_LOG_DB=${INSTALL_DIR}/data/request_log.db
MEDIA_DB=${INSTALL_DIR}/data/media_library.db
USERS_FILE=${INSTALL_DIR}/data/users.json
LOG_FILE=${INSTALL_DIR}/logs/bot.log

# VPN
VPN_ENABLED=${VPN_ENABLED_VAL}
VPN_CONFIG_FILE=${VPN_CONFIG_VAL}
VPN_AUTH_FILE=${VPN_AUTH_VAL}
VPN_RECONNECT=${VPN_RECONNECT_VAL}
VPN_MAX_RETRIES=${VPN_MAX_RETRIES_VAL}
EOF

    chmod 600 "${INSTALL_DIR}/.env"
    ok ".env written and locked (600)"
fi

chmod -R 755 "${INSTALL_DIR}/downloads" "${INSTALL_DIR}/media" "${INSTALL_DIR}/logs" 2>/dev/null || true


# ════════════════════════════════════════════════════════
#  STEP 7 — Health checks
# ════════════════════════════════════════════════════════
hdr "Step 7 — Health checks"

if command -v curl &>/dev/null; then
    if [[ -n "${TMDB_KEY:-}" ]]; then
        ST=$(curl -s -o /dev/null -w "%{http_code}" \
            "https://api.themoviedb.org/3/configuration?api_key=${TMDB_KEY}" 2>/dev/null || echo "000")
        [[ "$ST" == "200" ]] && ok "TMDB API — connected" \
            || err "TMDB API — HTTP ${ST} — double-check your key"
    else
        warn "TMDB — skipped (no key)"
    fi

    if [[ -n "${TVDB_KEY:-}" ]]; then
        ST=$(curl -s -o /dev/null -w "%{http_code}" \
            "https://api4.thetvdb.com/v4/login" \
            -H "Content-Type: application/json" \
            -d "{\"apikey\":\"${TVDB_KEY}\"}" 2>/dev/null || echo "000")
        [[ "$ST" == "200" ]] && ok "TVDB API — connected" \
            || warn "TVDB API — HTTP ${ST}"
    else
        warn "TVDB — skipped (no key)"
    fi
else
    warn "curl not available — skipping API checks"
fi

for path in "${MEDIA_PATH_VAL:-${INSTALL_DIR}/media}" \
            "${DL_PATH_VAL:-${INSTALL_DIR}/downloads}" \
            "${INSTALL_DIR}/logs" \
            "${INSTALL_DIR}/data"; do
    [[ -w "$path" ]] && ok "Writable: $path" || err "Not writable: $path"
done

# Quick import test — catches missing deps before first run
python3 -c "
import importlib, sys
for m in ['telethon','telegram','aiohttp','aiofiles','dotenv','fuzzywuzzy']:
    try: importlib.import_module(m); print(f'  ✓  {m}')
    except ImportError: print(f'  ✗  {m} MISSING'); sys.exit(1)
" && ok "All Python imports OK"


# ════════════════════════════════════════════════════════
#  STEP 8 — Generate systemd service
# ════════════════════════════════════════════════════════
hdr "Step 8 — Generating systemd service"

INSTALL_USER=$(whoami)

cat > "${INSTALL_DIR}/tgbot.service" << EOF
[Unit]
Description=Telegram Media Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${INSTALL_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/tg_downloader_bot.py
Restart=always
RestartSec=10
StartLimitIntervalSec=0
MemoryMax=300M
MemorySwapMax=0
StandardOutput=append:/var/log/tgbot.log
StandardError=append:/var/log/tgbot.log

[Install]
WantedBy=multi-user.target
EOF

ok "tgbot.service generated (User=${INSTALL_USER})"


# ════════════════════════════════════════════════════════
#  STEP 9 — Install systemd service (optional)
# ════════════════════════════════════════════════════════
hdr "Step 9 — Systemd service (run on boot)"

if command -v systemctl &>/dev/null && [[ -d /etc/systemd/system ]]; then
    read -rp "  Install as a systemd service (starts on boot)? [Y/n]: " DO_SERVICE
    if [[ "${DO_SERVICE,,}" != "n" ]]; then
        sudo cp "${INSTALL_DIR}/tgbot.service" /etc/systemd/system/tgbot.service \
            && sudo systemctl daemon-reload \
            && ok "Service installed" \
            || warn "Could not install service — you can do it later with: sudo cp tgbot.service /etc/systemd/system/"
    else
        ok "Skipped — you can install later with: sudo cp tgbot.service /etc/systemd/system/"
    fi
else
    warn "systemd not detected — service install skipped"
fi


# ════════════════════════════════════════════════════════
#  STEP 10 — First-run Telethon auth
# ════════════════════════════════════════════════════════
hdr "Step 10 — Telegram login (one-time)"

echo "  Telethon needs to log in to your Telegram account once"
echo "  to create a session file. You will need:"
echo "   • Your phone number (international format, e.g. +919876543210)"
echo "   • The OTP Telegram sends you"
echo ""
read -rp "  Do this now? [Y/n]: " DO_AUTH

if [[ "${DO_AUTH,,}" != "n" ]]; then
    echo ""
    echo -e "  ${YELLOW}Enter your phone number when prompted, then the OTP.${RESET}"
    echo -e "  ${YELLOW}After auth completes the bot will exit — that's normal.${RESET}"
    echo ""

    # Load env for the auth session
    export $(grep -v '^#' "${INSTALL_DIR}/.env" | xargs)

    # ── Start VPN if enabled (before Telegram auth) ────────────
    if [[ "${VPN_ENABLED_VAL:-false}" == "true" ]]; then
        echo ""
        echo -e "  ${CYAN}VPN is enabled — connecting before Telegram auth...${RESET}"

        if ! command -v openvpn &>/dev/null; then
            err "OpenVPN not found. Install it first: sudo apt install openvpn"
            exit 1
        fi

        if [[ ! -f "${VPN_CONFIG_VAL}" ]]; then
            err "VPN config file not found: ${VPN_CONFIG_VAL}"
            exit 1
        fi

        # Start OpenVPN in background with split tunneling
        # Only route Telegram IPs through VPN, not all traffic
        VPN_LOG="/tmp/openvpn-setup-$$.log"
        VPN_CMD="sudo openvpn --config ${VPN_CONFIG_VAL} --daemon --log ${VPN_LOG}"
        VPN_CMD="${VPN_CMD} --route-nopull"  # Don't use server's default routes
        VPN_CMD="${VPN_CMD} --route 149.154.160.0 255.255.240.0"  # Telegram DC1-DC5
        VPN_CMD="${VPN_CMD} --route 91.108.4.0 255.255.252.0"     # Telegram additional
        VPN_CMD="${VPN_CMD} --route 91.108.56.0 255.255.252.0"    # Telegram additional
        if [[ -n "${VPN_AUTH_VAL}" && -f "${VPN_AUTH_VAL}" ]]; then
            VPN_CMD="${VPN_CMD} --auth-user-pass ${VPN_AUTH_VAL}"
        fi

        echo "  Starting VPN (split tunnel - Telegram traffic only)..."
        echo "  Your server's other traffic will NOT use VPN"
        eval "${VPN_CMD}" 2>&1 | grep -v "dbus-org.freedesktop.resolve1" || true

        sleep 2  # Give OpenVPN time to initialize

        # Check if process started
        if ! pgrep -f "openvpn.*${VPN_CONFIG_VAL}" >/dev/null; then
            err "OpenVPN process failed to start"
            echo "  Check log: ${VPN_LOG}"
            exit 1
        fi

        # Wait for tun interface to come up (max 60 seconds)
        echo -n "  Waiting for VPN tunnel"
        VPN_CONNECTED=false
        for i in {1..60}; do
            if ip link show 2>/dev/null | grep -q "tun\|tap"; then
                echo ""
                ok "VPN tunnel interface detected"
                sleep 3  # Give routing tables time to update

                # Verify we can reach the internet through VPN
                echo -n "  Testing VPN connectivity"
                if timeout 10 curl -s --max-time 5 https://api.telegram.org >/dev/null 2>&1; then
                    echo ""
                    ok "VPN connected and Telegram reachable ✓"
                    VPN_CONNECTED=true
                    break
                else
                    echo " (tunnel up but no connectivity yet)"
                fi
            fi
            echo -n "."
            sleep 1
            if [[ $i -eq 60 ]]; then
                echo ""
                err "VPN tunnel not ready after 60s"
                echo ""
                echo -e "${YELLOW}Troubleshooting:${RESET}"
                echo "  1. Check VPN logs: sudo journalctl -xe | grep openvpn"
                echo "  2. Test manually: sudo openvpn --config ${VPN_CONFIG_VAL}"
                echo "  3. Verify credentials in ${VPN_AUTH_VAL}"
                echo ""
                read -rp "Continue without VPN? [y/N]: " CONTINUE_NO_VPN
                if [[ "${CONTINUE_NO_VPN,,}" != "y" ]]; then
                    exit 1
                fi
            fi
        done
        echo ""

        if [[ "$VPN_CONNECTED" == "false" ]]; then
            warn "Proceeding without confirmed VPN connection"
        fi
    fi

    python3 - << 'AUTHEOF'
import asyncio, os, sys
from telethon import TelegramClient

async def auth():
    # Increase timeouts for VPN connections
    client = TelegramClient(
        os.getenv("SESSION_PATH", "./data/userbot_session"),
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
        connection_retries=10,
        retry_delay=5,
        timeout=30
    )
    await client.start()
    me = await client.get_me()
    print(f"\n  ✓  Logged in as: {me.first_name} (id={me.id})")
    await client.disconnect()

asyncio.run(auth())
AUTHEOF

    # ── Stop VPN if we started it ──────────────────────────────
    if [[ "${VPN_ENABLED_VAL:-false}" == "true" ]]; then
        echo ""
        echo -e "  ${CYAN}Stopping VPN (auth complete)...${RESET}"
        sudo pkill -f "openvpn.*${VPN_CONFIG_VAL}" 2>/dev/null || true
        sleep 1
        ok "VPN stopped"
    fi

    ok "Telethon session saved — no login needed on future starts"
else
    warn "Skipped — bot will ask for phone + OTP on first run"
fi


# ════════════════════════════════════════════════════════
#  DONE
# ════════════════════════════════════════════════════════
hdr "All done!"

echo -e "${BOLD}How to start the bot:${RESET}"
echo ""

if systemctl is-enabled tgbot &>/dev/null 2>&1; then
    echo -e "  ${CYAN}sudo systemctl start tgbot${RESET}     ← start now"
    echo -e "  ${CYAN}sudo systemctl status tgbot${RESET}    ← check status"
    echo -e "  ${CYAN}sudo journalctl -u tgbot -f${RESET}    ← live logs"
else
    echo -e "  ${CYAN}python3 tg_downloader_bot.py${RESET}   ← run directly"
    echo ""
    echo "  Or install the service:"
    echo -e "  ${CYAN}sudo cp tgbot.service /etc/systemd/system/${RESET}"
    echo -e "  ${CYAN}sudo systemctl enable --now tgbot${RESET}"
fi

echo ""
echo -e "  Config : ${INSTALL_DIR}/.env"
echo -e "  Logs   : ${INSTALL_DIR}/logs/bot.log"
echo -e "  Media  : ${MEDIA_PATH_VAL:-${INSTALL_DIR}/media}"
echo ""
echo -e "  ${YELLOW}⚠  Keep .env private — it contains your credentials.${RESET}"
echo ""
