#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
#  idontPG-backup — One-command Installer
# ══════════════════════════════════════════════════════════════════════════════
#  Developer  : durwinam
#  Maintainer : durwinam
#  GitHub     : https://github.com/durwinam/idontPG-backup
#  License    : MIT
#
#  Installs:
#    - idontPG-backup CLI
#    - idont-backup alias
#    - Web Panel
#    - Web Panel Scheduler
#
#  The installed CLI supports:
#      idont-backup
#      idont-backup --set
#      idont-backup update
#
#  Web Panel:
#      http://SERVER_IP:5000
# ══════════════════════════════════════════════════════════════════════════════

set -e

RED='\e[1;31m'
GREEN='\e[2;32m'
YELLOW='\e[1;33m'
NC='\e[0m'

REPO="durwinam/idontPG-backup"
RAW_BASE="https://raw.githubusercontent.com/${REPO}"

INSTALL_PATH="/usr/local/bin/idontPG-backup"
ALIAS_PATH="/usr/local/bin/idont-backup"
TMP_PATH="/tmp/pg_backup.py"

WEB_PANEL_PATH="/usr/local/bin/idontPG-backup-web.py"
WEB_TMP="/tmp/idontpg-web-panel.py"

WEB_ASSET_DIR="/usr/local/share/idontPG-backup"
WEB_LOGO_PATH="${WEB_ASSET_DIR}/logo.png"

WEB_PANEL_URL="${RAW_BASE}/main/web_panel.py"
WEB_LOGO_URL="${RAW_BASE}/main/web/static/logo.png"
WEB_PG_LOGO_URL="${RAW_BASE}/main/web/static/pasarguard-logo.png"

# When this installer is run from the distributed ZIP, prefer the bundled
# files. This makes the release self-contained and prevents an unrelated
# GitHub 'main' update from replacing the exact version being installed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CORE="${SCRIPT_DIR}/pg_backup.py"
LOCAL_WEB_PANEL="${SCRIPT_DIR}/web_panel.py"
LOCAL_LOGO="${SCRIPT_DIR}/web/static/logo.png"
LOCAL_PG_LOGO="${SCRIPT_DIR}/web/static/pasarguard-logo.png"

DEVELOPER="durwinam"

VERSION_TAG="${1:-}"

# ──────────────────────────────────────────────────────────────────────────────
# Root check
# ──────────────────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}[-] Please run this installer as root.${NC}"
    exit 1
fi

echo
echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║             idontPG-backup Installer               ║${NC}"
echo -e "${GREEN}║                    durwinam                         ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo

# ──────────────────────────────────────────────────────────────────────────────
# Version argument
#
# Examples:
#   bash install.sh
#   bash install.sh 5.5.0
#   bash install.sh v5.5.0
# ──────────────────────────────────────────────────────────────────────────────

if [ -z "${VERSION_TAG}" ] && [[ "${0}" =~ ^[vV]?[0-9]+(\.[0-9]+){1,2}$ ]]; then
    VERSION_TAG="${0}"
fi

if [ -n "${VERSION_TAG}" ]; then
    VERSION_TAG="${VERSION_TAG#v}"
    VERSION_TAG="${VERSION_TAG#V}"
    VERSION_TAG="v${VERSION_TAG}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Install system packages
# ──────────────────────────────────────────────────────────────────────────────

echo -e "${GREEN}[*] Checking system packages...${NC}"

if command -v apt-get >/dev/null 2>&1; then

    apt-get update -y >/dev/null 2>&1 || true

    apt-get install -y \
        python3 \
        python3-pip \
        curl \
        unzip \
        ca-certificates \
        >/dev/null 2>&1 || true

fi

# ──────────────────────────────────────────────────────────────────────────────
# Python dependencies
# ──────────────────────────────────────────────────────────────────────────────

echo -e "${GREEN}[*] Installing Python dependencies...${NC}"

pip3 install \
    --break-system-packages \
    requests \
    urllib3 \
    paramiko \
    pysocks \
    grpcio \
    >/dev/null 2>&1 || \
pip3 install \
    requests \
    urllib3 \
    paramiko \
    pysocks \
    grpcio \
    >/dev/null 2>&1 || true

# ──────────────────────────────────────────────────────────────────────────────
# Download CLI
# ──────────────────────────────────────────────────────────────────────────────

echo -e "${GREEN}[*] Downloading idontPG-backup...${NC}"

if [ -n "${VERSION_TAG}" ]; then

    SOURCE="${RAW_BASE}/${VERSION_TAG}/pg_backup.py"

    echo -e "${GREEN}[*] Source: ${VERSION_TAG}${NC}"

else

    SOURCE="${RAW_BASE}/main/pg_backup.py?cache=$(date +%s)"

    echo -e "${GREEN}[*] Source: latest main branch${NC}"

fi

rm -f "${TMP_PATH}"

if [ -s "${LOCAL_CORE}" ]; then
    echo -e "${GREEN}[*] Source: bundled pg_backup.py${NC}"
    cp "${LOCAL_CORE}" "${TMP_PATH}"
else
    if ! curl \
        --fail \
        --silent \
        --show-error \
        --location \
        --retry 3 \
        --connect-timeout 15 \
        "${SOURCE}" \
        -o "${TMP_PATH}"; then

        echo -e "${RED}[-] Failed to download pg_backup.py${NC}"
        echo -e "${RED}[-] URL: ${SOURCE}${NC}"
        exit 1
    fi
fi

if [ ! -s "${TMP_PATH}" ]; then
    echo -e "${RED}[-] Downloaded pg_backup.py is empty.${NC}"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Validate Python before installation
# ──────────────────────────────────────────────────────────────────────────────

echo -e "${GREEN}[*] Validating CLI Python file...${NC}"

if ! python3 -m py_compile "${TMP_PATH}" >/dev/null 2>&1; then

    echo -e "${RED}[-] pg_backup.py failed Python syntax validation.${NC}"
    echo -e "${RED}[-] Installation aborted. Existing installation was not replaced.${NC}"

    rm -f "${TMP_PATH}"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Read installed version
# ──────────────────────────────────────────────────────────────────────────────

INSTALLED_VERSION="unknown"

INSTALLED_VERSION=$(
    grep -m1 -oE \
    'v[0-9]+\.[0-9]+(\.[0-9]+)?' \
    "${TMP_PATH}" 2>/dev/null \
    | head -1 \
    | sed 's/^v//'
) || true

if [ -z "${INSTALLED_VERSION}" ]; then
    INSTALLED_VERSION="unknown"
fi

echo -e "${GREEN}[+] CLI version: v${INSTALLED_VERSION}${NC}"

# ──────────────────────────────────────────────────────────────────────────────
# Install CLI
# ──────────────────────────────────────────────────────────────────────────────

if ! head -n 1 "${TMP_PATH}" | grep -q "python"; then

    {
        echo '#!/usr/bin/env python3'
        cat "${TMP_PATH}"
    } > "${TMP_PATH}.fixed"

    mv "${TMP_PATH}.fixed" "${TMP_PATH}"

fi

install -m 700 "${TMP_PATH}" "${INSTALL_PATH}"

rm -f "${TMP_PATH}"

# ──────────────────────────────────────────────────────────────────────────────
# CLI alias
# ──────────────────────────────────────────────────────────────────────────────

ln -sfn "${INSTALL_PATH}" "${ALIAS_PATH}"

chmod 700 "${INSTALL_PATH}"
chmod 700 "${ALIAS_PATH}"

echo -e "${GREEN}[+] CLI installed:${NC} ${INSTALL_PATH}"
echo -e "${GREEN}[+] Command:${NC} idont-backup"
echo -e "${GREEN}[+] Update command:${NC} idont-backup update"
echo -e "${GREEN}[+] Settings command:${NC} idont-backup --set"

# ──────────────────────────────────────────────────────────────────────────────
# Web Panel
# ──────────────────────────────────────────────────────────────────────────────

echo
echo -e "${GREEN}[*] Installing Web Panel...${NC}"

rm -f "${WEB_TMP}"

# Prefer the exact files bundled in the release ZIP. Fall back to GitHub only
# when a bundled file is genuinely missing. Keep validation/installation
# outside the source-selection branch so bundled ZIP installs work correctly.
if [ -s "${LOCAL_WEB_PANEL}" ]; then
    echo -e "${GREEN}[*] Source: bundled web_panel.py${NC}"
    cp "${LOCAL_WEB_PANEL}" "${WEB_TMP}"
elif curl \
    --fail \
    --silent \
    --show-error \
    --location \
    --retry 3 \
    --connect-timeout 15 \
    "${WEB_PANEL_URL}?cache=$(date +%s)" \
    -o "${WEB_TMP}" \
    && [ -s "${WEB_TMP}" ]; then
    echo -e "${GREEN}[*] Source: GitHub web_panel.py${NC}"
else
    echo -e "${RED}[!] Failed to obtain web_panel.py${NC}"
    echo -e "${YELLOW}[!] CLI installation remains intact.${NC}"
    rm -f "${WEB_TMP}"
    exit 1
fi

# Validate Web Panel Python before replacing the installed copy.
if ! python3 -m py_compile "${WEB_TMP}" >/dev/null 2>&1; then
    echo -e "${RED}[!] Web Panel Python validation failed.${NC}"
    echo -e "${YELLOW}[!] Existing Web Panel was not replaced.${NC}"
    rm -f "${WEB_TMP}"
    exit 1
fi

install -m 700 "${WEB_TMP}" "${WEB_PANEL_PATH}"
rm -f "${WEB_TMP}"

mkdir -p "${WEB_ASSET_DIR}"
chmod 755 "${WEB_ASSET_DIR}"

# idontPG branding logo
if [ -s "${LOCAL_LOGO}" ]; then
    cp "${LOCAL_LOGO}" "${WEB_LOGO_PATH}"
    chmod 644 "${WEB_LOGO_PATH}"
    echo -e "${GREEN}[+] Bundled Web Panel logo installed.${NC}"
elif curl \
    --fail --silent --show-error --location --retry 3 --connect-timeout 15 \
    "${WEB_LOGO_URL}?cache=$(date +%s)" -o "${WEB_LOGO_PATH}" \
    && [ -s "${WEB_LOGO_PATH}" ]; then
    chmod 644 "${WEB_LOGO_PATH}"
    echo -e "${GREEN}[+] Web Panel logo installed.${NC}"
else
    rm -f "${WEB_LOGO_PATH}"
    echo -e "${YELLOW}[!] idontPG logo unavailable; fallback branding will be used.${NC}"
fi

# PasarGuard official lion mark — kept separate from idontPG branding.
PG_LOGO_PATH="${WEB_ASSET_DIR}/pasarguard-logo.png"
if [ -s "${LOCAL_PG_LOGO}" ]; then
    cp "${LOCAL_PG_LOGO}" "${PG_LOGO_PATH}"
    chmod 644 "${PG_LOGO_PATH}"
    echo -e "${GREEN}[+] Bundled PasarGuard logo installed.${NC}"
elif curl \
    --fail --silent --show-error --location --retry 3 --connect-timeout 15 \
    "${WEB_PG_LOGO_URL}?cache=$(date +%s)" -o "${PG_LOGO_PATH}" \
    && [ -s "${PG_LOGO_PATH}" ]; then
    chmod 644 "${PG_LOGO_PATH}"
    echo -e "${GREEN}[+] PasarGuard logo installed.${NC}"
else
    rm -f "${PG_LOGO_PATH}"
    echo -e "${YELLOW}[!] PasarGuard logo unavailable; fallback branding will be used.${NC}"
fi

        # ──────────────────────────────────────────────────────────────────────
        # Web Panel service
        # ──────────────────────────────────────────────────────────────────────

        cat > /etc/systemd/system/idontpg-backup-web.service <<EOF
[Unit]
Description=idontPG-backup Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${WEB_PANEL_PATH}
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

        chmod 600 /etc/systemd/system/idontpg-backup-web.service

        # ──────────────────────────────────────────────────────────────────────
        # Web Scheduler service
        # ──────────────────────────────────────────────────────────────────────

        cat > /etc/systemd/system/idontpg-backup-web-scheduler.service <<EOF
[Unit]
Description=idontPG-backup Web Scheduler
After=network-online.target docker.service idontpg-backup-web.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${WEB_PANEL_PATH} --worker
Restart=always
RestartSec=15
User=root

[Install]
WantedBy=multi-user.target
EOF

        chmod 600 /etc/systemd/system/idontpg-backup-web-scheduler.service

        # ──────────────────────────────────────────────────────────────────────
        # Reload systemd
        # ──────────────────────────────────────────────────────────────────────

        echo -e "${GREEN}[*] Reloading systemd...${NC}"

        systemctl daemon-reload

        # ──────────────────────────────────────────────────────────────────────
        # IMPORTANT:
        # Enable AND start BOTH services.
        #
        # Previous installer only enabled the Web Panel.
        # This version also enables the Scheduler Worker.
        # ──────────────────────────────────────────────────────────────────────

        echo -e "${GREEN}[*] Starting Web Panel...${NC}"

        if systemctl enable --now idontpg-backup-web.service >/dev/null 2>&1; then
            echo -e "${GREEN}[+] Web Panel is running.${NC}"
        else
            echo -e "${RED}[!] Web Panel failed to start.${NC}"
            systemctl status idontpg-backup-web.service --no-pager || true
        fi

        echo -e "${GREEN}[*] Starting Web Scheduler...${NC}"

        if systemctl enable --now idontpg-backup-web-scheduler.service >/dev/null 2>&1; then
            echo -e "${GREEN}[+] Web Scheduler is running.${NC}"
        else
            echo -e "${RED}[!] Web Scheduler failed to start.${NC}"
            systemctl status idontpg-backup-web-scheduler.service --no-pager || true
        fi

        echo
        echo -e "${GREEN}[+] Web Panel:${NC} http://SERVER_IP:5000"

# ──────────────────────────────────────────────────────────────────────────────
# Final verification
# ──────────────────────────────────────────────────────────────────────────────

echo
echo -e "${GREEN}[*] Running final checks...${NC}"

if [ -x "${INSTALL_PATH}" ]; then
    echo -e "${GREEN}[✓] CLI: OK${NC}"
else
    echo -e "${RED}[✗] CLI: FAILED${NC}"
fi

if [ -x "${WEB_PANEL_PATH}" ]; then
    echo -e "${GREEN}[✓] Web Panel file: OK${NC}"
else
    echo -e "${YELLOW}[!] Web Panel file: not installed${NC}"
fi

if systemctl is-active --quiet idontpg-backup-web.service; then
    echo -e "${GREEN}[✓] Web Panel service: RUNNING${NC}"
else
    echo -e "${YELLOW}[!] Web Panel service: NOT RUNNING${NC}"
fi

if systemctl is-active --quiet idontpg-backup-web-scheduler.service; then
    echo -e "${GREEN}[✓] Web Scheduler: RUNNING${NC}"
else
    echo -e "${YELLOW}[!] Web Scheduler: NOT RUNNING${NC}"
fi

echo
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} Installation completed successfully.${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo
echo -e "  CLI:"
echo -e "    ${GREEN}idont-backup${NC}"
echo
echo -e "  Settings:"
echo -e "    ${GREEN}idont-backup --set${NC}"
echo
echo -e "  Update:"
echo -e "    ${GREEN}idont-backup update${NC}"
echo
echo -e "  Web Panel:"
echo -e "    ${GREEN}http://SERVER_IP:5000${NC}"
echo
echo -e "  Web Scheduler:"
echo -e "    ${GREEN}systemctl status idontpg-backup-web-scheduler${NC}"
echo
echo -e "${GREEN}Developer: durwinam${NC}"
echo
echo -e "${YELLOW}[*] Launching idontPG-backup...${NC}"
echo

exec "${INSTALL_PATH}"
