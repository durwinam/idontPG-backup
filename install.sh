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
#      https://SERVER_IP:5000 (when HTTPS setup succeeds)
#      http://SERVER_IP:5000  (fallback)
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
        # Optional HTTPS / Let's Encrypt setup
        #
        # HTTPS is deliberately non-fatal: if certificate issuance fails because
        # DNS, firewall, port 80, or ACME validation is unavailable, installation
        # continues and the panel remains available on HTTP :5000.
        # ──────────────────────────────────────────────────────────────────────

        HTTPS_STATE_DIR="/etc/idontPG-backup"
        HTTPS_CONF="${HTTPS_STATE_DIR}/https.conf"
        CERT_MANAGER="/usr/local/bin/idontpg-cert-manager"
        CERTBOT_BIN="$(command -v certbot 2>/dev/null || true)"

        mkdir -p "${HTTPS_STATE_DIR}"
        chmod 700 "${HTTPS_STATE_DIR}"

        detect_public_ip() {
            local ip=""
            ip="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
            if [[ ! "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
                ip="$(curl -4fsS --max-time 8 https://ifconfig.me/ip 2>/dev/null || true)"
            fi
            printf '%s' "${ip}"
        }

        certbot_version_ok_for_ip() {
            local v major minor
            v="$("${CERTBOT_BIN}" --version 2>/dev/null | sed -n 's/.*certbot \([0-9][0-9.]*\).*/\1/p')"
            major="${v%%.*}"
            minor="${v#*.}"; minor="${minor%%.*}"
            [ -n "${major}" ] && [ -n "${minor}" ] && { [ "${major}" -gt 5 ] || { [ "${major}" -eq 5 ] && [ "${minor}" -ge 4 ]; }; }
        }

        ensure_ip_certbot() {
            if command -v certbot >/dev/null 2>&1; then
                CERTBOT_BIN="$(command -v certbot)"
                if certbot_version_ok_for_ip; then return 0; fi
                echo -e "${YELLOW}[!] Installed Certbot is too old for IP certificates; upgrading...${NC}"
            fi
            if command -v snap >/dev/null 2>&1; then
                snap install certbot --classic >/dev/null 2>&1 || snap refresh certbot >/dev/null 2>&1 || true
                CERTBOT_BIN="$(command -v certbot 2>/dev/null || true)"
                if [ -n "${CERTBOT_BIN}" ] && certbot_version_ok_for_ip; then return 0; fi
            fi
            if command -v python3 >/dev/null 2>&1; then
                python3 -m pip install --break-system-packages --upgrade 'certbot>=5.4,<6' >/dev/null 2>&1 || \
                python3 -m pip install --upgrade 'certbot>=5.4,<6' >/dev/null 2>&1 || true
                CERTBOT_BIN="$(command -v certbot 2>/dev/null || true)"
                if [ -n "${CERTBOT_BIN}" ] && certbot_version_ok_for_ip; then return 0; fi
            fi
            return 1
        }

        install_certbot_if_needed() {
            if command -v certbot >/dev/null 2>&1; then
                CERTBOT_BIN="$(command -v certbot)"
                return 0
            fi
            echo -e "${GREEN}[*] Installing Certbot for HTTPS...${NC}"
            if command -v snap >/dev/null 2>&1; then
                snap install certbot --classic >/dev/null 2>&1 || true
                CERTBOT_BIN="$(command -v certbot 2>/dev/null || true)"
            fi
            if [ -z "${CERTBOT_BIN}" ] && command -v apt-get >/dev/null 2>&1; then
                apt-get install -y certbot >/dev/null 2>&1 || true
                CERTBOT_BIN="$(command -v certbot 2>/dev/null || true)"
            fi
            [ -n "${CERTBOT_BIN}" ]
        }

        write_https_conf() {
            local mode="$1" identifier="$2" auto_ip="$3" cert="$4" key="$5"
            cat > "${HTTPS_CONF}" <<EOF
MODE=${mode}
IDENTIFIER=${identifier}
AUTO_IP=${auto_ip}
CERT_FILE=${cert}
KEY_FILE=${key}
PORT=5000
EOF
            chmod 600 "${HTTPS_CONF}"
        }

        setup_https_certificate() {
            local choice="" identifier="" current_ip="" resolved_ip="" cert_dir=""
            echo
            echo -e "${GREEN}════════ HTTPS Configuration ════════${NC}"
            echo -e "  ${GREEN}1)${NC} Domain"
            echo -e "  ${GREEN}2)${NC} Public IP"
            echo -e "  ${GREEN}3)${NC} Skip HTTPS"
            echo
            read -r -p "Select [1-3]: " choice || choice="3"

            case "${choice}" in
                1)
                    read -r -p "Enter domain: " identifier || identifier=""
                    identifier="$(printf '%s' "${identifier}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
                    if [[ ! "${identifier}" =~ ^([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
                        echo -e "${YELLOW}[!] Invalid domain. HTTPS skipped; installation continues.${NC}"
                        return 0
                    fi
                    if ! install_certbot_if_needed; then
                        echo -e "${YELLOW}[!] Certbot unavailable. HTTPS skipped; installation continues.${NC}"
                        return 0
                    fi
                    current_ip="$(detect_public_ip)"
                    resolved_ip="$(getent ahostsv4 "${identifier}" 2>/dev/null | awk 'NR==1{print $1}')"
                    if [ -z "${resolved_ip}" ]; then
                        echo -e "${YELLOW}[!] DNS lookup failed for ${identifier}. HTTPS skipped.${NC}"
                        return 0
                    fi
                    echo -e "${GREEN}[+] DNS:${NC} ${identifier} -> ${resolved_ip}"
                    if [ -n "${current_ip}" ] && [ "${resolved_ip}" != "${current_ip}" ]; then
                        echo -e "${YELLOW}[!] DNS does not point to this server (${current_ip}). HTTPS skipped.${NC}"
                        return 0
                    fi
                    echo -e "${GREEN}[*] Requesting Let's Encrypt certificate...${NC}"
                    if "${CERTBOT_BIN}" certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email --preferred-challenges http -d "${identifier}" --keep-until-expiring >/tmp/idontpg-certbot.log 2>&1; then
                        cert_dir="/etc/letsencrypt/live/${identifier}"
                        if [ -s "${cert_dir}/fullchain.pem" ] && [ -s "${cert_dir}/privkey.pem" ]; then
                            write_https_conf "domain" "${identifier}" "0" "${cert_dir}/fullchain.pem" "${cert_dir}/privkey.pem"
                            echo -e "${GREEN}[✓] HTTPS certificate issued.${NC}"
                            echo -e "${GREEN}[✓] HTTPS will be available at https://${identifier}:5000${NC}"
                            return 0
                        fi
                    fi
                    echo -e "${YELLOW}[!] Certificate issuance failed. See /tmp/idontpg-certbot.log${NC}"
                    echo -e "${YELLOW}[!] Installation continues without HTTPS.${NC}"
                    ;;
                2)
                    current_ip="$(detect_public_ip)"
                    read -r -p "Detected Public IP: ${current_ip}. Use this IP? [Y/n]: " use_auto || use_auto="Y"
                    use_auto="${use_auto:-Y}"
                    if [[ "${use_auto}" =~ ^[Nn]$ ]]; then
                        read -r -p "Enter public IP: " identifier || identifier=""
                        auto_ip="0"
                    else
                        identifier="${current_ip}"
                        auto_ip="1"
                    fi
                    if [[ ! "${identifier}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
                        echo -e "${YELLOW}[!] Invalid IPv4 address. HTTPS skipped; installation continues.${NC}"
                        return 0
                    fi
                    if ! ensure_ip_certbot; then
                        echo -e "${YELLOW}[!] Certbot 5.4+ unavailable. HTTPS skipped; installation continues.${NC}"
                        return 0
                    fi
                    echo -e "${GREEN}[*] Requesting short-lived IP certificate...${NC}"
                    if "${CERTBOT_BIN}" certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email --preferred-profile shortlived --preferred-challenges http --ip-address "${identifier}" >/tmp/idontpg-certbot.log 2>&1; then
                        cert_dir="/etc/letsencrypt/live/${identifier}"
                        if [ -s "${cert_dir}/fullchain.pem" ] && [ -s "${cert_dir}/privkey.pem" ]; then
                            write_https_conf "ip" "${identifier}" "${auto_ip}" "${cert_dir}/fullchain.pem" "${cert_dir}/privkey.pem"
                            echo -e "${GREEN}[✓] IP certificate issued.${NC}"
                            echo -e "${GREEN}[✓] Daily IP/certificate check enabled.${NC}"
                            echo -e "${GREEN}[✓] HTTPS will be available at https://${identifier}:5000${NC}"
                            return 0
                        fi
                    fi
                    echo -e "${YELLOW}[!] IP certificate issuance failed. See /tmp/idontpg-certbot.log${NC}"
                    echo -e "${YELLOW}[!] Installation continues without HTTPS.${NC}"
                    ;;
                *)
                    echo -e "${YELLOW}[!] HTTPS skipped.${NC}"
                    ;;
            esac
        }

        # HTTPS is configured before the service starts, so the first launch can
        # immediately use the certificate when issuance succeeded.
        setup_https_certificate || true

        cat > "${CERT_MANAGER}" <<'EOF'
#!/bin/bash
set +e
STATE_DIR="/etc/idontPG-backup"
CONF="${STATE_DIR}/https.conf"
CERTBOT="$(command -v certbot 2>/dev/null || true)"
LOG="/var/log/idontpg-cert-manager.log"
mkdir -p "${STATE_DIR}"
exec >>"${LOG}" 2>&1

echo "[$(date -Is)] certificate check started"
[ -s "${CONF}" ] || { echo "[$(date -Is)] HTTPS not configured"; exit 0; }
[ -n "${CERTBOT}" ] || { echo "[$(date -Is)] certbot not found"; exit 0; }

# shellcheck disable=SC1090
source "${CONF}"

public_ip() {
    curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || \
    curl -4fsS --max-time 8 https://ifconfig.me/ip 2>/dev/null || true
}

write_conf() {
    local id="$1"
    local dir="/etc/letsencrypt/live/${id}"
    cat >"${CONF}" <<EOC
MODE=ip
IDENTIFIER=${id}
AUTO_IP=1
CERT_FILE=${dir}/fullchain.pem
KEY_FILE=${dir}/privkey.pem
PORT=5000
EOC
    chmod 600 "${CONF}"
}

if [ "${MODE}" = "ip" ] && [ "${AUTO_IP}" = "1" ]; then
    NOW="$(public_ip)"
    if [[ "${NOW}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && [ "${NOW}" != "${IDENTIFIER}" ]; then
        echo "[$(date -Is)] public IP changed: ${IDENTIFIER} -> ${NOW}"
        if "${CERTBOT}" certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email --preferred-profile shortlived --preferred-challenges http --ip-address "${NOW}"; then
            if [ -s "/etc/letsencrypt/live/${NOW}/fullchain.pem" ] && [ -s "/etc/letsencrypt/live/${NOW}/privkey.pem" ]; then
                write_conf "${NOW}"
                systemctl restart idontpg-backup-web.service >/dev/null 2>&1 || true
                echo "[$(date -Is)] new IP certificate installed"
                exit 0
            fi
        fi
        echo "[$(date -Is)] new IP certificate failed; keeping previous configuration"
    fi
fi

# Daily renewal check. For IP certificates Certbot uses the shortlived profile.
if [ "${MODE}" = "ip" ]; then
    "${CERTBOT}" renew --non-interactive --preferred-profile shortlived --deploy-hook 'systemctl restart idontpg-backup-web.service' || true
else
    "${CERTBOT}" renew --non-interactive --deploy-hook 'systemctl restart idontpg-backup-web.service' || true
fi

echo "[$(date -Is)] certificate check finished"
EOF
        chmod 700 "${CERT_MANAGER}"

        cat > /etc/systemd/system/idontpg-cert-renew.service <<EOF
[Unit]
Description=idontPG-backup HTTPS certificate check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${CERT_MANAGER}
User=root
EOF
        chmod 600 /etc/systemd/system/idontpg-cert-renew.service

        cat > /etc/systemd/system/idontpg-cert-renew.timer <<'EOF'
[Unit]
Description=Daily idontPG-backup HTTPS certificate check

[Timer]
OnBootSec=10min
OnUnitActiveSec=24h
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
        chmod 600 /etc/systemd/system/idontpg-cert-renew.timer

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
        systemctl enable --now idontpg-cert-renew.timer >/dev/null 2>&1 || true

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
        if [ -s "${HTTPS_CONF}" ] && grep -q '^CERT_FILE=' "${HTTPS_CONF}" && [ -s "$(sed -n 's/^CERT_FILE=//p' "${HTTPS_CONF}")" ]; then
            echo -e "${GREEN}[+] Web Panel:${NC} https://$(sed -n 's/^IDENTIFIER=//p' "${HTTPS_CONF}"):5000"
        else
            echo -e "${GREEN}[+] Web Panel:${NC} http://SERVER_IP:5000"
        fi

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
if [ -s "/etc/idontPG-backup/https.conf" ] && grep -q '^CERT_FILE=' /etc/idontPG-backup/https.conf; then
    HTTPS_ID="$(sed -n 's/^IDENTIFIER=//p' /etc/idontPG-backup/https.conf)"
    echo -e "    ${GREEN}https://${HTTPS_ID}:5000${NC}"
else
    echo -e "    ${GREEN}http://SERVER_IP:5000${NC}"
fi
echo
echo -e "  Web Scheduler:"
echo -e "    ${GREEN}systemctl status idontpg-backup-web-scheduler${NC}"
echo
echo -e "${GREEN}Developer: durwinam${NC}"
echo
echo -e "${YELLOW}[*] Launching idontPG-backup...${NC}"
echo

exec "${INSTALL_PATH}"
