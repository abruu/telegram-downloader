#!/bin/bash
# ================================================================
#   Telegram Media Bot — Uninstall Script
#   Removes all bot dependencies, services, and optionally data
# ================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✓${RESET}  $1"; }
warn() { echo -e "${YELLOW}⚠${RESET}  $1"; }
err()  { echo -e "${RED}✗${RESET}  $1"; }
hdr()  { echo -e "\n${BOLD}${CYAN}━━━  $1  ━━━${RESET}\n"; }

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

clear
echo -e "${BOLD}${RED}"
cat << 'BANNER'
  ╔══════════════════════════════════════════════════════╗
  ║       Telegram Media Bot — Uninstall                 ║
  ║                                                      ║
  ║  This will remove:                                   ║
  ║   • Python dependencies (telethon, telegram, etc.)   ║
  ║   • Systemd service (if installed)                   ║
  ║   • Bot data and downloads (optional)                ║
  ╚══════════════════════════════════════════════════════╝
BANNER
echo -e "${RESET}"

echo -e "${YELLOW}Python itself will NOT be removed.${RESET}"
echo ""
read -rp "Continue with uninstall? [y/N]: " CONFIRM
if [[ "${CONFIRM,,}" != "y" ]]; then
    echo "Cancelled."
    exit 0
fi


# ════════════════════════════════════════════════════════
#  STEP 1 — Stop and remove systemd service
# ════════════════════════════════════════════════════════
hdr "Step 1 — Systemd service"

if systemctl is-active tgbot &>/dev/null; then
    echo "  Stopping tgbot service..."
    sudo systemctl stop tgbot && ok "Service stopped"
fi

if systemctl is-enabled tgbot &>/dev/null 2>&1; then
    echo "  Disabling tgbot service..."
    sudo systemctl disable tgbot && ok "Service disabled"
fi

if [[ -f /etc/systemd/system/tgbot.service ]]; then
    sudo rm -f /etc/systemd/system/tgbot.service
    sudo systemctl daemon-reload
    ok "Service file removed"
else
    ok "No systemd service found"
fi


# ════════════════════════════════════════════════════════
#  STEP 2 — Remove Python dependencies
# ════════════════════════════════════════════════════════
hdr "Step 2 — Python dependencies"

echo "  Removing Telegram bot packages..."
pip3 uninstall -y \
    telethon \
    python-telegram-bot \
    aiohttp \
    aiofiles \
    python-dotenv \
    fuzzywuzzy \
    python-Levenshtein \
    2>/dev/null || true

ok "Python packages removed"


# ════════════════════════════════════════════════════════
#  STEP 3 — Remove OpenVPN (optional)
# ════════════════════════════════════════════════════════
# hdr "Step 3 — OpenVPN"

# if command -v openvpn &>/dev/null; then
#     echo ""
#     echo -e "${YELLOW}OpenVPN is installed on this system.${RESET}"
#     read -rp "  Remove OpenVPN? [y/N]: " REMOVE_OPENVPN
#     if [[ "${REMOVE_OPENVPN,,}" == "y" ]]; then
#         sudo apt-get remove -y openvpn 2>/dev/null && ok "OpenVPN removed"
#     else
#         ok "OpenVPN kept"
#     fi
# else
#     ok "OpenVPN not installed"
# fi


# ════════════════════════════════════════════════════════
#  STEP 4 — Remove bot data (optional)
# ════════════════════════════════════════════════════════
hdr "Step 4 — Bot data and downloads"

echo ""
echo -e "${YELLOW}The following directories contain bot data:${RESET}"
echo "  • ${INSTALL_DIR}/data/        (databases, session, user list)"
echo "  • ${INSTALL_DIR}/logs/        (log files)"
echo "  • ${INSTALL_DIR}/downloads/   (temporary downloads)"
echo ""

# Check if .env exists to read MEDIA_PATH
MEDIA_PATH_VAL=""
if [[ -f "${INSTALL_DIR}/.env" ]]; then
    source "${INSTALL_DIR}/.env" 2>/dev/null || true
    MEDIA_PATH_VAL="${MEDIA_PATH:-}"
fi

if [[ -n "$MEDIA_PATH_VAL" && -d "$MEDIA_PATH_VAL" ]]; then
    echo -e "${YELLOW}Media library found at: ${MEDIA_PATH_VAL}${RESET}"
    echo ""
fi

read -rp "Remove bot data (data/, logs/, downloads/)? [y/N]: " REMOVE_DATA
if [[ "${REMOVE_DATA,,}" == "y" ]]; then
    rm -rf "${INSTALL_DIR}/data" && ok "data/ removed"
    rm -rf "${INSTALL_DIR}/logs" && ok "logs/ removed"
    rm -rf "${INSTALL_DIR}/downloads" && ok "downloads/ removed"
else
    ok "Bot data kept"
fi

if [[ -n "$MEDIA_PATH_VAL" && -d "$MEDIA_PATH_VAL" ]]; then
    echo ""
    read -rp "Remove media library (${MEDIA_PATH_VAL})? [y/N]: " REMOVE_MEDIA
    if [[ "${REMOVE_MEDIA,,}" == "y" ]]; then
        rm -rf "$MEDIA_PATH_VAL" && ok "Media library removed"
    else
        ok "Media library kept"
    fi
fi


# ════════════════════════════════════════════════════════
#  STEP 5 — Remove configuration
# ════════════════════════════════════════════════════════
hdr "Step 5 — Configuration"

if [[ -f "${INSTALL_DIR}/.env" ]]; then
    read -rp "Remove .env configuration file? [y/N]: " REMOVE_ENV
    if [[ "${REMOVE_ENV,,}" == "y" ]]; then
        rm -f "${INSTALL_DIR}/.env" && ok ".env removed"
    else
        ok ".env kept"
    fi
else
    ok "No .env file found"
fi

if [[ -d "${INSTALL_DIR}/config" ]]; then
    rm -rf "${INSTALL_DIR}/config" && ok "config/ removed"
fi

if [[ -f "${INSTALL_DIR}/tgbot.service" ]]; then
    rm -f "${INSTALL_DIR}/tgbot.service" && ok "tgbot.service removed"
fi


# ════════════════════════════════════════════════════════
#  STEP 6 — Remove bot scripts (optional)
# ════════════════════════════════════════════════════════
hdr "Step 6 — Bot source files"

echo ""
echo -e "${YELLOW}Remove bot source files?${RESET}"
echo "  • tg_downloader_bot.py"
echo "  • media_classifier.py"
echo "  • setup.sh, uninstall.sh"
echo ""
read -rp "Remove all bot source files? [y/N]: " REMOVE_SOURCE
if [[ "${REMOVE_SOURCE,,}" == "y" ]]; then
    rm -f "${INSTALL_DIR}/tg_downloader_bot.py" && ok "tg_downloader_bot.py removed"
    rm -f "${INSTALL_DIR}/media_classifier.py" && ok "media_classifier.py removed"
    rm -f "${INSTALL_DIR}/setup.sh" && ok "setup.sh removed"
    rm -f "${INSTALL_DIR}/uninstall.sh" && ok "uninstall.sh removed"
    rm -f "${INSTALL_DIR}/requirements.txt" && ok "requirements.txt removed"
    rm -f "${INSTALL_DIR}/.env.example" && ok ".env.example removed"
    rm -f "${INSTALL_DIR}/.gitignore" && ok ".gitignore removed"
    rm -f "${INSTALL_DIR}/README.md" && ok "README.md removed"
    rm -f "${INSTALL_DIR}/LICENSE" && ok "LICENSE removed"

    # Remove the directory if it's empty
    if [[ -z "$(ls -A "${INSTALL_DIR}")" ]]; then
        cd ..
        rmdir "${INSTALL_DIR}" && ok "Empty directory removed: ${INSTALL_DIR}"
    else
        warn "Directory not empty — kept: ${INSTALL_DIR}"
    fi
else
    ok "Source files kept"
fi


# ════════════════════════════════════════════════════════
#  DONE
# ════════════════════════════════════════════════════════
hdr "Uninstall complete!"

echo ""
echo -e "${GREEN}The Telegram Media Bot has been uninstalled.${RESET}"
echo ""
echo -e "${BOLD}What was removed:${RESET}"
echo "  ✓  Python dependencies (telethon, python-telegram-bot, etc.)"
echo "  ✓  Systemd service (if installed)"
# [[ "${REMOVE_OPENVPN,,}" == "y" ]] && echo "  ✓  OpenVPN"
[[ "${REMOVE_DATA,,}" == "y" ]] && echo "  ✓  Bot data (databases, logs, downloads)"
[[ "${REMOVE_MEDIA,,}" == "y" ]] && echo "  ✓  Media library"
[[ "${REMOVE_ENV,,}" == "y" ]] && echo "  ✓  Configuration (.env)"
[[ "${REMOVE_SOURCE,,}" == "y" ]] && echo "  ✓  Bot source files"
echo ""
echo -e "${YELLOW}Python itself was NOT removed.${RESET}"
echo ""
