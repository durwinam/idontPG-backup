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
#      HTTP mode:  http://SERVER_IP:5000
#      HTTPS mode: https://DOMAIN:<free-port> with automatic Let's Encrypt
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
BOT_URL="${RAW_BASE}/main/idont_bot.py"
BOT_PATH="/usr/local/bin/idontPG-backup-bot.py"
BOT_TMP="/tmp/idontpg-bot.py"
BOT_CONFIG="/etc/idontPG-backup/telegram_bot.json"
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


# Return the server's public IPv4 address when available.
# This is informational only for the HTTP-only installer; failure must never
# stop installation. Prefer curl, then wget, then a local address fallback.
get_public_ip() {
    local ip=""
    if command -v curl >/dev/null 2>&1; then
        ip="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    fi
    if [ -z "${ip}" ] && command -v wget >/dev/null 2>&1; then
        ip="$(wget -qO- -T 5 https://api.ipify.org 2>/dev/null || true)"
    fi
    if [[ "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        printf '%s\n' "${ip}"
    else
        printf '%s\n' ""
    fi
}

port_is_free() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ! ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(:|\])${port}$"
        return $?
    fi
    if command -v lsof >/dev/null 2>&1; then
        ! lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
        return $?
    fi
    return 0
}

find_free_https_port() {
    local p
    for p in $(seq 5443 5499); do
        if port_is_free "$p"; then
            printf '%s\n' "$p"
            return 0
        fi
    done
    return 1
}

dns_records() {
    local domain="$1"
    if command -v dig >/dev/null 2>&1; then
        {
            dig +short A "$domain" 2>/dev/null || true
            dig +short AAAA "$domain" 2>/dev/null || true
        } | sed '/^$/d' | sort -u | tr '\n' ' ' | sed 's/[[:space:]]*$//'
    elif command -v getent >/dev/null 2>&1; then
        getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' | sed 's/[[:space:]]*$//'
    else
        printf '%s\n' ""
    fi
}

firewall_open_tcp() {
    local port="$1"
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
        ufw allow "${port}/tcp" >/dev/null 2>&1 || true
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null 2>&1 || true
        firewall-cmd --add-port="${port}/tcp" >/dev/null 2>&1 || true
    fi
}

port_owner() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp "sport = :${port}" 2>/dev/null | tail -n +2 | sed 's/^[[:space:]]*//' | head -1
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1
    fi
}

stop_known_http_service() {
    local service=""
    for service in nginx apache2 httpd caddy; do
        if systemctl is-active --quiet "$service" 2>/dev/null; then
            echo -e "${YELLOW}[!] ${service} is using TCP port 80.${NC}"
            read -r -p "  Temporarily stop ${service} for Let's Encrypt validation? [y/N]: " answer
            if [[ "$answer" =~ ^[Yy]$ ]]; then
                systemctl stop "$service"
                printf '%s\n' "$service"
                return 0
            fi
            return 1
        fi
    done
    return 1
}

install_certbot() {
    if command -v certbot >/dev/null 2>&1; then return 0; fi
    echo -e "${GREEN}[*] Installing Certbot...${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get install -y certbot
    fi
    command -v certbot >/dev/null 2>&1
}

setup_web_panel_mode() {
    local mode="" domain="" email="" port="" records="" public_ip="" cert_dir="" cert_file="" key_file="" stopped_service=""

    echo
    echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║             Web Panel Connection Mode              ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
    echo
    echo -e "  ${GREEN}1${NC} - HTTP via Server IP  ${YELLOW}(port 5000)${NC}"
    echo -e "  ${GREEN}2${NC} - HTTPS via Domain + Let's Encrypt  ${YELLOW}(automatic certificate)${NC}"
    echo
    read -r -p "  Select mode [1/2, default 1]: " mode
    mode="${mode:-1}"

    if [ "$mode" = "2" ]; then
        while true; do
            read -r -p "  Domain (example.com): " domain
            domain="$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]' | sed 's#^https\?://##; s#/.*$##; s/^[[:space:]]*//; s/[[:space:]]*$//')"
            if [[ "$domain" =~ ^([a-z0-9-]+\.)+[a-z]{2,63}$ ]]; then break; fi
            echo -e "${RED}[!] Invalid domain name.${NC}"
        done

        echo -e "${GREEN}[*] Checking DNS for ${domain}...${NC}"
        records="$(dns_records "$domain")"
        if [ -z "$records" ]; then
            echo -e "${RED}[!] DNS does not resolve for ${domain}.${NC}"
            echo -e "${YELLOW}[!] Create an A record pointing to this server and wait for DNS propagation.${NC}"
            return 1
        fi
        echo -e "${GREEN}[+] DNS records:${NC} ${records}"

        public_ip="$(get_public_ip)"
        if [ -n "$public_ip" ]; then
            if printf '%s' "$records" | tr ' ' '\n' | grep -Fxq "$public_ip"; then
                echo -e "${GREEN}[+] DNS A record matches this server: ${public_ip}${NC}"
            else
                echo -e "${YELLOW}[!] DNS does not directly resolve to this server's public IPv4 (${public_ip}).${NC}"
                echo -e "${YELLOW}    This may be a CDN/Cloudflare proxy. HTTP-01 still requires port 80 to reach this origin.${NC}"
                read -r -p "  Continue with certificate issuance? [y/N]: " answer
                if [[ ! "$answer" =~ ^[Yy]$ ]]; then return 1; fi
            fi
        fi

        if ! install_certbot; then
            echo -e "${RED}[!] Certbot installation failed. Run 'apt-get update && apt-get install certbot' and retry.${NC}"
            return 1
        fi

        if ! port_is_free 80; then
            echo -e "${YELLOW}[!] TCP port 80 is currently in use.${NC}"
            port_owner 80 || true
            stopped_service="$(stop_known_http_service || true)"
            if [ -z "$stopped_service" ] && ! port_is_free 80; then
                echo -e "${RED}[!] Cannot start Let's Encrypt HTTP validation because port 80 is busy.${NC}"
                echo -e "${YELLOW}[!] Stop the service shown above, then run the installer again.${NC}"
                return 1
            fi
        fi

        # HTTP-01 validation is performed before the Web Panel service starts.
        # Open the validation port when a host firewall is enabled.
        firewall_open_tcp 80

        port="$(find_free_https_port)" || {
            echo -e "${RED}[!] Could not find a free HTTPS port in 5443-5499.${NC}"
            [ -n "$stopped_service" ] && systemctl start "$stopped_service" || true
            return 1
        }
        echo -e "${GREEN}[+] Free HTTPS port selected:${NC} ${port}"

        read -r -p "  Email for Let's Encrypt (optional, press Enter to skip): " email
        cert_dir="/etc/letsencrypt/live/${domain}"
        cert_file="${cert_dir}/fullchain.pem"
        key_file="${cert_dir}/privkey.pem"

        echo -e "${GREEN}[*] Requesting Let's Encrypt certificate for ${domain}...${NC}"
        certbot_args=(certonly --standalone --preferred-challenges http-01 --http-01-port 80 --non-interactive --agree-tos -d "$domain")
        if [ -n "$email" ]; then
            certbot_args+=(--email "$email" --no-eff-email)
        else
            certbot_args+=(--register-unsafely-without-email)
        fi

        if ! certbot "${certbot_args[@]}"; then
            echo -e "${RED}[!] Let's Encrypt could not issue the certificate.${NC}"
            echo -e "${YELLOW}[!] Check: DNS → this server, public TCP 80, Cloudflare proxy/origin rules, and the Certbot log at /var/log/letsencrypt/letsencrypt.log${NC}"
            [ -n "$stopped_service" ] && systemctl start "$stopped_service" || true
            return 1
        fi

        if [ ! -s "$cert_file" ] || [ ! -s "$key_file" ]; then
            echo -e "${RED}[!] Certificate files were not created. HTTPS setup aborted.${NC}"
            [ -n "$stopped_service" ] && systemctl start "$stopped_service" || true
            return 1
        fi

        # The certificate files are kept at the stable Let's Encrypt /live path.
        # Web Panel reads these paths directly, so renewals automatically use the
        # renewed certificate after the deploy-hook restarts the service.
        cat > /etc/default/idontpg-backup-web <<EOF
IDONTPG_HOST=0.0.0.0
IDONTPG_PORT=${port}
IDONTPG_SCHEME=https
IDONTPG_SSL_CERTFILE=${cert_file}
IDONTPG_SSL_KEYFILE=${key_file}
IDONT_PG_WEB_URL=https://${domain}:${port}
IDONTPG_SSL_DOMAIN=${domain}
EOF
        chmod 600 /etc/default/idontpg-backup-web
        firewall_open_tcp "$port"

        echo -e "${GREEN}[+] Let's Encrypt certificate issued successfully.${NC}"
        echo -e "${GREEN}[+] Certificate:${NC} ${cert_file}"
        echo -e "${GREEN}[+] Private key:${NC} ${key_file}"
        echo -e "${GREEN}[+] HTTPS Web Panel configured.${NC}"
        echo -e "${GREEN}[+] URL:${NC} https://${domain}:${port}"

        cat > /etc/systemd/system/idontpg-cert-renew.service <<'EOF'
[Unit]
Description=idontPG-backup Let's Encrypt certificate renewal
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --quiet --deploy-hook "systemctl restart idontpg-backup-web.service"
EOF
        cat > /etc/systemd/system/idontpg-cert-renew.timer <<'EOF'
[Unit]
Description=Daily idontPG-backup certificate renewal check

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
        systemctl daemon-reload
        systemctl enable --now idontpg-cert-renew.timer >/dev/null 2>&1 || true
        systemctl disable --now certbot.timer >/dev/null 2>&1 || true
        [ -n "$stopped_service" ] && systemctl start "$stopped_service" || true
        return 0
    fi

    cat > /etc/default/idontpg-backup-web <<'EOF'
IDONTPG_HOST=0.0.0.0
IDONTPG_PORT=5000
IDONTPG_SCHEME=http
IDONTPG_SSL_CERTFILE=
IDONTPG_SSL_KEYFILE=
IDONTPG_SSL_DOMAIN=
EOF
    chmod 600 /etc/default/idontpg-backup-web
    systemctl disable --now idontpg-cert-renew.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/idontpg-cert-renew.timer /etc/systemd/system/idontpg-cert-renew.service
    systemctl disable --now certbot.timer >/dev/null 2>&1 || true
    systemctl daemon-reload
    return 0
}

setup_telegram_bot() {
    local answer token admin_ids mini_url include_node
    echo
    echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║          Telegram Management Bot                  ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
    echo
    echo -e "  This adds an admin-only Telegram control center."
    echo -e "  Colored buttons use Telegram's current Bot API styles."
    read -r -p "  Install Telegram Management Bot? [Y/n]: " answer
    answer="${answer:-Y}"
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        rm -f "${BOT_TMP}"
        echo -e "${YELLOW}[!] Telegram Management Bot skipped.${NC}"
        return 0
    fi

    rm -f "${BOT_TMP}"
    if [ -s "${SCRIPT_DIR}/idont_bot.py" ]; then
        cp "${SCRIPT_DIR}/idont_bot.py" "${BOT_TMP}"
    elif curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 "${BOT_URL}?cache=$(date +%s)" -o "${BOT_TMP}" && [ -s "${BOT_TMP}" ]; then
        :
    else
        echo -e "${RED}[!] Failed to obtain idont_bot.py${NC}"
        return 1
    fi
    if ! python3 -m py_compile "${BOT_TMP}" >/dev/null 2>&1; then
        echo -e "${RED}[!] Telegram Bot Python validation failed.${NC}"
        rm -f "${BOT_TMP}"
        return 1
    fi
    install -m 700 "${BOT_TMP}" "${BOT_PATH}"
    rm -f "${BOT_TMP}"

    token=""
    if [ -s /etc/idontPG-backup/web.json ]; then
        token="$(python3 - <<'PY2'
import json
try:
    d=json.load(open('/etc/idontPG-backup/web.json'))
    print(d.get('token',''))
except Exception: pass
PY2
)"
    fi
    if [ -z "$token" ]; then
        read -r -p "  Bot Token: " token
    else
        echo -e "${GREEN}[+] Reusing Telegram Bot Token from Web Panel configuration.${NC}"
    fi
    while [ -z "$token" ]; do
        echo -e "${RED}[!] Bot Token cannot be empty.${NC}"
        read -r -p "  Bot Token: " token
    done

    admin_ids=""
    if [ -s /etc/idontPG-backup/web.json ]; then
        admin_ids="$(python3 - <<'PY2'
import json
try:
    d=json.load(open('/etc/idontPG-backup/web.json'))
    x=str(d.get('chat','')).strip()
    print(x if x.lstrip('-').isdigit() else '')
except Exception: pass
PY2
)"
    fi
    read -r -p "  Admin Telegram ID${admin_ids:+ [default ${admin_ids}]}: " answer
    admin_ids="${answer:-$admin_ids}"
    while [ -z "$admin_ids" ] || ! [[ "$admin_ids" =~ ^[0-9-]+$ ]]; do
        echo -e "${RED}[!] Enter a numeric Telegram user ID.${NC}"
        read -r -p "  Admin Telegram ID: " admin_ids
    done

    mini_url=""
    if [ -s /etc/default/idontpg-backup-web ]; then
        mini_url="$(grep -E '^IDONT_PG_WEB_URL=' /etc/default/idontpg-backup-web | cut -d= -f2- | tr -d '"')"
    fi
    include_node="false"
    if [ -s /etc/idontPG-backup/web.json ]; then
        include_node="$(python3 - <<'PY2'
import json
try:
    d=json.load(open('/etc/idontPG-backup/web.json'))
    print('true' if d.get('node') else 'false')
except Exception: print('false')
PY2
)"
    fi
    mkdir -p /etc/idontPG-backup
    chmod 700 /etc/idontPG-backup
    python3 - "$BOT_CONFIG" "$token" "$admin_ids" "$mini_url" "$include_node" <<'PY2'
import json, os, sys
path, token, admin, mini, node = sys.argv[1:]
data = {
    'token': token,
    'admin_ids': [int(admin)],
    'mini_app_url': mini,
    'include_node': node == 'true',
}
tmp=path+'.tmp'
open(tmp,'w',encoding='utf-8').write(json.dumps(data,ensure_ascii=False,indent=2))
os.chmod(tmp,0o600)
os.replace(tmp,path)
os.chmod(path,0o600)
PY2

    cat > /etc/systemd/system/idontpg-backup-telegram-bot.service <<EOF
[Unit]
Description=idontPG-backup Telegram Management Bot
After=network-online.target idontpg-backup-web.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${BOT_PATH}
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
    chmod 600 /etc/systemd/system/idontpg-backup-telegram-bot.service
    systemctl daemon-reload
    if systemctl enable --now idontpg-backup-telegram-bot.service >/dev/null 2>&1; then
        echo -e "${GREEN}[+] Telegram Management Bot is running.${NC}"
        echo -e "${GREEN}[+] Access:${NC} Admin Telegram IDs only"
    else
        echo -e "${RED}[!] Telegram Management Bot failed to start.${NC}"
        systemctl status idontpg-backup-telegram-bot.service --no-pager || true
    fi
}

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
    qrcode \
    >/dev/null 2>&1 || \
pip3 install \
    requests \
    urllib3 \
    paramiko \
    pysocks \
    grpcio \
    qrcode \
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
        # Web Panel connection mode
        # HTTP/IP -> port 5000
        # HTTPS/domain -> automatic DNS check + Let's Encrypt + free port
        # ──────────────────────────────────────────────────────────────────────

        if ! setup_web_panel_mode; then
            echo -e "${RED}[!] Web Panel mode setup failed.${NC}"
            exit 1
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
EnvironmentFile=-/etc/default/idontpg-backup-web
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

        # Ensure any legacy certificate-manager binary is no longer used.
        rm -f /usr/local/bin/idontpg-cert-manager
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

        setup_telegram_bot || echo -e "${YELLOW}[!] Telegram Management Bot setup skipped/failed; Web Panel remains installed.${NC}"

        echo
        # Read the exact selected Web Panel URL from the generated environment.
        # shellcheck disable=SC1091
        source /etc/default/idontpg-backup-web 2>/dev/null || true
        if [ "${IDONTPG_SCHEME:-http}" = "https" ] && [ -n "${IDONT_PG_WEB_URL:-}" ]; then
            echo -e "${GREEN}[+] Web Panel (HTTPS):${NC} ${IDONT_PG_WEB_URL}"
            echo -e "${GREEN}[+] Certificate:${NC} Let's Encrypt · automatic renewal enabled"
        else
            SERVER_IP="$(get_public_ip)"
            [ -z "${SERVER_IP}" ] && SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
            [ -z "${SERVER_IP}" ] && SERVER_IP="127.0.0.1"
            echo -e "${GREEN}[+] Web Panel (HTTP):${NC} http://${SERVER_IP}:5000"
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

if systemctl is-active --quiet idontpg-backup-telegram-bot.service; then
    echo -e "${GREEN}[✓] Telegram Management Bot: RUNNING${NC}"
elif [ -x "${BOT_PATH}" ]; then
    echo -e "${YELLOW}[!] Telegram Management Bot: NOT RUNNING${NC}"
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
# shellcheck disable=SC1091
source /etc/default/idontpg-backup-web 2>/dev/null || true
if [ "${IDONTPG_SCHEME:-http}" = "https" ] && [ -n "${IDONT_PG_WEB_URL:-}" ]; then
    echo -e "    ${GREEN}${IDONT_PG_WEB_URL}${NC}"
else
    SERVER_IP="$(get_public_ip)"
    [ -z "${SERVER_IP}" ] && SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -z "${SERVER_IP}" ] && SERVER_IP="127.0.0.1"
    echo -e "    ${GREEN}http://${SERVER_IP}:5000${NC}"
fi
echo -e "  Web Scheduler:"
echo -e "    ${GREEN}systemctl status idontpg-backup-web-scheduler${NC}"
echo
echo -e "${GREEN}Developer: durwinam${NC}"
echo
echo -e "${YELLOW}[*] Launching idontPG-backup...${NC}"
echo

exec "${INSTALL_PATH}"
