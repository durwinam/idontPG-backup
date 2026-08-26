#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# idontPG-backup — One-command Installer
# Developer / Maintainer: durwinam
# GitHub: https://github.com/durwinam/idontPG-backup
# Version: v5.4.2
# ══════════════════════════════════════════════════════════════════════════════

set -Eeuo pipefail

RED='\e[1;31m'
GREEN='\e[1;32m'
YELLOW='\e[1;33m'
NC='\e[0m'

REPO="durwinam/idontPG-backup"
RAW_BASE="https://raw.githubusercontent.com/${REPO}"

INSTALL_PATH="/usr/local/bin/idontPG-backup"
CLI_ALIAS="/usr/local/bin/idont-backup"

WEB_PANEL_PATH="/usr/local/bin/idontPG-backup-web.py"
WEB_ASSET_DIR="/usr/local/share/idontPG-backup"
WEB_LOGO_PATH="${WEB_ASSET_DIR}/logo.png"

WEB_PORT="${IDONTPG_PORT:-5000}"
DEVELOPER="durwinam"

TMP_PATH="$(mktemp /tmp/idontpg-backup.XXXXXX.py)"
WEB_TMP="$(mktemp /tmp/idontpg-web.XXXXXX.py)"

cleanup() {
    rm -f "${TMP_PATH}" "${WEB_TMP}" "${TMP_PATH}.new" 2>/dev/null || true
}
trap cleanup EXIT

VERSION_TAG="${1:-}"
OFFLINE=0

for arg in "$@"; do
    [ "${arg}" = "--offline" ] && OFFLINE=1
done

# ── Version validation ───────────────────────────────────────────────────────

if [ -n "${VERSION_TAG}" ] && [ "${VERSION_TAG}" = "--offline" ]; then
    VERSION_TAG=""
fi

if [ -n "${VERSION_TAG}" ] && \
   [[ ! "${VERSION_TAG}" =~ ^[vV]?[0-9]+(\.[0-9]+){1,2}$ ]]; then

    echo -e "${RED}[-] Invalid version: ${VERSION_TAG}${NC}"
    echo "    Example:"
    echo "    bash install.sh 5.4.2"
    exit 1
fi

if [ -n "${VERSION_TAG}" ] && [[ ! "${VERSION_TAG}" =~ ^v ]]; then
    VERSION_TAG="v${VERSION_TAG}"
fi

# ── Root check ───────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}[-] Please run this installer as root.${NC}"
    exit 1
fi

echo -e "${GREEN}[*] Installing idontPG-backup...${NC}"

# ── Dependencies ─────────────────────────────────────────────────────────────

if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null 2>&1 || true

    apt-get install -y \
        python3 \
        python3-pip \
        curl \
        unzip \
        >/dev/null 2>&1 || true
fi

python3 -m pip install \
    --break-system-packages \
    requests urllib3 paramiko \
    >/dev/null 2>&1 || \
python3 -m pip install \
    requests urllib3 paramiko \
    >/dev/null 2>&1 || true

# ── Select source ────────────────────────────────────────────────────────────

if [ -n "${VERSION_TAG}" ]; then

    REF="${VERSION_TAG}"

    echo -e "${GREEN}[*] Source: pinned version ${REF}${NC}"

elif [ "${OFFLINE}" -eq 1 ]; then

    LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

    if [ ! -f "${LOCAL_DIR}/pg_backup.py" ]; then
        echo -e "${RED}[-] --offline was specified but pg_backup.py was not found.${NC}"
        exit 1
    fi

    cp "${LOCAL_DIR}/pg_backup.py" "${TMP_PATH}"

    REF=""

    echo -e "${GREEN}[*] Source: local offline copy${NC}"

else

    REF="main"

    echo -e "${GREEN}[*] Source: GitHub main branch${NC}"

fi

# ── Download pg_backup.py ────────────────────────────────────────────────────

if [ -n "${REF}" ]; then

    SOURCE="${RAW_BASE}/${REF}/pg_backup.py?cachebust=$(date +%s)"

    if ! curl -fsSL \
        --retry 3 \
        --connect-timeout 15 \
        "${SOURCE}" \
        -o "${TMP_PATH}"; then

        echo -e "${RED}[-] Failed to download pg_backup.py${NC}"
        echo "    ${SOURCE}"
        exit 1
    fi

fi

if [ ! -s "${TMP_PATH}" ]; then
    echo -e "${RED}[-] pg_backup.py is empty or missing.${NC}"
    exit 1
fi

# ── Basic validation ─────────────────────────────────────────────────────────

if ! head -n 30 "${TMP_PATH}" | grep -Eq \
    'python|idontPG-backup|pg_backup'; then

    echo -e "${RED}[-] Downloaded pg_backup.py does not look valid.${NC}"
    exit 1
fi

# ── Restore Python shebang ───────────────────────────────────────────────────

if ! head -n 1 "${TMP_PATH}" | grep -q "python"; then

    {
        echo '#!/usr/bin/env python3'
        cat "${TMP_PATH}"
    } > "${TMP_PATH}.new"

    mv "${TMP_PATH}.new" "${TMP_PATH}"
fi

# ── Python syntax check ──────────────────────────────────────────────────────

if ! python3 -m py_compile "${TMP_PATH}"; then

    echo -e "${RED}[-] Python syntax check failed.${NC}"
    echo -e "${RED}[-] Existing installation was NOT replaced.${NC}"
    exit 1
fi

# ── Detect version ───────────────────────────────────────────────────────────

INSTALLED_VERSION="$(
    grep -m1 -oE 'v[0-9]+\.[0-9]+(\.[0-9]+)?' \
        "${TMP_PATH}" 2>/dev/null \
        | head -1 \
        | sed 's/^v//' || true
)"

if [ -z "${INSTALLED_VERSION}" ]; then
    INSTALLED_VERSION="unknown"
fi

echo -e "${GREEN}[+] Detected version: v${INSTALLED_VERSION}${NC}"

# ── Install CLI atomically ───────────────────────────────────────────────────

install -m 700 \
    "${TMP_PATH}" \
    "${INSTALL_PATH}"

ln -sfn \
    "${INSTALL_PATH}" \
    "${CLI_ALIAS}"

echo -e "${GREEN}[+] Installed: ${INSTALL_PATH}${NC}"
echo -e "${GREEN}[+] CLI command: idont-backup${NC}"

# ══════════════════════════════════════════════════════════════════════════════
# Web Panel
# ══════════════════════════════════════════════════════════════════════════════

WEB_REF="${REF:-main}"

echo
echo -e "${GREEN}[*] Installing Web Panel...${NC}"
echo -e "${GREEN}[*] Web Panel source: ${WEB_REF}${NC}"

WEB_PANEL_URL="${RAW_BASE}/${WEB_REF}/web_panel.py"
WEB_LOGO_URL="${RAW_BASE}/${WEB_REF}/web/static/logo.png"

if curl -fsSL \
    --retry 3 \
    --connect-timeout 15 \
    "${WEB_PANEL_URL}?cachebust=$(date +%s)" \
    -o "${WEB_TMP}" && \
    [ -s "${WEB_TMP}" ]; then

    # Validate web panel before replacing the current one.

    if python3 -m py_compile "${WEB_TMP}"; then

        install -m 700 \
            "${WEB_TMP}" \
            "${WEB_PANEL_PATH}"

        mkdir -p "${WEB_ASSET_DIR}"

        if curl -fsSL \
            --retry 3 \
            --connect-timeout 15 \
            "${WEB_LOGO_URL}?cachebust=$(date +%s)" \
            -o "${WEB_LOGO_PATH}" && \
            [ -s "${WEB_LOGO_PATH}" ]; then

            chmod 644 "${WEB_LOGO_PATH}"

            echo -e "${GREEN}[+] Web Panel logo installed.${NC}"

        else

            rm -f "${WEB_LOGO_PATH}"

            echo -e "${YELLOW}[!] Logo download failed.${NC}"

        fi

        # ── Web Panel service ────────────────────────────────────────────────

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
Environment=IDONTPG_PORT=${WEB_PORT}

[Install]
WantedBy=multi-user.target
EOF

        # ── Automatic Backup Scheduler ──────────────────────────────────────

        cat > /etc/systemd/system/idontpg-backup-web-scheduler.service <<EOF
[Unit]
Description=idontPG-backup Automatic Web Scheduler
After=network-online.target docker.service idontpg-backup-web.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${WEB_PANEL_PATH} --worker
Restart=always
RestartSec=15
User=root
Environment=IDONTPG_PORT=${WEB_PORT}

[Install]
WantedBy=multi-user.target
EOF

        systemctl daemon-reload

        # IMPORTANT:
        # Both the web panel and scheduler are enabled permanently
        # and started immediately.

        systemctl enable idontpg-backup-web.service >/dev/null 2>&1 || true
        systemctl enable idontpg-backup-web-scheduler.service >/dev/null 2>&1 || true

        systemctl restart idontpg-backup-web.service
        systemctl restart idontpg-backup-web-scheduler.service

        echo -e "${GREEN}[+] Web Panel installed.${NC}"
        echo -e "${GREEN}[+] Web Panel: http://SERVER_IP:${WEB_PORT}${NC}"
        echo -e "${GREEN}[+] Automatic Scheduler: ENABLED${NC}"
        echo -e "${GREEN}[+] Automatic Scheduler: RUNNING${NC}"

    else

        echo -e "${RED}[!] Web Panel syntax check failed.${NC}"
        echo -e "${YELLOW}[!] Existing Web Panel was kept.${NC}"

    fi

else

    echo -e "${YELLOW}[!] Web Panel download failed.${NC}"
    echo -e "${YELLOW}[!] CLI backup remains installed.${NC}"

fi

# ══════════════════════════════════════════════════════════════════════════════
# Final status
# ══════════════════════════════════════════════════════════════════════════════

echo
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}        idontPG-backup Installation Completed${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} Version     : v${INSTALLED_VERSION}${NC}"
echo -e "${GREEN} Developer   : ${DEVELOPER}${NC}"
echo -e "${GREEN} CLI         : idont-backup${NC}"
echo -e "${GREEN} Web Panel   : http://SERVER_IP:${WEB_PORT}${NC}"
echo -e "${GREEN} Scheduler   : systemd / automatic${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
echo

# Start CLI normally after installation.
exec "${INSTALL_PATH}"
