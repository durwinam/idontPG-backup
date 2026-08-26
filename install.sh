#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
#  idontPG-backup — One-command Installer
# ══════════════════════════════════════════════════════════════════════════════
#  Developer  : durwinam
#  Maintainer : durwinam
#  GitHub     : https://github.com/durwinam/idontPG-backup
#  License    : MIT
#
#  This installer is intentionally minimal and frozen.
#  All updates ship inside pg_backup.py — this file is never touched again.
# ══════════════════════════════════════════════════════════════════════════════
set -e

RED='\e[1;31m'
GREEN='\e[2;32m'
NC='\e[0m'

REPO="durwinam/idontPG-backup"
RAW_BASE="https://raw.githubusercontent.com/${REPO}"
INSTALL_PATH="/usr/local/bin/idontPG-backup"
TMP_PATH="/tmp/pg_backup.py"

DEVELOPER="durwinam"
VERSION_TAG="${1:-}"   # optional: pass a tag (e.g. v4.2.1 or 4.2.1) to pin that version

# Accept version from both forms:
#   bash install.sh 5.4.2        -> $1 = 5.4.2
#   bash -c "$(curl ...)" 5.4.2  -> $0 = 5.4.2 (bash -c name slot)
#   bash install.sh              -> VERSION_TAG stays empty (latest from main)
if [ -z "${VERSION_TAG}" ] && [[ "${0}" =~ ^[vV]?[0-9]+(\.[0-9]+){1,2}$ ]]; then
  VERSION_TAG="${0}"
fi

# Normalize tag: prepend "v" if missing (GitHub tags use v-prefix)
if [ -n "${VERSION_TAG}" ] && [[ ! "${VERSION_TAG}" =~ ^v ]]; then
  VERSION_TAG="v${VERSION_TAG}"
fi

# ── Root check ────────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
  echo -e "${RED}[-] Please run as root (sudo).${NC}"
  exit 1
fi

echo -e "${GREEN}[*] Installing idontPG-backup...${NC}"

# ── System packages ──────────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y python3 python3-pip curl unzip >/dev/null 2>&1 || true
fi

pip3 install --break-system-packages requests urllib3 paramiko >/dev/null 2>&1 || \
pip3 install requests urllib3 paramiko >/dev/null 2>&1 || true

# ── Pick source: pinned tag, explicit local offline copy, or main branch ─────
# v5.4.2 fix: the previous version silently reused ANY pg_backup.py sitting
# in the current working directory when no version tag was given — including
# when the installer was run via `bash -c "$(curl ...)"` from README, where
# $0 is literally "bash" and `dirname "$0"` resolves to ".". If a user
# happened to run the one-liner from a directory that had an old
# pg_backup.py left over (a previous manual download, an old git clone,
# a stale file from testing), the "official" installer would silently
# install THAT stale file instead of fetching the current version from
# GitHub — with no warning printed. That directly defeats the purpose of
# "Update to Latest Version".
#
# Offline installs (install.sh + pg_backup.py shipped together, no network)
# are still supported, but now require an explicit `--offline` flag instead
# of being auto-detected by directory sniffing, so a curl-piped invocation
# can never accidentally take this path.
OFFLINE=0
for arg in "$@"; do
  [ "${arg}" = "--offline" ] && OFFLINE=1
done

if [ -n "${VERSION_TAG}" ]; then
  SOURCE="${RAW_BASE}/${VERSION_TAG}/pg_backup.py"
  echo -e "${GREEN}[*] Source: pinned tag ${VERSION_TAG}${NC}"
elif [ "${OFFLINE}" -eq 1 ] && [ -f "$(dirname "$0")/pg_backup.py" ]; then
  cp "$(dirname "$0")/pg_backup.py" "${TMP_PATH}"
  SOURCE=""
  echo -e "${GREEN}[*] Source: local offline copy ($(dirname "$0")/pg_backup.py)${NC}"
elif [ "${OFFLINE}" -eq 1 ]; then
  echo -e "${RED}[-] --offline given but no pg_backup.py found next to install.sh.${NC}"
  exit 1
else
  SOURCE="${RAW_BASE}/main/pg_backup.py?v=$(date +%s)"
  echo -e "${GREEN}[*] Source: latest from 'main' branch${NC}"
fi

if [ -n "${SOURCE}" ]; then
  if ! curl -fsSL "${SOURCE}" -o "${TMP_PATH}"; then
    echo -e "${RED}[-] Download failed: ${SOURCE}${NC}"
    echo -e "${RED}[-] Check your network connection and that the tag/branch exists.${NC}"
    exit 1
  fi
fi

if [ ! -s "${TMP_PATH}" ]; then
  echo -e "${RED}[-] Download failed. Check GitHub repo/file name.${NC}"
  exit 1
fi

# ── Extract version from the file header comment ─────────────────────────────
# Looks for the first "vX.Y.Z" or "vX.Y" pattern in the first 30 lines.
INSTALLED_VERSION=$(grep -m1 -oE 'VERSION[[:space:]]*=[[:space:]]*"v?[0-9]+\.[0-9]+(\.[0-9]+)?"' "${TMP_PATH}" \
  | grep -oE 'v?[0-9]+\.[0-9]+(\.[0-9]+)?' \
  | head -1 \
  | sed 's/^v//')
if [ -z "${INSTALLED_VERSION}" ]; then
  INSTALLED_VERSION=$(head -30 "${TMP_PATH}" \
    | grep -m1 -oE 'v[0-9]+\.[0-9]+(\.[0-9]+)?' \
    | sed 's/^v//')
fi
[ -z "${INSTALLED_VERSION}" ] && INSTALLED_VERSION="unknown"

echo -e "${GREEN}[+] Version: v${INSTALLED_VERSION}${NC}"

# ── Restore shebang (raw GitHub strips it) ───────────────────────────────────
if ! head -n 1 "${TMP_PATH}" | grep -q "python"; then
  printf '%s\n%s\n' '#!/usr/bin/env python3' "$(cat "${TMP_PATH}")" > "${TMP_PATH}.tmp"
  mv "${TMP_PATH}.tmp" "${TMP_PATH}"
fi

mv "${TMP_PATH}" "${INSTALL_PATH}"
chmod +x "${INSTALL_PATH}"

# Convenience CLI alias for terminal configuration.
ln -sf "${INSTALL_PATH}" /usr/local/bin/idont-backup

echo -e "${GREEN}[+] Installed to ${INSTALL_PATH}${NC}"
echo -e "${GREEN}[+] CLI command: idont-backup --set${NC}"
echo -e "${GREEN}[+] Developer: ${DEVELOPER}${NC}"

# ── Web Panel ────────────────────────────────────────────────────────────────
WEB_PANEL_PATH="/usr/local/bin/idontPG-backup-web.py"
WEB_PANEL_URL="${RAW_BASE}/main/web_panel.py"
WEB_TMP="/tmp/idontpg-web-panel.py"
WEB_ASSET_DIR="/usr/local/share/idontPG-backup"
WEB_LOGO_PATH="${WEB_ASSET_DIR}/logo.png"
WEB_LOGO_URL="${RAW_BASE}/main/web/static/logo.png"
if curl -fsSL "${WEB_PANEL_URL}?v=$(date +%s)" -o "${WEB_TMP}" && [ -s "${WEB_TMP}" ]; then
  mv "${WEB_TMP}" "${WEB_PANEL_PATH}"
  chmod 700 "${WEB_PANEL_PATH}"
  mkdir -p "${WEB_ASSET_DIR}"
  if curl -fsSL "${WEB_LOGO_URL}?v=$(date +%s)" -o "${WEB_LOGO_PATH}" && [ -s "${WEB_LOGO_PATH}" ]; then
    chmod 644 "${WEB_LOGO_PATH}"
    echo -e "${GREEN}[+] Web panel logo installed.${NC}"
  else
    echo -e "${RED}[!] Web panel logo download failed; panel will use fallback branding.${NC}"
    rm -f "${WEB_LOGO_PATH}"
  fi
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
  systemctl daemon-reload
  systemctl enable --now idontpg-backup-web.service >/dev/null 2>&1 || true
  echo -e "${GREEN}[+] Web panel installed: http://SERVER_IP:5000${NC}"
else
  echo -e "${RED}[!] Web panel download failed; CLI backup remains installed.${NC}"
fi

echo -e "${GREEN}[+] Launching idontPG-backup...${NC}"
echo
exec "${INSTALL_PATH}"
