from importlib.machinery import SourceFileLoader
#!/usr/bin/env python3
"""idontPG-backup Web Panel
Author: durwinam
A dependency-free dark glass web UI for Telegram backup configuration.
"""
import argparse
import base64
import hashlib
import hmac
import html
import importlib.util
import importlib.machinery
import json
import os
import re
import socket
import secrets
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "idontPG-backup"
VERSION = "5.6.4"
ADMIN_PATH = "/control-7Kq9M2xP4/"
HOST = os.environ.get("IDONTPG_HOST", "0.0.0.0")
PORT = int(os.environ.get("IDONTPG_PORT", "5000"))
STATE_DIR = Path(os.environ.get("IDONTPG_STATE_DIR", "/etc/idontPG-backup"))
CONFIG = STATE_DIR / "web.json"
SCRIPT = Path(__file__).resolve()
LOGO_CANDIDATES = [SCRIPT.parent / "web" / "static" / "logo.png", Path("/usr/local/share/idontPG-backup/logo.png")]
CORE_CANDIDATES = [
    Path("/usr/local/bin/idontPG-backup"),
    Path("/usr/local/bin/PG-Backup"),
    SCRIPT.parent / "pg_backup.py",
]
SESSIONS = {}
ADMIN_LOGIN_ATTEMPTS = {}
SESSION_TTL = 12 * 60 * 60
ADMIN_SESSION_TTL = 2 * 60 * 60


def load_core():
    """Load the backup core safely from either a .py file or the extensionless
    installed executable created by install.sh.

    importlib.util.spec_from_file_location() can return a spec whose loader is
    None for extensionless files. The old web panel then crashed with:
    'NoneType' object has no attribute 'loader'. Use SourceFileLoader for that
    case and fail with a useful message if the file is not Python source.
    """
    for path in CORE_CANDIDATES:
        if not path.is_file():
            continue
        try:
            if path.suffix == ".py":
                spec = importlib.util.spec_from_file_location("idontpg_core", str(path))
            else:
                loader = importlib.machinery.SourceFileLoader("idontpg_core", str(path))
                spec = importlib.util.spec_from_loader("idontpg_core", loader)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "create_backup"):
                continue
            return mod
        except Exception as exc:
            last_error = exc
            continue
    detail = f": {last_error}" if 'last_error' in locals() else ""
    raise RuntimeError(f"idontPG-backup core could not be loaded{detail}")


def load_cfg():
    default = {
        "token": "", "chat": "", "topic": "", "proxy": "",
        "interval": "24", "node": False, "username": "admin", "password_hash": "", "password_salt": ""
    }
    if not CONFIG.exists():
        return default
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        default.update(data)
        return default
    except Exception:
        return default


def canonical_username(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9-]{5,32}", value) else "admin"


def valid_username(value):
    return bool(re.fullmatch(r"[A-Za-z0-9-]{5,32}", str(value or "")))


def valid_password(value):
    value = str(value or "")
    return (len(value) >= 8 and
            len(re.findall(r"[A-Za-z]", value)) >= 2 and
            bool(re.search(r"[A-Z]", value)) and
            bool(re.search(r"[0-9]", value)) and
            bool(re.search(r"[^A-Za-z0-9]", value)))


def save_cfg(c):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 250_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def check_password(password, c):
    try:
        salt = base64.b64decode(c["password_salt"])
        expected = base64.b64decode(c["password_hash"])
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 250_000)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def proxy_opener(proxy):
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def normalize_topic_id(value):
    """Accept a raw Telegram forum topic ID or a topic/message link."""
    value = str(value or "").strip()
    if not value:
        return ""
    # Raw numeric message_thread_id is the preferred form.
    if value.isdigit():
        return value
    # Also accept common Telegram links such as:
    # https://t.me/c/1234567890/456 and https://t.me/<username>/456
    m = re.search(r"/(?:c/)?(?:[^/]+/)?([0-9]+)(?:[?#].*)?$", value.rstrip("/"))
    if m:
        return m.group(1)
    # A link may contain a trailing query/fragment or whitespace.
    m = re.search(r"/([0-9]+)(?:[/?#].*)?$", value)
    return m.group(1) if m else ""


def telegram_request(token, endpoint, params, proxy=None, timeout=30):
    url = f"https://api.telegram.org/bot{token}/{endpoint}"
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}", method="GET")
    with proxy_opener(proxy).open(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
        return bool(payload.get("ok")), payload.get("description", "OK")


def telegram_test(c):
    token, chat = c.get("token", ""), c.get("chat", "")
    topic_raw = c.get("topic", "")
    topic = normalize_topic_id(topic_raw)
    if not token or not chat:
        return False, "Bot Token و Chat ID الزامی هستند."
    if topic_raw and not topic:
        return False, "Topic ID نامعتبر است. فقط عدد message_thread_id یا لینک Topic تلگرام را وارد کنید."
    params = {"chat_id": chat, "text": "✅ idontPG-backup\nTelegram connection test successful."}
    if topic:
        params["message_thread_id"] = int(topic)
    try:
        return telegram_request(token, "sendMessage", params, c.get("proxy") or None, 30)
    except Exception as e:
        return False, str(e)


def telegram_send(token, chat, topic, file_path, caption="", proxy=None):
    if not token or not chat:
        return False, "Bot Token و Chat ID الزامی هستند."
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = "----idontPG" + uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.extend([
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"", str(value).encode()
        ])

    field("chat_id", chat)
    topic_id = normalize_topic_id(topic)
    if topic and not topic_id:
        return False, "Topic ID نامعتبر است. فقط عدد message_thread_id یا لینک Topic تلگرام را وارد کنید."
    if topic_id:
        field("message_thread_id", int(topic_id))
    if caption:
        field("caption", caption)
    with open(file_path, "rb") as f:
        data = f.read()
    parts.extend([
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="document"; filename="{Path(file_path).name}"'.encode(),
        b"Content-Type: application/zip", b"", data,
        f"--{boundary}--".encode(), b""
    ])
    body = b"\r\n".join(parts)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    try:
        with proxy_opener(proxy).open(req, timeout=300) as r:
            payload = json.loads(r.read().decode("utf-8"))
            return bool(payload.get("ok")), payload.get("description", "OK")
    except Exception as e:
        return False, str(e)


def send_archive(core, archive, c, caption):
    """Send normal or oversized archive using the core splitter when available."""
    if not archive or not os.path.exists(archive):
        return False, "فایل Backup ساخته نشد."
    parts_to_remove = []
    try:
        if os.path.getsize(archive) <= 49 * 1024 * 1024:
            return telegram_send(c.get("token"), c.get("chat"), c.get("topic"), archive, caption, c.get("proxy") or None)
        if hasattr(core, "_split_file_into_chunks"):
            info = core._split_file_into_chunks(archive)
            chunks = info.get("chunks", [])
        else:
            chunks = []
        if not chunks:
            return False, "تقسیم فایل بزرگ برای Telegram در هسته Backup در دسترس نیست."
        for i, part in enumerate(chunks, 1):
            if part != archive:
                parts_to_remove.append(part)
            ok, msg = telegram_send(
                c.get("token"), c.get("chat"), c.get("topic"), part,
                f"{caption}\nPart {i}/{len(chunks)}", c.get("proxy") or None
            )
            if not ok:
                return False, f"ارسال قسمت {i} ناموفق بود: {msg}"
        return True, f"Backup در {len(chunks)} قسمت ارسال شد."
    finally:
        for part in parts_to_remove:
            try:
                os.remove(part)
            except OSError:
                pass
        try:
            os.remove(archive)
        except OSError:
            pass


def make_backup(send=True):
    c = load_cfg()
    core = load_core()
    try:
        archive = core.create_backup(bool(c.get("node", False)))
        if archive and os.path.exists(archive):
            _record_backup_created(archive)
        if not send:
            _record_activity("Backup دستی ساخته شد", "ok")
            return True, f"Backup ساخته شد: {archive}"
        ok, msg = send_archive(core, archive, c, f"idontPG-backup · {time.strftime('%Y-%m-%d %H:%M:%S')}")
        _record_activity("Backup و ارسال به Telegram موفق بود" if ok else "Backup ساخته شد ولی ارسال Telegram ناموفق بود", "ok" if ok else "bad")
        return ok, msg
    except Exception as exc:
        _record_activity("Backup ناموفق بود", "bad")
        raise


def scheduler_service(action):
    return subprocess.run(
        ["systemctl", action, "idontpg-backup-web-scheduler.service"],
        capture_output=True, text=True, timeout=20
    )


def scheduler_status():
    p = subprocess.run(["systemctl", "is-active", "idontpg-backup-web-scheduler.service"], capture_output=True, text=True)
    return p.stdout.strip() or "inactive"



def _panel_read_env():
    """Read PasarGuard's .env using the same source path as PGClockMG."""
    env_path = Path("/opt/pasarguard/.env")
    try:
        return env_path.read_text(encoding="utf-8", errors="ignore") if env_path.exists() else ""
    except Exception:
        return ""


def _panel_env_var(text, key):
    if not text:
        return ""
    pattern = re.compile(r"(?m)^\s*" + re.escape(key) + r"\s*=\s*(.*)\s*$")
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def _panel_has_ssl(env_text):
    cert = _panel_env_var(env_text, "UVICORN_SSL_CERTFILE")
    key = _panel_env_var(env_text, "UVICORN_SSL_KEYFILE")
    return bool(cert and key and not cert.startswith("#") and not key.startswith("#"))


def _panel_server_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _panel_hostname_from_url(value):
    try:
        parsed = urllib.parse.urlparse((value or "").strip())
        host = parsed.hostname or ""
        if host and "." in host and host not in ("localhost", "127.0.0.1"):
            return host
    except Exception:
        pass
    return ""


def _panel_guess_domain(env_text):
    for key in (
        "SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL_PREFIX",
        "XRAY_SUBSCRIPTION_URL",
        "SUBSCRIPTION_URL",
        "PUBLIC_URL",
        "UVICORN_PUBLIC_URL",
        "ALLOWED_ORIGINS",
    ):
        raw = _panel_env_var(env_text, key)
        if not raw:
            continue
        values = re.split(r"[\s,]+", raw) if key == "ALLOWED_ORIGINS" else [raw]
        for value in values:
            host = _panel_hostname_from_url(value.rstrip("/"))
            if host:
                return host
    cert = _panel_env_var(env_text, "UVICORN_SSL_CERTFILE")
    match = re.search(r"/certs/([^/]+)/", cert.replace("\\", "/"))
    if match and "." in match.group(1) and match.group(1) != "ip":
        return match.group(1)
    return ""


def _panel_build_url(host, port="8000", https=True, root_path=""):
    root = (root_path or "").rstrip("/")
    path = f"{root}/dashboard/".replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path
    scheme = "https" if https else "http"
    return f"{scheme}://{host}:{str(port or '8000').strip()}{path}"


def get_panel_info():
    """Build/check the PasarGuard panel URL using PGClockMG's access model."""
    env_text = _panel_read_env()
    port = _panel_env_var(env_text, "UVICORN_PORT") or "8000"
    root_path = _panel_env_var(env_text, "UVICORN_ROOT_PATH").rstrip("/")
    ssl = _panel_has_ssl(env_text)
    domain = _panel_guess_domain(env_text)
    host = domain or _panel_server_ip()

    public_https = _panel_build_url(host, port, True, root_path)
    localhost_url = _panel_build_url("127.0.0.1", port, False, root_path)
    url = public_https if (ssl or domain) else localhost_url

    status = "Online"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "idontPG-backup-panel-check/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            status = "Online" if response.status < 500 else "Offline"
    except urllib.error.HTTPError as exc:
        status = "Online" if exc.code < 500 else "Offline"
    except Exception:
        status = "Offline"

    return {"url": url, "status": status}


def _format_bytes(value):
    value = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _directory_size(path):
    total = 0
    try:
        base = Path(path)
        if not base.is_dir():
            return 0
        for item in base.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _backup_archives():
    """Find retained idontPG-backup archives without recursively scanning the whole filesystem."""
    candidates = [Path.cwd(), Path("/"), Path("/root"), Path("/opt/pasarguard"),
                  Path("/var/lib/idontPG-backup"), Path("/usr/local/share/idontPG-backup")]
    seen = set()
    found = []
    for directory in candidates:
        try:
            if not directory.is_dir():
                continue
            for item in directory.glob("backup_*.zip"):
                try:
                    stat = item.stat()
                    key = (stat.st_dev, stat.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append((item, stat.st_size, stat.st_mtime))
                except OSError:
                    continue
        except OSError:
            continue
    found.sort(key=lambda x: x[2], reverse=True)
    return found


ACTIVITY_FILE = STATE_DIR / "activities.json"
BACKUP_HISTORY_FILE = STATE_DIR / "backup_history.json"
BACKUP_HISTORY_FILES = (
    STATE_DIR / "backup_history.json",
    Path("/var/lib/idontPG-backup/backup_history.json"),
)

def _record_activity(message, kind="ok"):
    """Keep a tiny rolling activity history for the dashboard."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        items = []
        if ACTIVITY_FILE.is_file():
            try:
                items = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
                if not isinstance(items, list): items = []
            except Exception:
                items = []
        items.insert(0, {"time": time.time(), "message": str(message), "kind": str(kind)})
        ACTIVITY_FILE.write_text(json.dumps(items[:3], ensure_ascii=False), encoding="utf-8")
        try: ACTIVITY_FILE.chmod(0o600)
        except OSError: pass
    except Exception:
        pass

def get_recent_activities():
    """Return up to five recent dashboard activities."""
    items = []
    try:
        if ACTIVITY_FILE.is_file():
            raw = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list): items = raw[:3]
    except Exception:
        pass
    return items

def _next_backup_seconds(interval_hours, latest_mtime):
    try:
        interval = max(0.5, float(interval_hours)) * 3600
        if not latest_mtime:
            return int(interval)
        return max(0, int(latest_mtime + interval - time.time()))
    except Exception:
        return None

def _relative_time(ts):
    try:
        delta=max(0,int(time.time()-float(ts)))
        if delta < 60: return "همین الان"
        if delta < 3600: return f"{delta//60} دقیقه پیش"
        if delta < 86400: return f"{delta//3600} ساعت پیش"
        return f"{delta//86400} روز پیش"
    except Exception:
        return "—"

def _load_backup_history():
    """Load and merge backup metadata from all durable history locations."""
    merged = []
    seen = set()
    for history_path in BACKUP_HISTORY_FILES:
        try:
            if not history_path.is_file():
                continue
            raw = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if not name or name in seen:
                    continue
                merged.append(item)
                seen.add(name)
        except Exception:
            continue
    return merged


def _record_backup_created(archive):
    """Persist backup metadata before the archive is sent/removed."""
    try:
        path = Path(archive)
        if not path.is_file():
            return
        stat = path.stat()
        entry = {"name": path.name, "size": int(stat.st_size), "mtime": float(stat.st_mtime)}
        existing = _load_backup_history()
        existing = [entry] + [x for x in existing if str(x.get("name") or "") != path.name]
        existing = existing[:100]
        for history_path in BACKUP_HISTORY_FILES:
            try:
                history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = history_path.with_suffix(history_path.suffix + ".tmp")
                tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
                os.chmod(tmp, 0o600)
                tmp.replace(history_path)
            except Exception:
                continue
    except Exception:
        pass


def get_backup_info():
    """Return reliable backup statistics from local files + creation history."""
    archives = _backup_archives()
    history = _load_backup_history()

    # Files still on disk are authoritative for their current size. For
    # Telegram backups that are removed after upload, persisted metadata keeps
    # the count and latest-backup information available.
    current_by_name = {item.name: (size, mtime) for item, size, mtime in archives}
    merged = []
    seen = set()
    for item in history:
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue
        size = int(item.get("size") or 0)
        mtime = float(item.get("mtime") or 0)
        if name in current_by_name:
            size, mtime = current_by_name[name]
        merged.append((name, max(0, size), mtime))
        seen.add(name)
    for item, size, mtime in archives:
        if item.name not in seen:
            merged.append((item.name, size, mtime))
            seen.add(item.name)
    merged.sort(key=lambda x: x[2], reverse=True)

    total = sum(size for _, size, _ in merged)
    latest = merged[0] if merged else None
    return {
        "count": len(merged),
        "size": _format_bytes(total),
        "latest": latest[0] if latest else "هنوز Backup ساخته نشده",
        "latest_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(latest[2])) if latest else "—",
        "latest_mtime": latest[2] if latest else 0,
    }


def get_backup_storage_usage():
    return get_backup_info()["size"]


def _pg_env_value(key):
    """Read a PasarGuard .env value without requiring python-dotenv."""
    for env_path in (Path("/opt/pasarguard/.env"), Path("/opt/pasarguard/.env.local")):
        try:
            if not env_path.is_file():
                continue
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() != key:
                    continue
                v = v.strip().strip("\"").strip("'")
                return v
        except OSError:
            continue
    return os.environ.get(key, "")


def _pg_api_base():
    """Build the local PasarGuard API base URL from its .env configuration."""
    port = _pg_env_value("UVICORN_PORT") or "8000"
    root = (_pg_env_value("UVICORN_ROOT_PATH") or "").strip().rstrip("/")
    # The local HTTP endpoint avoids DNS/Cloudflare and works even when the
    # public panel URL uses a domain or HTTPS certificate.
    return f"http://127.0.0.1:{port}{root}/api"


def _pg_api_token():
    """Authenticate to PasarGuard using its configured sudo/admin account."""
    username = _pg_env_value("SUDO_USERNAME")
    password = _pg_env_value("SUDO_PASSWORD")
    if not username or not password:
        return ""
    data = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "grant_type": "password",
    }).encode("utf-8")
    req = urllib.request.Request(
        _pg_api_base() + "/admin/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload.get("access_token") or "")


def _pg_json_get(path, token):
    req = urllib.request.Request(
        _pg_api_base() + path,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Api-Key": token,
            "Accept": "application/json",
            "User-Agent": f"{APP}/{VERSION}",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_number(value):
    try:
        if isinstance(value, bool):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _sum_user_traffic(payload):
    """Extract total user traffic from common PasarGuard API list shapes."""
    if isinstance(payload, dict):
        # Some API versions wrap users in `users`, `items` or `data`.
        for key in ("users", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return _sum_user_traffic(value)
        data = payload.get("data")
        if isinstance(data, (list, dict)):
            nested = _sum_user_traffic(data)
            if nested > 0 or isinstance(data, list):
                return nested
        # Prefer a direct system-wide metric when available.
        for key in ("total_traffic", "traffic_usage", "used_traffic", "total_used_traffic"):
            if key in payload and isinstance(payload[key], (int, float, str)):
                return _extract_number(payload[key])
        return 0
    if not isinstance(payload, list):
        return 0
    total = 0
    for user in payload:
        if not isinstance(user, dict):
            continue
        # PasarGuard user objects expose used_traffic; accept a few historical
        # aliases without recursively summing unrelated nested values.
        if "used_traffic" in user:
            total += _extract_number(user.get("used_traffic"))
        elif "traffic_used" in user:
            total += _extract_number(user.get("traffic_used"))
        elif "data_usage" in user:
            total += _extract_number(user.get("data_usage"))
        else:
            uplink = user.get("uplink", user.get("upload", user.get("incoming_bandwidth", 0)))
            downlink = user.get("downlink", user.get("download", user.get("outgoing_bandwidth", 0)))
            total += _extract_number(uplink) + _extract_number(downlink)
    return total


def _users_page(payload):
    """Return (users, reported_total) from the PasarGuard users response."""
    if isinstance(payload, list):
        return [u for u in payload if isinstance(u, dict)], None
    if not isinstance(payload, dict):
        return [], None

    users = payload.get("users")
    if not isinstance(users, list):
        users = payload.get("items")
    if not isinstance(users, list):
        data = payload.get("data")
        if isinstance(data, dict):
            users = data.get("users") or data.get("items")
        elif isinstance(data, list):
            users = data
    if not isinstance(users, list):
        users = []

    total = None
    for key in ("total", "count", "total_count"):
        value = payload.get(key)
        if value is None and isinstance(payload.get("data"), dict):
            value = payload["data"].get(key)
        try:
            if value is not None:
                total = int(value)
                break
        except (TypeError, ValueError):
            pass
    return [u for u in users if isinstance(u, dict)], total


def _sum_all_panel_users(token):
    """Sum current `used_traffic` for every PasarGuard user.

    PasarGuard returns users through a paginated /api/users endpoint. Reading
    only the first page makes the dashboard show a small/incorrect total on
    installations with many users, so walk every page explicitly.
    """
    total_used = 0
    offset = 0
    limit = 100
    seen_pages = set()

    while offset < 100000:
        path = f"/users?limit={limit}&offset={offset}&load_sub=true"
        payload = _pg_json_get(path, token)
        users, reported_total = _users_page(payload)
        marker = (offset, len(users), reported_total)
        if marker in seen_pages:
            break
        seen_pages.add(marker)

        for user in users:
            value = user.get("used_traffic")
            if value is None:
                value = user.get("traffic_used")
            if value is None:
                value = user.get("data_usage")
            total_used += _extract_number(value)

        if not users:
            break
        offset += len(users)

        if reported_total is not None and offset >= reported_total:
            break
        if len(users) < limit and reported_total is None:
            break

    return total_used


# ── PasarGuard Node traffic (direct Node API) ───────────────────────────────
# Traffic is read from the registered PasarGuard Nodes themselves.  The panel
# API is used only to discover node address/port/API key/CA.
NODE_TRAFFIC_FILE = STATE_DIR / "node_traffic.json"
_NODE_TRAFFIC_CACHE = {"ts": 0.0, "value": None}
_NODE_TRAFFIC_LOCK = __import__("threading").Lock()
_NODE_TRAFFIC_LAST_ERROR = ""


def _pb_varint(value):
    value = int(value)
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _pb_read_varint(data, pos):
    value = 0
    shift = 0
    n = len(data)
    while pos < n:
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("invalid protobuf varint")
    raise ValueError("truncated protobuf varint")


def _pb_fields(data):
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _pb_read_varint(data, pos)
        field_no, wire = key >> 3, key & 7
        if field_no <= 0:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, pos = _pb_read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > n:
                raise ValueError("truncated fixed64")
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = _pb_read_varint(data, pos)
            if pos + size > n:
                raise ValueError("truncated bytes")
            value, pos = data[pos:pos + size], pos + size
        elif wire == 5:
            if pos + 4 > n:
                raise ValueError("truncated fixed32")
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield field_no, wire, value


def _pb_stat_request(name="", reset=False, stat_type=4):
    body = bytearray()
    if name:
        raw = str(name).encode("utf-8")
        body += b"\x0a" + _pb_varint(len(raw)) + raw
    if reset:
        body += b"\x10\x01"
    body += b"\x18" + _pb_varint(stat_type)  # UsersStat = 4
    return bytes(body)


def _pb_parse_stat_response(data):
    stats = []
    for field_no, wire, value in _pb_fields(data):
        if field_no != 1 or wire != 2:
            continue
        name = stat_type = link = ""
        amount = 0
        for sf, sw, sv in _pb_fields(value):
            if sf in (1, 2, 3) and sw == 2:
                text = sv.decode("utf-8", "replace")
                if sf == 1:
                    name = text
                elif sf == 2:
                    stat_type = text
                else:
                    link = text
            elif sf == 4 and sw == 0:
                amount = int(sv)
        stats.append({"name": name, "type": stat_type, "link": link, "value": amount})
    return stats


def _node_json_list(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("nodes", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        return _node_json_list(data)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _discover_pg_nodes(token):
    nodes = []
    offset = 0
    limit = 100
    seen = set()
    while offset < 10000:
        payload = _pg_json_get(f"/nodes?limit={limit}&offset={offset}", token)
        page = _node_json_list(payload)
        if not page:
            break
        marker = json.dumps(page, sort_keys=True, ensure_ascii=False)[:8000]
        if marker in seen:
            break
        seen.add(marker)
        nodes.extend(page)
        offset += len(page)
        if len(page) < limit:
            break
    return nodes


def _node_value(node, *keys):
    # PasarGuard panel versions have returned connection fields both at the
    # top level and inside config/connection/node/settings.  Search all of
    # those shapes without assuming a fixed schema.
    objects = [node]
    for parent in ("config", "connection", "node", "settings", "server"):
        obj = node.get(parent) if isinstance(node, dict) else None
        if isinstance(obj, dict):
            objects.append(obj)
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return value
    return ""


def _node_address(node):
    value = str(_node_value(node, "address", "host", "ip", "domain", "hostname", "server") or "").strip()
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        value = parsed.hostname or value
    elif value.startswith("[") and "]" in value:
        value = value[1:value.find("]")]
    # If the panel returns host:port in address, keep only the host here; the
    # service port is resolved separately from the node's own port field.
    try:
        parsed = urllib.parse.urlparse("//" + value)
        if parsed.hostname:
            value = parsed.hostname
    except Exception:
        pass
    return value.strip("[]")


def _node_port(node):
    value = _node_value(node, "port", "service_port", "grpc_port", "node_port")
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        return 0


def _node_api_key(node):
    return str(_node_value(node, "api_key", "apiKey", "key") or "").strip()


def _node_ca_text(node):
    ca = str(_node_value(node, "server_ca", "serverCA", "ca", "certificate") or "")
    return ca.replace("\\n", "\n").strip()


def _node_ssl_context(node):
    ca = _node_ca_text(node)
    if ca:
        try:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cadata=ca)
            return ctx
        except Exception:
            pass
    return ssl._create_unverified_context()


def _node_grpc_target(node):
    address = _node_address(node)
    port = _node_port(node)
    if not address:
        return ""
    if ":" in address and address.count(":") > 1:
        address = f"[{address}]"
    return f"{address}:{port}" if port else address


def _node_stats_total(stats):
    # UsersStat returns one Stat record per traffic counter.  Depending on
    # Node/Core version the type string can be "uplink", "downlink", or be
    # omitted/renamed.  For a UsersStat response every value is a traffic
    # counter, so summing all non-negative values is the version-safe choice.
    total = 0
    for item in stats:
        try:
            total += max(0, int(item.get("value") or 0))
        except (TypeError, ValueError):
            continue
    return total


def _node_grpc_call(node, auth_header):
    import grpc
    target = _node_grpc_target(node)
    key = _node_api_key(node)
    if not target or not key:
        raise RuntimeError("node address/port/api_key missing")

    ca = _node_ca_text(node)
    credentials = grpc.ssl_channel_credentials(root_certificates=ca.encode("utf-8") if ca else None)
    channel = grpc.secure_channel(target, credentials)
    try:
        rpc = channel.unary_unary(
            "/service.NodeService/GetStats",
            request_serializer=lambda x: x,
            response_deserializer=lambda x: x,
        )
        response = rpc(
            _pb_stat_request(stat_type=4, reset=False),
            metadata=((auth_header, f"Bearer {key}"),),
            timeout=6,
        )
        return _node_stats_total(_pb_parse_stat_response(response))
    finally:
        channel.close()


def _node_stats_grpc(node):
    try:
        import grpc  # noqa: F401
    except Exception as exc:
        raise RuntimeError(f"grpcio unavailable: {exc}")

    errors = []
    # Current docs specify authorization: Bearer. Older deployments may still
    # accept x-api-key, so retain it as a compatibility attempt.
    for header in ("authorization", "x-api-key"):
        try:
            return _node_grpc_call(node, header)
        except Exception as exc:
            errors.append(f"grpc/{header}: {exc}")
    raise RuntimeError("; ".join(errors))


def _node_stats_rest(node):
    address = _node_address(node)
    port = _node_port(node)
    key = _node_api_key(node)
    if not address or not port or not key:
        raise RuntimeError("node address/port/api_key missing")
    if ":" in address and address.count(":") > 1:
        host = f"[{address}]"
    else:
        host = address
    url = f"https://{host}:{port}/stats/"
    body = _pb_stat_request(stat_type=4, reset=False)
    errors = []
    for headers in (
        {"Authorization": f"Bearer {key}"},
        {"X-Api-Key": key},
    ):
        try:
            req = urllib.request.Request(url, data=body, method="GET", headers={
                **headers,
                "Content-Type": "application/x-protobuf",
                "Accept": "application/x-protobuf",
                "User-Agent": f"{APP}/{VERSION}",
            })
            with urllib.request.urlopen(req, timeout=6, context=_node_ssl_context(node)) as response:
                payload = response.read()
            return _node_stats_total(_pb_parse_stat_response(payload))
        except Exception as exc:
            errors.append(f"rest/{next(iter(headers))}: {exc}")
    raise RuntimeError("; ".join(errors))


def _node_stats_raw(node):
    # Honor the Node's configured transport when present. The same node
    # address/port/api_key/server_ca supplied by PasarGuard is used; no fixed
    # localhost address or fixed port is assumed.
    protocol = str(_node_value(node, "service_protocol", "protocol", "transport") or "").lower()
    if protocol in ("rest", "http", "https"):
        try:
            return _node_stats_rest(node)
        except Exception as rest_error:
            try:
                return _node_stats_grpc(node)
            except Exception as grpc_error:
                raise RuntimeError(f"rest: {rest_error} | grpc: {grpc_error}")
    try:
        return _node_stats_grpc(node)
    except Exception as grpc_error:
        try:
            return _node_stats_rest(node)
        except Exception as rest_error:
            raise RuntimeError(f"grpc: {grpc_error} | rest: {rest_error}")


def _load_node_traffic_state():
    try:
        raw = json.loads(NODE_TRAFFIC_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"nodes": {}}
    except Exception:
        return {"nodes": {}}


def _save_node_traffic_state(state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = NODE_TRAFFIC_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(NODE_TRAFFIC_FILE)
    except Exception:
        pass


def get_panel_storage_usage():
    """Return cumulative traffic collected directly from connected Nodes."""
    global _NODE_TRAFFIC_LAST_ERROR
    now = time.time()
    with _NODE_TRAFFIC_LOCK:
        if _NODE_TRAFFIC_CACHE.get("value") is not None and now - _NODE_TRAFFIC_CACHE.get("ts", 0) < 15:
            return _format_bytes(_NODE_TRAFFIC_CACHE["value"])
        try:
            token = _pg_api_token()
            if not token:
                _NODE_TRAFFIC_LAST_ERROR = "PasarGuard sudo credentials not found"
                return "قابل دریافت نیست"
            nodes = _discover_pg_nodes(token)
            if not nodes:
                _NODE_TRAFFIC_LAST_ERROR = "PasarGuard returned no nodes"
                return "قابل دریافت نیست"

            state = _load_node_traffic_state()
            state_nodes = state.setdefault("nodes", {})
            successful = 0
            errors = []

            for node in nodes:
                status = str(node.get("status") or "").lower()
                if status in ("disabled", "deleted", "offline", "disconnected"):
                    continue
                key = str(node.get("id") or node.get("name") or _node_address(node) or "")
                if not key:
                    continue
                try:
                    raw = int(_node_stats_raw(node))
                    successful += 1
                except Exception as exc:
                    errors.append(f"{key}: {exc}")
                    continue

                previous = state_nodes.get(key, {}) if isinstance(state_nodes.get(key), dict) else {}
                last_raw = int(previous.get("raw") or 0)
                accumulated = int(previous.get("total") or 0)
                delta = raw - last_raw if raw >= last_raw else raw
                accumulated += max(0, delta)
                state_nodes[key] = {"raw": raw, "total": accumulated, "updated": now}

            if successful == 0:
                _NODE_TRAFFIC_LAST_ERROR = " | ".join(errors)[-1800:] or "all nodes failed"
                return "قابل دریافت نیست"

            total = sum(max(0, int(v.get("total") or 0)) for v in state_nodes.values() if isinstance(v, dict))
            state["updated"] = now
            _save_node_traffic_state(state)
            _NODE_TRAFFIC_LAST_ERROR = ""
            _NODE_TRAFFIC_CACHE.update({"ts": now, "value": total})
            return _format_bytes(total)
        except Exception as exc:
            _NODE_TRAFFIC_LAST_ERROR = str(exc)[-1800:]
            return "قابل دریافت نیست"

def get_disk_info():
    try:
        st=os.statvfs(str(STATE_DIR if STATE_DIR.exists() else Path('/')))
        total=st.f_blocks*st.f_frsize; free=st.f_bavail*st.f_frsize; used=max(0,total-free)
        percent=(used/total*100) if total else 0
        return {'used':_format_bytes(used),'free':_format_bytes(free),'total':_format_bytes(total),'percent':min(100,max(0,round(percent,1)))}
    except Exception:
        return {'used':'—','free':'—','total':'—','percent':0}

def _cpu_times():
    try:
        line=Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        vals=[int(x) for x in line.split()[1:]]
        idle=vals[3]+(vals[4] if len(vals)>4 else 0)
        total=sum(vals)
        return total,idle
    except Exception:
        return None

def get_server_resource_usage():
    """Return live CPU, RAM and root-disk usage without external dependencies."""
    cpu=0.0
    a=_cpu_times()
    if a:
        time.sleep(0.12)
        b=_cpu_times()
        if b:
            total=b[0]-a[0]; idle=b[1]-a[1]
            if total>0:
                cpu=max(0.0,min(100.0,(total-idle)*100.0/total))
    ram=0.0
    try:
        mem={}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            k,v=line.split(":",1); mem[k]=int(v.strip().split()[0])
        total=mem.get("MemTotal",0); avail=mem.get("MemAvailable",mem.get("MemFree",0))
        if total>0: ram=max(0.0,min(100.0,(total-avail)*100.0/total))
    except Exception:
        pass
    disk=float(get_disk_info().get("percent",0) or 0)
    return {"cpu":round(cpu,1),"ram":round(ram,1),"disk":round(max(0,min(100,disk)),1)}



# ── Neon UI icon system (v5.5.4-final4) ───────────────────────────────────────
_UI_ICONS = {
    "cpu": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9 2.8v3M12 2.8v3M15 2.8v3M9 18.2v3M12 18.2v3M15 18.2v3M2.8 9h3M2.8 12h3M2.8 15h3M18.2 9h3M18.2 12h3M18.2 15h3"/><rect x="10" y="10" width="4" height="4" rx=".7"/></svg>',
    "ram": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="7" width="17" height="10" rx="2"/><path d="M7 10v4M10 10v4M14 10v4M17 10v4M6 4.5v2.5M10 4.5v2.5M14 4.5v2.5M18 4.5v2.5M6 17v2.5M10 17v2.5M14 17v2.5M18 17v2.5"/></svg>',
    "disk": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7.5h16l1.5 4v7A2.5 2.5 0 0 1 19 21H5a2.5 2.5 0 0 1-2.5-2.5v-7L4 7.5Z"/><path d="M4 7.5 6 3h12l2 4.5M7 15h10M8 18h.01"/></svg>',
    "traffic": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16M7 4l-3 3M7 4l3 3M17 20V4M17 20l-3-3M17 20l3-3"/><path d="M10 8h5M9 16h6"/></svg>',
    "backup": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14l1.5 3v9A2 2 0 0 1 18.5 21h-13A2 2 0 0 1 3.5 19v-9L5 7Z"/><path d="M6 7 8 3h8l2 4M8 14h8M10 17h4"/></svg>',
    "panel": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 19 6v5.5c0 4.5-2.8 7.5-7 9.5-4.2-2-7-5-7-9.5V6l7-3Z"/><path d="m9 12 2 2 4-4"/></svg>',
    "telegram": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 4-3 16-6-5-3.5 3.5.7-5.1L5 11l16-7Z"/><path d="m9.2 13.4 8.5-6.1"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5v3M12 17.5v3M3.5 12h3M17.5 12h3M5.9 5.9l2.1 2.1M16 16l2.1 2.1M18.1 5.9 16 8M8 16l-2.1 2.1"/><circle cx="12" cy="12" r="4.2"/><circle cx="12" cy="12" r="1.2"/></svg>',
    "account": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.2"/><path d="M5.5 20c.8-3.3 3-5 6.5-5s5.7 1.7 6.5 5"/><path d="M17.5 14.5 20 17l-2.5 2.5"/></svg>',
    "lock": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="10" rx="2.4"/><path d="M8 10V7.4a4 4 0 0 1 8 0V10"/><circle cx="12" cy="15" r="1.25"/><path d="M12 16.25v1.6"/></svg>',
    "dashboard": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><rect x="14" y="14" width="6" height="6" rx="1.5"/></svg>',
    "test": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 4-3 16-6-5-3.5 3.5.7-5.1L5 11l16-7Z"/><path d="M5 11 2.5 9.5"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/></svg>',
    "health": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h4l2-6 4 12 2-6h4"/></svg>',
    "chart": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19V9M12 19V5M19 19v-8"/><path d="M3 19h18"/></svg>',
    "activity": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"/></svg>',
    "download": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M8 11l4 4 4-4M5 20h14"/></svg>',
    "trash": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3M8 10v7M12 10v7M16 10v7M6 7l1 14h10l1-14"/></svg>',
    "rocket": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4c3-2 6-1 6-1s1 3-1 6l-5 5-4-4 4-6Z"/><path d="m10 10-4 1-3 3 5 1M14 14l-1 4-3 3-1-5M8 16l-3 3"/></svg>',
    "link": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 14.5 14.5 9.5M7.5 17.5l-2 2a4 4 0 0 1-5-5l3-3a4 4 0 0 1 5-1M16.5 6.5l2-2a4 4 0 0 1 5 5l-3 3a4 4 0 0 1-5 1"/></svg>',
}

def ui_icon(name, extra=""):
    # Status dots are deliberately NOT part of ordinary icons.  They are
    # rendered only by status_badge()/status_dot() where a real state exists.
    icon_name = re.sub(r"[^a-z0-9_-]", "", str(name or "activity").lower()) or "activity"
    return f'<span class="neo-icon icon-{icon_name} {extra}" aria-hidden="true">{_UI_ICONS.get(icon_name, _UI_ICONS["activity"])}</span>'

def status_dot(state="ok"):
    # Keep status indicators as standalone spans so generic icon rules can
    # never turn them into rectangles or bars.
    state = str(state or "ok").lower()
    if state not in {"ok", "bad", "warn", "info", "off"}:
        state = "info"
    return f'<span class="status-dot status-{html.escape(state)}" aria-hidden="true"></span>'

def status_badge(label, state="ok"):
    state = str(state or "ok").lower()
    if state not in {"ok", "bad", "warn", "info", "off"}:
        state = "info"
    return f'<span class="status status-{html.escape(state)}">{status_dot(state)}<span>{html.escape(str(label))}</span></span>'

def _resource_chart_html():
    usage=get_server_resource_usage()
    chart = '''<div class="resource-monitor" id="resourceMonitor">
  <div class="resource-summary">
    <div class="resource-stat cpu">{ui_icon("cpu")}<div><small>CPU LOAD</small><strong id="cpuValue">CPUVAL%</strong></div><i></i></div>
    <div class="resource-stat ram">{ui_icon("ram")}<div><small>RAM USED</small><strong id="ramValue">RAMVAL%</strong></div><i></i></div>
    <div class="resource-stat disk">{ui_icon("disk")}<div><small>DISK USED</small><strong id="diskValue">DISKVAL%</strong></div><i></i></div>
  </div>
  <div class="resource-plot">
    <div class="plot-head"><div><span class="plot-kicker">LIVE TELEMETRY</span><b>وضعیت لحظه‌ای سرور</b></div><span class="live-dot"><i></i> LIVE</span></div>
    <div class="plot-body">
      <div class="y-axis"><span>100%</span><span>75%</span><span>50%</span><span>25%</span><span>0%</span></div>
      <svg class="telemetry" id="telemetryChart" viewBox="0 0 900 300" preserveAspectRatio="none" role="img" aria-label="نمودار لحظه‌ای منابع سرور">
        <defs>
          <linearGradient id="fillCpu" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6ee7ff" stop-opacity=".30"/><stop offset="1" stop-color="#6ee7ff" stop-opacity="0"/></linearGradient>
          <linearGradient id="fillRam" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff4fa3" stop-opacity=".24"/><stop offset="1" stop-color="#ff4fa3" stop-opacity="0"/></linearGradient>
          <linearGradient id="fillDisk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff9b4a" stop-opacity=".20"/><stop offset="1" stop-color="#ff9b4a" stop-opacity="0"/></linearGradient>
          <filter id="glowC"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
          <filter id="glowP"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
        </defs>
        <g class="grid-lines"><line x1="0" y1="0" x2="900" y2="0"/><line x1="0" y1="75" x2="900" y2="75"/><line x1="0" y1="150" x2="900" y2="150"/><line x1="0" y1="225" x2="900" y2="225"/><line x1="0" y1="300" x2="900" y2="300"/></g>
        <path id="cpuArea" class="area cpu-area"></path><path id="ramArea" class="area ram-area"></path><path id="diskArea" class="area disk-area"></path>
        <path id="cpuLine" class="line cpu-line"></path><path id="ramLine" class="line ram-line"></path><path id="diskLine" class="line disk-line"></path>
        <circle id="cpuDot" class="dot cpu-dot" r="5"></circle><circle id="ramDot" class="dot ram-dot" r="5"></circle><circle id="diskDot" class="dot disk-dot" r="5"></circle>
      </svg>
      <div class="chart-legend"><span class="legend cpu"><i></i> CPU</span><span class="legend ram"><i></i> RAM</span><span class="legend disk"><i></i> Disk</span><span class="updated" id="resourceUpdated">اکنون</span></div>
    </div>
  </div>
</div>
<script>
(function(){
  const el=document.getElementById('resourceMonitor'); if(!el) return;
  const initial={cpu:CPUVAL,ram:RAMVAL,disk:DISKVAL};
  const history=Array.from({length:24},()=>({cpu:initial.cpu,ram:initial.ram,disk:initial.disk}));
  function path(values){const n=values.length,w=900,h=300;return values.map((v,i)=>{const x=i*(w/(n-1)),y=h-(Math.max(0,Math.min(100,v))/100)*h;return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)}).join(' ')}
  function area(values){return path(values)+' L 900 300 L 0 300 Z'}
  function point(values){const v=values[values.length-1],x=900,y=300-(Math.max(0,Math.min(100,v))/100)*300;return [x,y]}
  function render(){['cpu','ram','disk'].forEach(k=>{const vals=history.map(x=>x[k]);document.getElementById(k+'Line').setAttribute('d',path(vals));document.getElementById(k+'Area').setAttribute('d',area(vals));const pt=point(vals),dot=document.getElementById(k+'Dot');dot.setAttribute('cx',pt[0]);dot.setAttribute('cy',pt[1]);});}
  function setVals(u){['cpu','ram','disk'].forEach(k=>document.getElementById(k+'Value').textContent=Number(u[k]).toFixed(1)+'%');history.push({cpu:Number(u.cpu),ram:Number(u.ram),disk:Number(u.disk)});history.shift();render();document.getElementById('resourceUpdated').textContent='به‌روزرسانی: '+new Date().toLocaleTimeString('fa-IR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
  render();
  async function refresh(){try{const r=await fetch('/api/resources',{cache:'no-store',credentials:'same-origin'});if(!r.ok) throw new Error('status');setVals(await r.json());}catch(e){}}
  setInterval(refresh,3000);
})();
</script>'''
    chart=chart.replace('{ui_icon("cpu")}',ui_icon('cpu')).replace('{ui_icon("ram")}',ui_icon('ram')).replace('{ui_icon("disk")}',ui_icon('disk')).replace('CPUVAL',f'{usage["cpu"]:.1f}').replace('RAMVAL',f'{usage["ram"]:.1f}').replace('DISKVAL',f'{usage["disk"]:.1f}')
    return chart

def get_health_info(c,panel_info=None):
    panel_info=panel_info or get_panel_info(); scheduler=scheduler_status(); telegram_ok=bool(c.get('token') and c.get('chat')); disk=get_disk_info()
    return [('Web Panel',True,'در حال اجرا'),('PasarGuard',panel_info.get('status')=='Online',panel_info.get('status','Unknown')),('Telegram',telegram_ok,'تنظیم شده' if telegram_ok else 'تنظیم نشده'),('Scheduler',scheduler=='active','فعال' if scheduler=='active' else 'متوقف'),('Disk',float(disk.get('percent',0))<90,f"{disk.get('percent',0)}% استفاده")]

def _backup_history_records():
    """Merge persisted backup metadata with archives still present on disk."""
    archives = _backup_archives()
    current = {item.name: (size, mtime) for item, size, mtime in archives}
    records = []
    seen = set()
    history = _load_backup_history()

    for item in history:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue
        size = int(item.get("size") or 0)
        mtime = float(item.get("mtime") or 0)
        if name in current:
            size, mtime = current[name]
        records.append((name, max(0, size), mtime))
        seen.add(name)

    for item, size, mtime in archives:
        if item.name not in seen:
            records.append((item.name, size, mtime))
            seen.add(item.name)

    records.sort(key=lambda x: x[2], reverse=True)
    return records


def get_backup_chart():
    now=time.time(); records=_backup_history_records(); buckets=[]
    for days in range(6,-1,-1):
        start=now-(days+1)*86400; end=now-days*86400
        total=sum(size for _,size,mtime in records if start<=mtime<end)
        buckets.append((time.strftime('%m/%d',time.localtime(end-1)),total))
    peak=max((v for _,v in buckets),default=0) or 1
    return [(label,size,round(size/peak*100)) for label,size in buckets]


def csrf_token(sid):
    return SESSIONS.get(sid, {}).get("csrf", "")


def cleanup_sessions():
    now = time.time()
    for sid, info in list(SESSIONS.items()):
        if now - info.get("created", now) > SESSION_TTL:
            SESSIONS.pop(sid, None)


CSS = r"""
@import url("https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap");
:root{--bg:#06070d;--text:#f5f7ff;--muted:#9aa3b8;--line:rgba(255,255,255,.12);--glass:rgba(15,18,31,.58);--glass2:rgba(255,255,255,.055);--accent:#8b5cf6;--accent2:#22d3ee;--pink:#ec4899;--red:#ef4444;--good:#34d399;--bad:#fb7185;--shadow:0 24px 70px rgba(0,0,0,.42)}
body.light{--bg:#fff1f8;--text:#26162d;--muted:#735c78;--line:rgba(124,58,237,.18);--glass:rgba(255,255,255,.62);--glass2:rgba(255,255,255,.50);--accent:#7c3aed;--accent2:#db2777;--shadow:0 24px 70px rgba(124,58,237,.16);background:radial-gradient(circle at 8% 12%,rgba(168,85,247,.30),transparent 30%),radial-gradient(circle at 88% 14%,rgba(236,72,153,.27),transparent 30%),radial-gradient(circle at 70% 92%,rgba(239,68,68,.23),transparent 34%),linear-gradient(135deg,#fff7fc 0%,#fdf1ff 42%,#fff1f6 72%,#fff5f0 100%);background-size:180% 180%;animation:lightBg 18s ease-in-out infinite alternate}
*{box-sizing:border-box}html{min-height:100%;background:#06070d}body{margin:0;min-height:100vh;color:var(--text);font-family:"Vazirmatn","Tahoma","Segoe UI",sans-serif;overflow-x:hidden;background:radial-gradient(circle at 15% 15%,rgba(139,92,246,.18),transparent 30%),radial-gradient(circle at 85% 10%,rgba(34,211,238,.14),transparent 28%),radial-gradient(circle at 70% 90%,rgba(236,72,153,.12),transparent 32%),#06070d;transition:background .45s ease,color .35s ease}body.light{background:radial-gradient(circle at 8% 12%,rgba(168,85,247,.30),transparent 30%),radial-gradient(circle at 88% 14%,rgba(236,72,153,.27),transparent 30%),radial-gradient(circle at 70% 92%,rgba(239,68,68,.23),transparent 34%),linear-gradient(135deg,#fff7fc 0%,#fdf1ff 42%,#fff1f6 72%,#fff5f0 100%);background-size:180% 180%;animation:lightBg 18s ease-in-out infinite alternate}@keyframes lightBg{0%{background-position:0% 0%}100%{background-position:100% 100%}}
body.light:before,body.light:after{opacity:.55}body:before,body:after{content:"";position:fixed;z-index:-2;width:42vw;height:42vw;border-radius:50%;filter:blur(75px);opacity:.38;animation:float 16s ease-in-out infinite alternate;pointer-events:none}body:before{background:#7c3aed;left:-12vw;top:10vh}body:after{background:#06b6d4;right:-12vw;bottom:0}@keyframes float{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(5vw,-3vh,0) scale(1.16)}}
.aurora{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}.orb{position:absolute;border-radius:999px;filter:blur(50px);opacity:.28;mix-blend-mode:screen;animation:drift 18s infinite alternate ease-in-out}.o1{width:30vw;height:30vw;background:#8b5cf6;left:5%;top:18%;animation-duration:20s}.o2{width:25vw;height:25vw;background:#06b6d4;right:4%;top:25%;animation-duration:24s}.o3{width:24vw;height:24vw;background:#ec4899;left:35%;bottom:-8%;animation-duration:22s}.light .o1{background:#a855f7}.light .o2{background:#ec4899}.light .o3{background:#ef4444}@keyframes drift{to{transform:translate(8vw,-5vh) rotate(25deg) scale(1.2)}}
.container{width:min(1180px,calc(100% - 34px));margin:0 auto;padding:28px 0 56px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:28px}.brand{display:flex;align-items:center;gap:14px}.brand-logo{width:58px;height:58px;object-fit:cover;border-radius:16px;border:1px solid rgba(255,255,255,.16);box-shadow:0 12px 40px rgba(34,211,238,.20),0 0 28px rgba(139,92,246,.16);transition:.25s ease}.brand-logo:hover{transform:translateY(-2px) scale(1.03);box-shadow:0 16px 50px rgba(34,211,238,.30),0 0 36px rgba(139,92,246,.24)}.brand h1{font-size:21px;margin:0}.brand p{margin:3px 0 0;color:var(--muted);font-size:13px}.pill{border:1px solid var(--line);background:rgba(255,255,255,.05);backdrop-filter:blur(18px);padding:9px 13px;border-radius:999px;color:var(--muted);font-size:12px}.top-actions{display:flex;align-items:center;gap:9px}.theme-picker{position:relative}.theme-menu{position:absolute;top:50px;right:0;z-index:130;min-width:150px;padding:7px;border:1px solid var(--line);border-radius:16px;background:rgba(12,14,24,.94);backdrop-filter:blur(22px) saturate(150%);-webkit-backdrop-filter:blur(22px) saturate(150%);box-shadow:0 18px 50px rgba(0,0,0,.35)}.theme-menu[hidden]{display:none}.theme-menu button{width:100%;display:flex;align-items:center;gap:9px;padding:10px 11px;border:0;border-radius:11px;background:transparent;color:var(--text);font:inherit;font-size:12px;cursor:pointer;text-align:right}.theme-menu button:hover,.theme-menu button.active{background:var(--glass2);color:var(--text)}body.light .theme-menu{background:rgba(255,255,255,.94);border-color:rgba(124,58,237,.18);box-shadow:0 18px 50px rgba(124,58,237,.16)}body.light .theme-menu button{color:#17131f}body.light .theme-menu button:hover,body.light .theme-menu button.active{background:rgba(124,58,237,.08)}body.custom .theme-menu{background:var(--glass);border-color:var(--line)}
.theme-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);cursor:pointer;font-size:18px;transition:.25s ease}.theme-toggle:hover{transform:translateY(-2px) rotate(4deg);border-color:rgba(236,72,153,.45);box-shadow:0 10px 30px rgba(236,72,153,.16)}.hero{margin-bottom:22px}.hero h2{font-size:clamp(30px,5vw,54px);line-height:1.02;margin:0 0 10px;letter-spacing:-1.8px}.gradient{background:linear-gradient(90deg,#fff,#c4b5fd,#67e8f9,#f9a8d4);-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{color:var(--muted);max-width:720px;margin:0;line-height:1.7}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px;min-width:0}.light .glass{background:linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.48))}.light .meta-row{background:rgba(255,255,255,.62)}.light .btn{background:rgba(255,255,255,.64);color:#17131f}.light .btn:hover{background:rgba(255,255,255,.86);color:#0f0b16}.light .toggle{background:rgba(255,255,255,.58);border-color:rgba(124,58,237,.18);color:#17131f}.light .notice{background:rgba(255,255,255,.62);color:#17131f}.light .gradient{background:linear-gradient(90deg,#17131f,#4c1d95,#9d174d,#991b1b);-webkit-background-clip:text;background-clip:text;color:transparent}.light .sub,.light .empty,.light .hint,.light .pill,.light .brand p,.light .footer,.light .meta-label{color:#17131f}.light .status{color:#17131f}.light .status.on{color:#111827;background:rgba(52,211,153,.18)}.light .status.off{color:#17131f;background:rgba(124,58,237,.08)}.light .btn.good,.light .btn.danger{color:#111827}.light .field label,.light .field input,.light .field select{color:#17131f}.light .field input::placeholder,.light .field select::placeholder{color:#4b3f52}.light .field input,.light .field select{background:rgba(255,255,255,.64)}.light .btn.primary{color:#17131f;text-shadow:none}.light .notice.ok,.light .notice.bad{color:#17131f}.light .theme-toggle{color:#17131f}.light .panel-brand-icon{overflow:hidden}.panel-brand-icon{overflow:hidden;display:grid;place-items:center}.panel-brand-icon img{width:100%;height:100%;object-fit:contain;border-radius:inherit;padding:7px;filter:none}.light .panel-brand-icon img{filter:none}
.icon{color:#17131f}.light .brand h1,.light .title{color:#17131f}.light .meta-row strong,.light .meta-row a,.light .meta-row span:last-child{color:#17131f}.hero h2{overflow-wrap:anywhere;word-break:break-word}.grid{overflow:visible}.glass{min-width:0;overflow:hidden;background:linear-gradient(145deg,var(--glass),rgba(255,255,255,.025));border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-radius:24px;padding:22px}.card{grid-column:span 6;min-width:0;transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease}.card:hover{transform:translateY(-4px);border-color:rgba(236,72,153,.42);box-shadow:0 28px 90px rgba(0,0,0,.5)}.wide{grid-column:span 12}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:18px;min-width:0}.card-head>div{min-width:0;flex:1 1 auto}.card-head>.status,.card-head>.status-wrap{flex:0 0 auto;white-space:nowrap}.status-wrap{display:inline-flex;align-items:center}.card-head .title{overflow-wrap:anywhere;word-break:break-word}.icon{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.2);font-size:21px}.title{font-size:18px;font-weight:750;margin:0 0 4px}.sub{font-size:12px;color:var(--muted);margin:0}.status{font-size:11px;padding:7px 10px;border-radius:999px;border:1px solid var(--line)}.status.on{color:var(--good);background:rgba(52,211,153,.08)}.status.off{color:var(--muted)}.meta{display:grid;gap:10px;margin:18px 0}.meta-row{display:flex;justify-content:space-between;gap:12px;min-width:0;overflow:hidden;padding:11px 12px;border-radius:14px;background:var(--glass2);border:1px solid var(--line);transition:.2s}.meta-row:hover{transform:translateX(-2px);border-color:rgba(236,72,153,.28)}/* v5.5.4-final6 status system: the status indicator is an isolated neon dot. */
.neo-icon{position:relative;display:inline-grid;place-items:center;flex:0 0 44px;width:44px;height:44px;border-radius:15px;color:#8fdcff;background:linear-gradient(145deg,rgba(93,225,255,.13),rgba(124,58,237,.18) 58%,rgba(236,72,153,.10));border:1px solid rgba(120,180,255,.22);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 0 22px rgba(76,201,240,.08),0 8px 28px rgba(124,58,237,.08);transition:transform .25s ease,box-shadow .25s ease,color .25s ease,border-color .25s ease;overflow:visible}
.neo-icon:before{content:"";position:absolute;inset:5px;border-radius:11px;background:radial-gradient(circle,rgba(255,255,255,.07),transparent 68%);pointer-events:none}.neo-icon:after{display:none!important}.neo-icon svg{position:relative;z-index:1;width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 5px currentColor)}@keyframes iconSpark{0%,100%{transform:scale(.72);opacity:.45}50%{transform:scale(1.18);opacity:1}}
.neo-icon.card-icon{flex:0 0 50px;width:50px;height:50px;border-radius:17px}
.neo-icon.card-icon svg{width:25px;height:25px;stroke-width:1.7}
.neo-icon.meta-icon{flex-basis:18px;width:18px;height:18px;border:0;background:transparent;box-shadow:none;border-radius:0;color:#9fb5d7;overflow:visible}.neo-icon.meta-icon:before,.neo-icon.meta-icon:after,.neo-icon.inline-icon:before,.neo-icon.inline-icon:after{display:none}
.neo-icon.meta-icon svg{width:17px;height:17px;filter:drop-shadow(0 0 3px currentColor);stroke-width:1.8}
.neo-icon.inline-icon{flex-basis:18px;width:18px;height:18px;border:0;background:transparent;box-shadow:none;color:currentColor;border-radius:0}
.neo-icon.inline-icon svg{width:18px;height:18px;filter:none}
.neo-icon:hover{transform:translateY(-2px) scale(1.03);box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 0 26px rgba(76,201,240,.14)}
/* v5.6.4 small meta/inline icons: glow belongs to the SVG, never to the icon box. */
.neo-icon.meta-icon,.neo-icon.inline-icon{background:transparent!important;border:0!important;border-color:transparent!important;box-shadow:none!important;border-radius:0!important;overflow:visible!important}
.neo-icon.meta-icon:hover,.neo-icon.inline-icon:hover{background:transparent!important;border:0!important;border-color:transparent!important;box-shadow:none!important;border-radius:0!important;transform:none!important}
.neo-icon.meta-icon svg{background:transparent!important;box-shadow:none!important;filter:drop-shadow(0 0 2px currentColor) drop-shadow(0 0 5px currentColor)!important}
.neo-icon.inline-icon svg{background:transparent!important;box-shadow:none!important;filter:drop-shadow(0 0 2px currentColor)!important}
body.custom .neo-icon.meta-icon,body.custom .neo-icon.inline-icon{background:transparent!important;border:0!important;border-color:transparent!important;box-shadow:none!important}
body.custom .neo-icon.meta-icon svg,body.custom .neo-icon.inline-icon svg{background:transparent!important;box-shadow:none!important}

/* v5.6.4: light theme icon polish — keep SVG icons readable and visually matched to the light glass UI. */
.light .neo-icon{color:#6d28d9;background:linear-gradient(145deg,rgba(255,255,255,.92),rgba(237,233,254,.72) 58%,rgba(252,231,243,.68));border-color:rgba(109,40,217,.20);box-shadow:inset 0 1px 0 rgba(255,255,255,.95),0 8px 24px rgba(109,40,217,.12),0 0 18px rgba(236,72,153,.08)}
.light .neo-icon:before{background:radial-gradient(circle,rgba(255,255,255,.72),transparent 68%)}
.light .neo-icon svg{filter:drop-shadow(0 1px 2px rgba(109,40,217,.16))}
.light .neo-icon.meta-icon{color:#6d28d9;background:transparent;border-color:transparent;box-shadow:none}
.light .neo-icon.inline-icon{color:currentColor;background:transparent;border-color:transparent;box-shadow:none}
.light .resource-stat .neo-icon{color:#6d28d9}
.light .resource-stat.cpu .neo-icon{color:#0369a1}
.light .resource-stat.ram .neo-icon{color:#be185d}
.light .resource-stat.disk .neo-icon{color:#c2410c}
.light .theme-toggle{background:rgba(255,255,255,.78);border-color:rgba(109,40,217,.18);box-shadow:0 6px 20px rgba(109,40,217,.10)}
.icon-backup,.icon-cpu{color:#67e8f9}.icon-panel,.icon-ram{color:#8ab4ff}.icon-telegram,.icon-link{color:#6ea8ff}.icon-settings,.icon-dashboard{color:#b58cff}.icon-account{color:#ff8bc7}.icon-clock{color:#ffd166}.icon-health{color:#59e39a}.icon-chart,.icon-disk{color:#ff9f68}.icon-activity,.icon-traffic{color:#65e6d1}.icon-test{color:#ffe36e}.icon-download{color:#70d7ff}.icon-trash{color:#ff7f96}.icon-rocket{color:#ff78c8}
.status{display:inline-flex;align-items:center;gap:7px;font-size:11px;padding:7px 10px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.status-dot{display:inline-block!important;position:relative!important;box-sizing:border-box!important;flex:0 0 10px!important;width:10px!important;min-width:10px!important;max-width:10px!important;height:10px!important;min-height:10px!important;max-height:10px!important;padding:0!important;margin:0!important;border:0!important;border-radius:50%!important;overflow:visible!important;vertical-align:middle!important;align-self:center!important;background:#43e6a0!important;opacity:1!important;transform:translateZ(0);will-change:transform,opacity,box-shadow;animation:statusBreath 1.6s ease-in-out infinite!important}.status-dot::after{content:none!important}.status-dot.status-ok{background:#43e6a0!important;color:#43e6a0!important}.status-dot.status-info{background:#58cfff!important;color:#58cfff!important}.status-dot.status-warn{background:#ffd45a!important;color:#ffd45a!important}.status-dot.status-bad{background:#ff526f!important;color:#ff526f!important}.status-dot.status-off{background:#8b96ad!important;color:#8b96ad!important;box-shadow:none!important;animation:none!important}.logo-fallback-text{display:none;font-weight:900;font-size:16px;letter-spacing:-.5px;color:#8fdcff;text-shadow:0 0 12px #6ee7ff}.logo-fallback .logo-fallback-text{display:block}.logo-fallback img{display:none!important}.panel-brand-icon{position:relative}.panel-brand-icon:after{display:none!important}.status.status-ok .status-dot{animation:statusBreath 1.6s ease-in-out infinite!important}.status.status-bad .status-dot{animation:statusBreath 1.6s ease-in-out infinite!important}.status.status-warn .status-dot{animation:statusBreath 1.6s ease-in-out infinite!important}.status.status-info .status-dot{animation:statusBreath 1.6s ease-in-out infinite!important}.status.status-ok{color:#8ff0c2;background:rgba(67,227,154,.075);border-color:rgba(67,227,154,.20)}.status.status-info{color:#9ed4ff;background:rgba(97,183,255,.075);border-color:rgba(97,183,255,.20)}.status.status-warn{color:#ffe58d;background:rgba(255,212,92,.075);border-color:rgba(255,212,92,.20)}.status.status-bad{color:#ff9aad;background:rgba(255,94,120,.075);border-color:rgba(255,94,120,.20)}.status.status-off{color:var(--muted);background:rgba(124,138,165,.055);border-color:var(--line)}
@keyframes statusBreath{0%,100%{opacity:.72;filter:brightness(.92);box-shadow:0 0 2px currentColor,0 0 5px currentColor}50%{opacity:1;filter:brightness(1.35);box-shadow:0 0 4px currentColor,0 0 9px currentColor}}


.has-status{overflow:visible!important}
.has-status .status-dot{z-index:3;position:relative!important}
.status{overflow:visible!important}
.meta-label{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:7px;min-width:0;overflow:visible}.meta-row.has-status{overflow:visible}.meta-row.has-status .meta-label{overflow:visible}.meta-label .neo-icon{flex:0 0 18px}.meta-row strong,.meta-row a{flex:0 1 auto;color:var(--text)}.meta-row a{color:var(--accent2);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}.meta-row span:last-child,.meta-row strong,.meta-row a{font-size:12px;max-width:65%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}.actions .btn{min-height:44px}.actions form{display:flex}.actions form .btn{height:100%}.btn{display:inline-flex;min-width:132px;max-width:100%;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);border-radius:14px;padding:12px 15px;color:var(--text);text-decoration:none;font-weight:700;font-size:13px;cursor:pointer;background:var(--glass2);transition:.22s;position:relative;overflow:hidden;white-space:normal;text-align:center}.btn:before{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 20%,rgba(255,255,255,.18),transparent 80%);transform:translateX(-120%);transition:.55s}.btn:hover:before{transform:translateX(120%)}.btn:hover{transform:translateY(-2px);background:rgba(255,255,255,.14);border-color:rgba(236,72,153,.28);box-shadow:0 10px 24px rgba(124,58,237,.16)}.btn:active{transform:translateY(0) scale(.98)}.btn.primary{border-color:transparent;background:linear-gradient(135deg,#7c3aed,#db2777,#ef4444);background-size:180% 180%;animation:buttonGlow 6s ease infinite;box-shadow:0 10px 28px rgba(219,39,119,.22)}@keyframes buttonGlow{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}.btn.good{border-color:rgba(52,211,153,.2);background:rgba(52,211,153,.09);color:#b7f7dc}.btn.danger{border-color:rgba(251,113,133,.2);background:rgba(251,113,133,.08);color:#fecdd3}.btn.full{width:100%}form{margin:0}.field{margin-bottom:16px}.field label{display:block;font-size:12px;color:var(--text);margin-bottom:7px}.field input,.field select{width:100%;border:1px solid var(--line);background:var(--glass2);color:var(--text);border-radius:14px;padding:13px 14px;outline:none;font-size:13px}.field input:focus,.field select:focus{border-color:rgba(103,232,249,.65);box-shadow:0 0 0 4px rgba(34,211,238,.08)}.hint{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.6}.toggle{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px;border-radius:16px;background:rgba(0,0,0,.14);border:1px solid rgba(255,255,255,.07);margin-bottom:14px}.toggle input{accent-color:#db2777;width:18px;height:18px}.notice{margin-bottom:16px;padding:14px 16px;border-radius:16px;border:1px solid var(--line);background:rgba(255,255,255,.055);color:#dbeafe}.notice.ok{border-color:rgba(52,211,153,.25);background:rgba(52,211,153,.07)}.notice.bad{border-color:rgba(251,113,133,.25);background:rgba(251,113,133,.07)}.footer{text-align:center;color:#667085;font-size:11px;padding-top:28px}.login{width:min(460px,100%);margin:9vh auto}.login .glass{padding:30px}.login-card{text-align:center}.login-icon-wrap{display:flex;justify-content:center;margin-bottom:2px}.login-icon{width:66px!important;height:66px!important;flex-basis:66px!important;border-radius:20px!important}.login-icon svg{width:31px!important;height:31px!important;stroke-width:1.7}.login-card form{text-align:right}.empty{color:var(--muted);font-size:13px;line-height:1.7}.activity-list{display:grid;gap:9px}.activity-list .meta-row{margin:0}.activity-bad{border-color:rgba(251,113,133,.30)}.backup-controls .card-head{min-width:0}.backup-controls .empty{max-width:100%;overflow-wrap:anywhere}.backup-actions{width:100%;min-width:0}.backup-actions .btn{min-width:0;max-width:100%}.backup-actions form{min-width:0;max-width:100%}.backup-actions form .btn{min-width:0}@media(min-width:801px){.backup-actions .btn{min-width:0}.backup-actions form{flex:1}.backup-actions form .btn{width:100%}.backup-actions>a.btn{flex:1}}@media(max-width:800px){.card-head{gap:10px;align-items:center}.card-head>.status{font-size:10px;padding:6px 8px}.meta-row{align-items:center}.meta-label{flex:1 1 52%;}.meta-row span:last-child,.meta-row strong,.meta-row a{flex:0 1 48%;max-width:48%;}.top-actions{margin-right:auto}.actions .btn,.actions form{width:100%}.actions form .btn{width:100%}.card,.wide{grid-column:span 12}.topbar{align-items:flex-start}.pill{display:none}.container{width:calc(100% - 22px);max-width:1180px;padding-top:18px;min-width:0}.glass{border-radius:20px;padding:18px}.grid{grid-template-columns:minmax(0,1fr);width:100%;}.card,.wide{grid-column:1/-1;width:100%;}.topbar{flex-wrap:wrap;}.top-actions{margin-right:0;}.actions{min-width:0;}.meta-row a,.meta-row span:last-child,.meta-row strong{max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}.brand{min-width:0;}.brand>div{min-width:0;}}

/* v5.5.3 glass navigation drawer + mobile hero fix */
.menu-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);cursor:pointer;font-size:21px;line-height:1;transition:.25s ease;display:inline-flex;align-items:center;justify-content:center}
.menu-toggle:hover{transform:translateY(-2px);border-color:rgba(236,72,153,.45);box-shadow:0 10px 30px rgba(124,58,237,.18)}
.menu-toggle .hamb{display:inline-block;transition:transform .3s ease}.menu-toggle.open .hamb{transform:rotate(90deg)}
.drawer-backdrop{position:fixed;inset:0;background:rgba(3,5,12,.46);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);opacity:0;visibility:hidden;transition:.28s ease;z-index:90}.drawer-backdrop.open{opacity:1;visibility:visible}
.drawer{position:fixed;top:14px;right:14px;bottom:14px;width:min(330px,calc(100vw - 28px));z-index:100;border:1px solid var(--line);border-radius:28px;background:linear-gradient(145deg,rgba(15,18,31,.88),rgba(34,19,55,.72));box-shadow:0 28px 100px rgba(0,0,0,.55),0 0 70px rgba(124,58,237,.16);backdrop-filter:blur(30px) saturate(145%);-webkit-backdrop-filter:blur(30px) saturate(145%);transform:translateX(calc(100% + 30px));transition:transform .34s cubic-bezier(.2,.8,.2,1);overflow:auto;padding:18px}.drawer.open{transform:translateX(0)}
.drawer-head{display:flex;align-items:center;gap:12px;padding:8px 6px 18px;border-bottom:1px solid var(--line);margin-bottom:14px}.drawer-logo{width:52px;height:52px;border-radius:15px;object-fit:cover;border:1px solid rgba(255,255,255,.14);box-shadow:0 10px 30px rgba(124,58,237,.18)}.drawer-head h3{margin:0;font-size:17px}.drawer-head p{margin:3px 0 0;color:var(--muted);font-size:11px}.drawer-close{margin-right:auto;width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--glass2);color:var(--text);cursor:pointer;font-size:18px}
.drawer-section{font-size:11px;color:var(--muted);padding:8px 8px 7px}.drawer-nav{display:grid;gap:7px}.drawer-link{display:flex;align-items:center;gap:12px;min-height:48px;padding:11px 13px;border-radius:15px;border:1px solid transparent;color:var(--text);text-decoration:none;background:rgba(255,255,255,.035);transition:.2s ease;position:relative;overflow:hidden}.drawer-link:before{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent,rgba(236,72,153,.10),rgba(124,58,237,.10),transparent);transform:translateX(-110%);transition:.5s ease}.drawer-link:hover:before{transform:translateX(110%)}.drawer-link:hover{transform:translateX(-2px);border-color:rgba(236,72,153,.25);background:rgba(255,255,255,.07);box-shadow:0 10px 30px rgba(124,58,237,.10)}.drawer-icon{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(145deg,rgba(93,225,255,.10),rgba(139,92,246,.18));border:1px solid rgba(139,92,246,.24);font-size:17px;flex:0 0 auto;box-shadow:0 0 16px rgba(124,58,237,.10)}.drawer-icon:before,.drawer-icon:after{display:none}.drawer-link strong{font-size:13px}.drawer-link small{display:block;color:var(--muted);font-size:10px;margin-top:2px}.drawer-link.logout{border-color:rgba(251,113,133,.16);background:rgba(251,113,133,.055)}
body.light .drawer{background:linear-gradient(145deg,rgba(255,255,255,.86),rgba(252,236,255,.78));box-shadow:0 28px 90px rgba(124,58,237,.22),0 0 60px rgba(236,72,153,.10)}body.light .drawer-link{color:#17131f;background:rgba(255,255,255,.46)}body.light .drawer-link:hover{background:rgba(255,255,255,.72)}body.light .drawer-head h3,body.light .drawer-link strong{color:#17131f}body.light .drawer-close,body.light .menu-toggle{color:#17131f;background:rgba(255,255,255,.58)}body.light .drawer-section,body.light .drawer-head p,body.light .drawer-link small{color:#4b3f52}body.light .drawer-backdrop{background:rgba(70,25,75,.20)}
@media(max-width:800px){.hero{margin-top:8px;margin-bottom:18px}.hero h2{font-size:clamp(25px,8vw,34px);line-height:1.25;letter-spacing:-.8px;max-width:100%;overflow-wrap:anywhere;word-break:break-word}.hero p{font-size:12px;line-height:1.8}.top-actions{gap:7px}}

/* v5.6.4 visual polish: consistent light surfaces, language selector and cleaner telemetry */
.language-picker{position:relative}.language-svg{width:21px;height:21px;display:block;filter:drop-shadow(0 0 7px currentColor);transition:.25s ease}.language-toggle:hover .language-svg{transform:rotate(8deg) scale(1.05);filter:drop-shadow(0 0 11px currentColor)}.lang-code{display:grid;place-items:center;width:27px;height:23px;border-radius:8px;font-size:9px;font-weight:900;letter-spacing:.6px;color:var(--accent2);background:color-mix(in srgb,var(--accent2) 10%,transparent);border:1px solid color-mix(in srgb,var(--accent2) 24%,transparent);box-shadow:0 0 14px color-mix(in srgb,var(--accent2) 14%,transparent)}.language-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);cursor:pointer;font-size:18px;transition:.25s ease}.language-toggle:hover{transform:translateY(-2px);border-color:rgba(34,211,238,.45);box-shadow:0 10px 30px rgba(34,211,238,.14)}.language-menu{position:absolute;top:50px;right:0;z-index:131;min-width:155px;padding:7px;border:1px solid var(--line);border-radius:16px;background:rgba(12,14,24,.94);backdrop-filter:blur(22px) saturate(150%);-webkit-backdrop-filter:blur(22px) saturate(150%);box-shadow:0 18px 50px rgba(0,0,0,.35)}.language-menu[hidden]{display:none}.language-menu button{width:100%;display:flex;align-items:center;gap:9px;padding:10px 11px;border:0;border-radius:11px;background:transparent;color:var(--text);cursor:pointer;font:inherit;text-align:right}.language-menu button:hover,.language-menu button.active{background:rgba(124,58,237,.12)}body.light .language-menu{background:rgba(255,255,255,.96);border-color:rgba(124,58,237,.18);box-shadow:0 18px 50px rgba(124,58,237,.18)}body.light .language-menu button{color:#26162d}body.light .language-menu button:hover,body.light .language-menu button.active{background:rgba(124,58,237,.08)}
body.light{color-scheme:light}body.light .glass{background:linear-gradient(145deg,rgba(255,255,255,.90),rgba(250,243,255,.78))!important;border-color:rgba(124,58,237,.16)!important;box-shadow:0 18px 55px rgba(77,37,102,.10),inset 0 1px 0 rgba(255,255,255,.90)}body.light .card:hover{box-shadow:0 22px 65px rgba(77,37,102,.13),inset 0 1px 0 rgba(255,255,255,.95)}body.light .meta-row,body.light .resource-stat,body.light .chart-bar,body.light .theme-toggle,body.light .menu-toggle{background:rgba(255,255,255,.68)!important;border-color:rgba(124,58,237,.14)!important;color:#26162d}body.light .resource-stat{box-shadow:0 8px 24px rgba(77,37,102,.07),inset 0 1px 0 rgba(255,255,255,.95)}body.light .resource-plot{background:linear-gradient(145deg,rgba(255,255,255,.84),rgba(246,238,255,.72))!important;border-color:rgba(124,58,237,.15)!important;box-shadow:0 14px 42px rgba(77,37,102,.08),inset 0 1px 0 rgba(255,255,255,.92)}body.light .resource-stat:after{opacity:.12}body.light .grid-lines line{stroke:#6b5875;stroke-opacity:.10}body.light .resource-stat small,body.light .resource-stat strong,body.light .plot-head b{color:#26162d}body.light .plot-kicker{color:#7c3aed}body.light .y-axis,body.light .legend,body.light .updated,body.light .resource-stat small{color:#735c78}body.light .chart-bar{box-shadow:inset 0 0 0 1px rgba(124,58,237,.08)}body.light .field input,body.light .field select,body.light textarea{background:rgba(255,255,255,.82)!important;color:#26162d!important;border-color:rgba(124,58,237,.16)!important}body.light .btn:not(.primary){background:rgba(255,255,255,.74);color:#3d2450;border-color:rgba(124,58,237,.16)}body.light .sub,body.light .empty,body.light .hint,body.light .brand p,body.light .footer{color:#735c78}body.light .brand h1,body.light .title{color:#26162d}body.light .gradient{background:linear-gradient(90deg,#5b21b6,#7c3aed,#db2777);-webkit-background-clip:text;background-clip:text;color:transparent}
body.light .card-icon,body.light .hero-icon{filter:none}body.light .neo-icon{background:transparent!important;border-color:transparent!important;box-shadow:none!important}body.light .neo-icon svg{filter:drop-shadow(0 0 7px currentColor)}
body.light .drawer{background:linear-gradient(145deg,rgba(255,255,255,.96),rgba(248,240,255,.92))!important;border-color:rgba(124,58,237,.16)!important}.lang-en .hero{text-align:left}.lang-en .card-head,.lang-en .plot-head,.lang-en .topbar{direction:ltr}.lang-en .meta-row,.lang-en .resource-stat{direction:ltr}.lang-ru .hero{text-align:left}.lang-ru .card-head,.lang-ru .plot-head,.lang-ru .topbar{direction:ltr}.lang-ru .meta-row,.lang-ru .resource-stat{direction:ltr}html[dir="ltr"] .language-menu,html[dir="ltr"] .theme-menu{right:auto;left:0}html[dir="ltr"] .updated{margin-left:auto;margin-right:0}html[dir="ltr"] .drawer{left:auto;right:0}html[dir="ltr"] .plot-body{padding-left:38px;padding-right:0}html[dir="ltr"] .y-axis{left:0;right:auto;align-items:flex-end}
@media(max-width:720px){.top-actions{flex-wrap:wrap}.language-toggle,.theme-toggle{width:44px;min-width:44px}.language-menu,.theme-menu{top:48px}.resource-stat{min-height:68px}}


"""



def ui_theme_css():
    """Return the admin-selected CUSTOM theme CSS. Light/dark stay fixed."""
    try:
        u = idont_load_ui_settings()
    except Exception:
        return ""
    t = u.get("theme", {})
    preset = str(t.get("preset", "ultra-dark"))
    presets = {
        "ultra-dark": {"bg":"#050507","bg2":"#0b0b10","text":"#f7f7fb","muted":"#9ca3b8","line":"rgba(255,255,255,.12)","glass":"rgba(12,12,18,.70)","glass2":"rgba(255,255,255,.055)","accent":"#7c3aed","accent2":"#22d3ee","pink":"#ec4899","shadow":"rgba(0,0,0,.56)"},
        "aurora": {"bg":"#07111b","bg2":"#0b1624","text":"#f4f8ff","muted":"#9fb2c9","line":"rgba(148,197,255,.16)","glass":"rgba(12,12,18,.70)","glass2":"rgba(255,255,255,.055)","accent":"#8b5cf6","accent2":"#22d3ee","pink":"#ec4899","shadow":"rgba(0,0,0,.50)"},
        "midnight": {"bg":"#0b0613","bg2":"#14091d","text":"#fff7ff","muted":"#b6a5bd","line":"rgba(236,180,255,.15)","glass":"rgba(12,12,18,.70)","glass2":"rgba(255,255,255,.055)","accent":"#a855f7","accent2":"#ec4899","pink":"#f472b6","shadow":"rgba(20,0,30,.58)"},
        "cyber": {"bg":"#03090a","bg2":"#061314","text":"#effffb","muted":"#91aaa6","line":"rgba(45,212,191,.16)","glass":"rgba(4,17,17,.70)","glass2":"rgba(255,255,255,.045)","accent":"#22c55e","accent2":"#06b6d4","pink":"#14b8a6","shadow":"rgba(0,0,0,.60)"},
        "ocean": {"bg":"#041018","bg2":"#061b27","text":"#effcff","muted":"#8faeb8","line":"rgba(56,189,248,.16)","glass":"rgba(5,23,32,.68)","glass2":"rgba(255,255,255,.05)","accent":"#0ea5e9","accent2":"#2dd4bf","pink":"#22d3ee","shadow":"rgba(0,0,0,.54)"},
        "ruby": {"bg":"#100608","bg2":"#19090d","text":"#fff5f6","muted":"#bba0a5","line":"rgba(251,113,133,.16)","glass":"rgba(27,8,13,.70)","glass2":"rgba(255,255,255,.05)","accent":"#f43f5e","accent2":"#fb923c","pink":"#e11d48","shadow":"rgba(25,0,5,.60)"},
    }
    c = presets.get(preset, presets["ultra-dark"])
    try:
        op=max(.25,min(.95,float(t.get("glass_opacity",.58)))); blur=max(0,min(40,int(t.get("blur",18)))); radius=max(8,min(36,int(t.get("radius",20))))
        fs=max(11,min(22,int(t.get("font_size",15)))); glow=max(0,min(1,float(t.get("glow",.72)))); width=max(860,min(1500,int(t.get("content_width",1180)))); gap=max(8,min(32,int(t.get("card_gap",16)))); shadow=max(0,min(1,float(t.get("shadow_strength",.5))))
    except Exception:
        op,blur,radius,fs,glow,width,gap,shadow=.58,18,20,15,.72,1180,16,.5
    return f'''body.custom{{--bg:{c["bg"]};--bg2:{c["bg2"]};--text:{c["text"]};--muted:{c["muted"]};--line:{c["line"]};--glass:{c["glass"]};--glass2:{c["glass2"]};--accent:{c["accent"]};--accent2:{c["accent2"]};--pink:{c["pink"]};--shadow-color:{c["shadow"]};--theme-radius:{radius}px;--theme-blur:{blur}px;--theme-font:{fs}px;--theme-width:{width}px;--theme-gap:{gap}px;--theme-glow:{glow:.2f};--theme-shadow:{shadow:.2f};color:var(--text)!important;background:radial-gradient(circle at 15% 12%,color-mix(in srgb,var(--accent) 24%,transparent),transparent 31%),radial-gradient(circle at 86% 14%,color-mix(in srgb,var(--accent2) 18%,transparent),transparent 29%),radial-gradient(circle at 70% 90%,color-mix(in srgb,var(--pink) 16%,transparent),transparent 34%),linear-gradient(135deg,var(--bg),var(--bg2))!important}}
body.custom .container{{width:min(var(--theme-width),calc(100% - 34px))}}body.custom .grid{{gap:var(--theme-gap)}}body.custom .glass{{color:var(--text);background:linear-gradient(145deg,var(--glass),color-mix(in srgb,var(--glass2) 55%,transparent))!important;border-color:var(--line)!important;border-radius:var(--theme-radius)!important;backdrop-filter:blur(var(--theme-blur)) saturate(145%);-webkit-backdrop-filter:blur(var(--theme-blur)) saturate(145%)}}
body.custom .meta-row,body.custom .resource-stat,body.custom .chart-bar,body.custom .theme-toggle,body.custom .menu-toggle{{background:var(--glass2);border-color:var(--line);color:var(--text)}}body.custom .drawer{{background:linear-gradient(145deg,var(--glass),color-mix(in srgb,var(--accent) 12%,var(--glass)))!important;border-color:var(--line)!important}}body.custom .drawer-link{{color:var(--text);background:var(--glass2);border-color:transparent}}body.custom .drawer-link:hover{{border-color:var(--accent2)}}body.custom .title,body.custom .brand h1,body.custom .field label,body.custom .field input,body.custom .field select{{color:var(--text)}}body.custom .sub,body.custom .empty,body.custom .hint,body.custom .pill,body.custom .brand p,body.custom .footer,body.custom .meta-label,body.custom .updated,body.custom .legend,body.custom .drawer-link small{{color:var(--muted)}}body.custom .gradient{{background:linear-gradient(90deg,var(--text),var(--accent2),var(--pink));-webkit-background-clip:text;background-clip:text;color:transparent}}body.custom .btn.primary{{background:linear-gradient(135deg,var(--accent),var(--accent2))!important;color:#fff!important;box-shadow:0 10px 30px color-mix(in srgb,var(--accent) 35%,transparent)!important}}body.custom .btn:not(.primary){{background:var(--glass2);color:var(--text);border-color:var(--line)}}body.custom .field input,body.custom .field select,body.custom textarea{{background:var(--glass2);color:var(--text);border-color:var(--line)}}body.custom .neo-icon{{color:var(--accent2);background:linear-gradient(145deg,color-mix(in srgb,var(--accent2) 13%,transparent),color-mix(in srgb,var(--accent) 18%,transparent))!important;border-color:var(--line)!important}}body.custom .resource-plot{{background:linear-gradient(145deg,var(--glass),color-mix(in srgb,var(--pink) 10%,var(--glass)))!important;border-color:var(--line)!important}}body.custom .theme-toggle:hover,body.custom .menu-toggle:hover{{border-color:var(--accent2);box-shadow:0 10px 30px color-mix(in srgb,var(--accent2) 18%,transparent)}}body.custom .aurora .o1{{background:var(--accent)}}body.custom .aurora .o2{{background:var(--accent2)}}body.custom .aurora .o3{{background:var(--pink)}}body.custom .glass,body.custom .card{{box-shadow:0 24px 70px var(--shadow-color)}}body.custom .card:hover{{box-shadow:0 28px 90px var(--shadow-color)}}body.custom{{font-size:var(--theme-font)}}'''


def ui_language_menu():
    return """<div class="language-picker"><button class="language-toggle" id="languageToggle" type="button" aria-label="Language" title="Language"><svg class="language-svg" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 12h18M12 3c2.7 2.5 4.1 5.5 4.1 9s-1.4 6.5-4.1 9c-2.7-2.5-4.1-5.5-4.1-9S9.3 5.5 12 3Z" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/><path d="M5 7.3h14M5 16.7h14" fill="none" stroke="currentColor" stroke-width="1.1" stroke-linecap="round" opacity=".75"/></svg></button><div class="language-menu" id="languageMenu" hidden><button type="button" data-lang-choice="fa"><span class="lang-code">FA</span><span data-lang-label="fa">فارسی</span></button><button type="button" data-lang-choice="en"><span class="lang-code">EN</span><span data-lang-label="en">English</span></button><button type="button" data-lang-choice="ru"><span class="lang-code">RU</span><span data-lang-label="ru">Русский</span></button></div></div>"""


def ui_language_script():
    # Client-side localization keeps the backend/API unchanged and makes the
    # three-language switch instant. Custom/admin-entered text is left alone.
    return r"""(function(){
const LANG_KEY="idontpg-lang";
const maps={
 fa:{
  "Backup Information":"اطلاعات Backup","Panel Information":"اطلاعات پنل","Backup Controls":"تنظیمات Backup","Telegram Backup":"بکاپ تلگرام","Next Scheduled Backup":"Backup بعدی","Backup Activity":"فعالیت Backup","Disk Monitor":"فضای دیسک","Health Check":"وضعیت سرویس‌ها","Virtual Server Resources":"منابع سرور مجازی","Live server status":"وضعیت لحظه‌ای سرور","LIVE TELEMETRY":"پایش لحظه‌ای","Recent Activity":"فعالیت‌های اخیر","Recent activity count":"تعداد فعالیت‌های اخیر","System overview":"نمای کلی سیستم","Settings & delivery":"تنظیمات و ارسال","Scheduler & Backup":"Scheduler و Backup","Telegram Test":"تست تلگرام","Connection check":"بررسی اتصال","Account":"حساب کاربری","Login management":"مدیریت ورود","Logout":"خروج","End session":"پایان نشست","Main menu":"منوی اصلی","Manage Backups":"مدیریت Backupها","No backups found.":"هنوز Backupای پیدا نشد.","Backup count":"تعداد Backup","Total backup size":"حجم کل Backupها","Latest backup":"آخرین Backup","No backup created yet":"هنوز Backup ساخته نشده","Unavailable":"قابل دریافت نیست","Panel link":"لینک پنل","Panel status":"وضعیت پنل","Real Node usage":"مصرف واقعی Node","Not configured":"تنظیم نشده","Running":"در حال اجرا","Active":"فعال","Stopped":"متوقف","Bot Token":"Bot Token","Chat ID":"Chat ID","Topic ID":"Topic ID","Proxy":"Proxy","No proxy":"بدون Proxy","Telegram Settings":"تنظیمات Telegram","Send Test":"ارسال تست","Manage Backup":"مدیریت Backup","Run Manual Backup":"شروع Backup دستی","Send a Test Message to Telegram":"ارسال تست پیام به Telegram","Check the connection before enabling the scheduler.":"قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.","Next Backup":"Backup بعدی","Backup — Last 7 Days":"Backup هفت روز اخیر","Disk Space":"فضای دیسک","Service status":"وضعیت سرویس‌ها","CPU · RAM · Disk · Live monitoring":"CPU · RAM · Disk · پایش لحظه‌ای","Now":"اکنون","Updated":"به‌روزرسانی","minutes ago":"دقیقه پیش","hours ago":"ساعت پیش","days ago":"روز پیش","Just now":"همین الان","Private Administration":"مدیریت خصوصی","This area is for the main administrator only.":"این بخش فقط برای مدیر اصلی است.","Panel Login":"ورود به پنل","Enter the admin username and password to continue.":"برای ورود، نام کاربری و رمز عبور ادمین را وارد کنید.","Secure Login":"ورود امن","Initial Setup":"راه‌اندازی اولیه","Create Password & Login":"ساخت رمز و ورود","Admin Password":"رمز مدیر","Confirm Password":"تکرار رمز","New Password":"رمز جدید","Confirm New Password":"تکرار رمز جدید","Change Password":"تغییر رمز","Control Center":"مرکز مدیریت","Customization":"شخصی‌سازی","Appearance":"ظاهر","Primary color":"رنگ اصلی","Secondary color":"رنگ دوم","Background":"پس‌زمینه","Logo URL":"Logo URL","Logo width":"عرض Logo","Glow intensity":"شدت Glow","Glow & Motion":"Glow و حرکت","Animation speed":"سرعت انیمیشن","Text & Branding":"متن و برند","Panel name":"نام پنل","Dashboard title":"عنوان داشبورد","Backup title":"عنوان بکاپ","Traffic title":"عنوان حجم مصرفی","Recent activity title":"عنوان فعالیت‌های اخیر","Layout & User Experience":"چیدمان و تجربه کاربر","Dashboard content width":"عرض محتوای داشبورد","Card spacing":"فاصله بین کارت‌ها","Dashboard sections":"بخش‌های داشبورد","Traffic usage":"حجم مصرفی","Server statistics":"آمار سرور","Custom Button":"دکمه سفارشی","Custom Buttons":"دکمه‌های سفارشی","Name":"نام","Icon":"آیکون","Link":"لینک","Add":"افزودن","Delete":"حذف","Save Changes":"ذخیره تغییرات","Back":"بازگشت","Custom":"کاستوم","Light":"لایت","Dark":"دارک","Manual Backup":"بکاپ دستی","Automatic Scheduler":"زمان‌بندی خودکار","Backup interval (hours)":"بازه Backup (ساعت)","Include PG-Node":"شامل PG-Node شود","Save & Start":"ذخیره و شروع","Stop":"توقف","Create a full backup now.":"همین حالا یک Backup کامل بگیرید.","A test message will be sent using the current settings.":"یک پیام آزمایشی با تنظیمات فعلی ارسال می‌شود.","Send Test Message":"ارسال پیام تست","Test message sent successfully.":"پیام تست با موفقیت ارسال شد.","Test message failed.":"ارسال پیام تست ناموفق بود.","Scheduler saved and started.":"Scheduler ذخیره و شروع شد.","Scheduler saved, but starting it failed.":"Scheduler ذخیره شد ولی شروع آن با خطا مواجه شد.","General settings saved.":"تنظیمات عمومی ذخیره شد.","Telegram settings saved successfully.":"تنظیمات Telegram با موفقیت ذخیره شد.","Account settings saved successfully.":"تنظیمات حساب با موفقیت ذخیره شد.","Settings saved and applied across the web panel.":"تنظیمات با موفقیت ذخیره شد و روی کل وب‌پنل اعمال شد.","Online":"آنلاین","Offline":"آفلاین","Configured":"تنظیم شده","Invalid":"نامعتبر","Telegram":"Telegram","Health":"سلامت","Panel":"پنل","Backup":"Backup","Resources":"منابع","Status":"وضعیت","Next":"بعدی","Period":"بازه","Test message":"پیام تست","Send test message":"ارسال پیام تست"
 },
 en:{
  "داشبورد":"Dashboard","نمای کلی سیستم":"System overview","بکاپ تلگرام":"Telegram Backup","تنظیمات و ارسال":"Settings & delivery","تنظیمات بکاپ":"Backup Settings","Scheduler و Backup":"Scheduler & Backup","تست تلگرام":"Telegram Test","بررسی اتصال":"Connection check","حساب کاربری":"Account","مدیریت ورود":"Login management","خروج":"Logout","پایان نشست":"End session","منوی اصلی":"Main menu",
  "مدیریت Backupها":"Manage Backups","Backupهای موجود را مشاهده، دانلود یا حذف کنید.":"View, download or delete available backups.","هنوز Backupای پیدا نشد.":"No backups found.","اطلاعات Backup":"Backup Information","تعداد Backup":"Backup count","حجم کل Backupها":"Total backup size","آخرین Backup":"Latest backup","هنوز Backup ساخته نشده":"No backup created yet","قابل دریافت نیست":"Unavailable",
  "لینک پنل":"Panel link","وضعیت پنل":"Panel status","مصرف واقعی Node":"Real Node usage","تنظیم نشده":"Not configured","در حال اجرا":"Running","فعال":"Active","متوقف":"Stopped","بکاپ تلگرام":"Telegram Backup","Telegram Backup":"Telegram Backup","Bot Token":"Bot Token","Chat ID":"Chat ID","Topic ID":"Topic ID","Proxy":"Proxy","بدون Proxy":"No proxy","تنظیمات Telegram":"Telegram Settings","ارسال تست":"Send Test","تنظیمات Backup":"Backup Settings","Backup Controls":"Backup Controls","مدیریت Backup":"Manage Backup","شروع Backup دستی":"Run Manual Backup","ارسال تست پیام به Telegram":"Send a Test Message to Telegram","قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.":"Check the connection before enabling the scheduler.",
  "Backup بعدی":"Next Backup","Next Scheduled Backup":"Next Scheduled Backup","Backup هفت روز اخیر":"Backup — Last 7 Days","Backup Activity":"Backup Activity","فضای دیسک":"Disk Space","Disk Monitor":"Disk Monitor","وضعیت سرویس‌ها":"Service status","Health Check":"Health Check","منابع سرور مجازی":"Virtual Server Resources","CPU · RAM · Disk · پایش لحظه‌ای":"CPU · RAM · Disk · Live monitoring","وضعیت لحظه‌ای سرور":"Live server status","LIVE TELEMETRY":"LIVE TELEMETRY","به‌روزرسانی":"Updated","اکنون":"Now","فعالیت‌های اخیر":"Recent Activity","آخرین فعالیت‌ها":"Recent Activity","Recent Activity":"Recent Activity","تعداد فعالیت‌های اخیر":"Recent activity count",
  "همین الان":"Just now","دقیقه پیش":"minutes ago","ساعت پیش":"hours ago","روز پیش":"days ago","روز":"days","ساعت":"hours","دقیقه":"minutes",
  "مدیریت خصوصی":"Private Administration","این بخش فقط برای مدیر اصلی است.":"This area is for the main administrator only.","ورود به پنل":"Panel Login","برای ورود، نام کاربری و رمز عبور ادمین را وارد کنید.":"Enter the admin username and password to continue.","ورود امن":"Secure Login","راه‌اندازی اولیه":"Initial Setup","ساخت رمز و ورود":"Create Password & Login","رمز مدیر":"Admin Password","تکرار رمز":"Confirm Password","رمز جدید":"New Password","تکرار رمز جدید":"Confirm New Password","تغییر رمز":"Change Password",
  "مرکز مدیریت":"Control Center","شخصی‌سازی کامل ظاهر، داشبورد و اجزای وب‌پنل.":"Customize the complete look, dashboard and web-panel components.","شخصی‌سازی":"Customization","شش تم آماده":"Six Ready Themes","کاربر عادی هیچ گزینه‌ای برای انتخاب تم نمی‌بیند.":"Regular users cannot choose the admin theme.","ظاهر":"Appearance","رنگ اصلی":"Primary color","رنگ دوم":"Secondary color","Background":"Background","Logo URL":"Logo URL","عرض Logo":"Logo width","شدت Glow":"Glow intensity","Glow و حرکت":"Glow & Motion","سرعت انیمیشن":"Animation speed","متن و برند":"Text & Branding","نام پنل":"Panel name","عنوان داشبورد":"Dashboard title","عنوان بکاپ":"Backup title","عنوان حجم مصرفی":"Traffic title","عنوان فعالیت‌های اخیر":"Recent activity title","چیدمان و تجربه کاربر":"Layout & User Experience","عرض محتوای داشبورد":"Dashboard content width","فاصله بین کارت‌ها":"Card spacing","بخش‌های داشبورد":"Dashboard sections","حجم مصرفی":"Traffic usage","آمار سرور":"Server statistics","دکمه سفارشی":"Custom Button","دکمه‌های سفارشی":"Custom Buttons","نام":"Name","آیکون":"Icon","لینک":"Link","افزودن":"Add","حذف":"Delete","ذخیره تغییرات":"Save Changes","بازگشت":"Back","شش تم آماده":"Six Ready Themes","کاستوم":"Custom","لایت":"Light","دارک":"Dark",
  "بکاپ دستی":"Manual Backup","زمان‌بندی خودکار":"Automatic Scheduler","بازه Backup (ساعت)":"Backup interval (hours)","شامل PG-Node شود":"Include PG-Node","ذخیره و شروع":"Save & Start","توقف":"Stop","همین حالا یک Backup کامل بگیرید.":"Create a full backup now.","یک پیام آزمایشی با تنظیمات فعلی ارسال می‌شود.":"A test message will be sent using the current settings.","ارسال پیام تست":"Send Test Message",
  "پیام تست با موفقیت ارسال شد.":"Test message sent successfully.","ارسال پیام تست ناموفق بود.":"Test message failed.","Scheduler ذخیره و شروع شد.":"Scheduler saved and started.","Scheduler ذخیره شد ولی شروع آن با خطا مواجه شد.":"Scheduler saved, but starting it failed.","تنظیمات عمومی ذخیره شد.":"General settings saved.","تنظیمات Telegram با موفقیت ذخیره شد.":"Telegram settings saved successfully.","تنظیمات حساب با موفقیت ذخیره شد.":"Account settings saved successfully.","تنظیمات با موفقیت ذخیره شد و روی کل وب‌پنل اعمال شد.":"Settings saved and applied across the web panel.",
  "در حال اجرا":"Running","آنلاین":"Online","آفلاین":"Offline","تنظیم شده":"Configured","تنظیم نشده":"Not configured","قابل دریافت نیست":"Unavailable","بدون Proxy":"No Proxy","نامعتبر":"Invalid"
 },
 ru:{
  "داشبورد":"Панель","نمای کلی سیستم":"Обзор системы","بکاپ تلگرام":"Резервная копия Telegram","تنظیمات و ارسال":"Настройки и отправка","تنظیمات بکاپ":"Настройки бэкапа","Scheduler و Backup":"Планировщик и бэкап","تست تلگرام":"Тест Telegram","بررسی اتصال":"Проверка подключения","حساب کاربری":"Аккаунт","مدیریت ورود":"Управление входом","خروج":"Выйти","پایان نشست":"Завершить сеанс","منوی اصلی":"Главное меню",
  "مدیریت Backupها":"Управление бэкапами","Backupهای موجود را مشاهده، دانلود یا حذف کنید.":"Просмотр, скачивание или удаление бэкапов.","هنوز Backupای پیدا نشد.":"Бэкапы не найдены.","اطلاعات Backup":"Информация о бэкапе","تعداد Backup":"Количество бэкапов","حجم کل Backupها":"Общий размер бэкапов","آخرین Backup":"Последний бэкап","هنوز Backup ساخته نشده":"Бэкапы ещё не созданы","قابل دریافت نیست":"Недоступно","لینک پنل":"Ссылка панели","وضعیت پنل":"Статус панели","مصرف واقعی Node":"Реальный трафик Node","تنظیم نشده":"Не настроено","در حال اجرا":"Работает","فعال":"Активно","متوقف":"Остановлено","Bot Token":"Bot Token","Chat ID":"Chat ID","Topic ID":"Topic ID","Proxy":"Прокси","بدون Proxy":"Без прокси","تنظیمات Telegram":"Настройки Telegram","ارسال تست":"Тестовая отправка","تنظیمات Backup":"Настройки бэкапа","مدیریت Backup":"Управление бэкапом","شروع Backup دستی":"Запустить бэкап вручную","ارسال تست پیام به Telegram":"Отправить тестовое сообщение в Telegram","قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.":"Проверьте подключение перед включением планировщика.",
  "Backup بعدی":"Следующий бэкап","Backup هفت روز اخیر":"Бэкапы за 7 дней","فضای دیسک":"Дисковое пространство","وضعیت سرویس‌ها":"Состояние сервисов","منابع سرور مجازی":"Ресурсы виртуального сервера","CPU · RAM · Disk · پایش لحظه‌ای":"CPU · RAM · Disk · Мониторинг в реальном времени","وضعیت لحظه‌ای سرور":"Состояние сервера в реальном времени","فعالیت‌های اخیر":"Последние действия","آخرین فعالیت‌ها":"Последние действия","تعداد فعالیت‌های اخیر":"Количество последних действий","همین الان":"Только что","دقیقه پیش":"мин. назад","ساعت پیش":"ч. назад","روز پیش":"дн. назад","روز":"дн.","ساعت":"ч.","دقیقه":"мин.","به‌روزرسانی":"Обновлено","اکنون":"Сейчас",
  "مدیریت خصوصی":"Приватное администрирование","این بخش فقط برای مدیر اصلی است.":"Этот раздел доступен только главному администратору.","ورود به پنل":"Вход в панель","ورود امن":"Безопасный вход","راه‌اندازی اولیه":"Первичная настройка","ساخت رمز و ورود":"Создать пароль и войти","رمز مدیر":"Пароль администратора","تکرار رمز":"Повтор пароля","رمز جدید":"Новый пароль","تکرار رمز جدید":"Повтор нового пароля","تغییر رمز":"Изменить пароль",
  "مرکز مدیریت":"Центр управления","شخصی‌سازی کامل ظاهر، داشبورد و اجزای وب‌پنل.":"Полная настройка внешнего вида, панели и компонентов.","شخصی‌سازی":"Настройка","شش تم آماده":"Шесть готовых тем","کاربر عادی هیچ گزینه‌ای برای انتخاب تم نمی‌بیند.":"Обычные пользователи не видят выбор темы.","ظاهر":"Внешний вид","رنگ اصلی":"Основной цвет","رنگ دوم":"Вторичный цвет","Background":"Фон","Logo URL":"URL логотипа","عرض Logo":"Ширина логотипа","شدت Glow":"Интенсивность свечения","Glow و حرکت":"Свечение и анимация","سرعت انیمیشن":"Скорость анимации","متن و برند":"Текст и бренд","نام پنل":"Название панели","عنوان داشبورد":"Заголовок панели","عنوان بکاپ":"Заголовок бэкапа","عنوان حجم مصرفی":"Заголовок трафика","عنوان فعالیت‌های اخیر":"Заголовок последних действий","چیدمان و تجربه کاربر":"Макет и UX","عرض محتوای داشبورد":"Ширина содержимого","فاصله بین کارت‌ها":"Расстояние между карточками","بخش‌های داشبورد":"Разделы панели","حجم مصرفی":"Использование трафика","آمار سرور":"Статистика сервера","دکمه سفارشی":"Пользовательская кнопка","دکمه‌های سفارشی":"Пользовательские кнопки","نام":"Название","آیکون":"Иконка","لینک":"Ссылка","افزودن":"Добавить","حذف":"Удалить","ذخیره تغییرات":"Сохранить изменения","بازگشت":"Назад","کاستوم":"Пользовательская","لایت":"Светлая","دارک":"Тёмная",
  "بکاپ دستی":"Ручной бэкап","زمان‌بندی خودکار":"Автопланировщик","بازه Backup (ساعت)":"Интервал бэкапа (часы)","شامل PG-Node شود":"Включить PG-Node","ذخیره و شروع":"Сохранить и запустить","توقف":"Остановить","همین حالا یک Backup کامل بگیرید.":"Создать полный бэкап сейчас.","یک پیام آزمایشی با تنظیمات فعلی ارسال می‌شود.":"Будет отправлено тестовое сообщение с текущими настройками.","ارسال پیام تست":"Отправить тестовое сообщение",
  "پیام تست با موفقیت ارسال شد.":"Тестовое сообщение отправлено.","ارسال پیام تست ناموفق بود.":"Не удалось отправить тестовое сообщение.","Scheduler ذخیره و شروع شد.":"Планировщик сохранён и запущен.","Scheduler ذخیره شد ولی شروع آن با خطا مواجه شد.":"Планировщик сохранён, но запуск завершился ошибкой.","تنظیمات عمومی ذخیره شد.":"Общие настройки сохранены.","تنظیمات Telegram با موفقیت ذخیره شد.":"Настройки Telegram сохранены.","تنظیمات حساب با موفقیت ذخیره شد.":"Настройки аккаунта сохранены.","تنظیمات با موفقیت ذخیره شد و روی کل وب‌پنل اعمال شد.":"Настройки сохранены и применены ко всей панели.","آنلاین":"Онлайн","آفلاین":"Офлайн","قابل دریافت نیست":"Недоступно","بدون Proxy":"Без прокси","نامعتبر":"Недействительно"
 }
};
// Extra mixed-language phrases and server-side notices. Keep the canonical
// Persian source as the stable key so switching languages never compounds.
Object.assign(maps.en,{
 "اطلاعات ربات، مقصد، Topic، پروکسی و زمان‌بندی را تنظیم کنید؛ سپس Scheduler را شروع کنید.":"Configure the bot, destination, Topic, proxy and schedule, then start the Scheduler.",
 "با Chat ID و Topic فعلی ارسال می‌شود.":"Sent using the current Chat ID and Topic.",
 "توکن BotFather را وارد کنید.":"Enter your BotFather token.",
 "اختیاری. اگر Proxy ندارید خالی بگذارید.":"Optional. Leave blank if you do not use a proxy.",
 "شماره Topic را وارد کنید؛ لینک Topic تلگرام هم قابل قبول است.":"Enter the Topic number; a Telegram Topic link is also accepted.",
 "زمان‌بندی را روشن/خاموش کنید، Backup دستی بگیرید یا مشخص کنید PG-Node هم همراه Backup ذخیره شود.":"Enable or disable scheduling, run a manual backup, or include PG-Node in the backup.",
 "هنوز فعالیتی ثبت نشده.":"No activity recorded yet.",
 "Backup پیدا نشد.":"Backup not found.",
 "خواندن Backup ناموفق بود.":"Failed to read backup.",
 "درخواست نامعتبر است.":"Invalid request.",
 "درخواست نامعتبر یا منقضی شده است. صفحه را دوباره باز کنید.":"Invalid or expired request. Reload the page.",
 "صفحه پیدا نشد.":"Page not found.",
 "تلاش دوباره":"Try again","ثبت تغییرات":"Save Changes","ذخیره همه تغییرات":"Save All Changes","تنظیمات عمومی":"General Settings","تنظیمات حساب":"Account Settings","تنظیمات Telegram":"Telegram Settings","تنظیمات از کد اصلی جدا ذخیره می‌شوند":"Settings are stored separately from the main code","توضیح داشبورد":"Dashboard description","عنوان مصرف":"Traffic title","عنوان فعالیت":"Activity title","عنوان کوچک زیر برند":"Brand subtitle","نمایش بخش‌ها":"Visible sections","اندازه فونت":"Font size","شفافیت Glass":"Glass opacity","گردی کارت":"Card radius","شدت سایه کارت":"Card shadow strength","رنگ":"Color","بازه":"Interval","آزاد":"Available","استفاده‌شده":"Used"
});
Object.assign(maps.ru,{
 "اطلاعات ربات، مقصد، Topic، پروکسی و زمان‌بندی را تنظیم کنید؛ سپس Scheduler را شروع کنید.":"Настройте бота, назначение, Topic, прокси и расписание, затем запустите планировщик.",
 "با Chat ID و Topic فعلی ارسال می‌شود.":"Отправляется с текущими Chat ID и Topic.",
 "توکن BotFather را وارد کنید.":"Введите токен BotFather.",
 "اختیاری. اگر Proxy ندارید خالی بگذارید.":"Необязательно. Оставьте пустым, если прокси не используется.",
 "شماره Topic را وارد کنید؛ لینک Topic تلگرام هم قابل قبول است.":"Введите номер Topic; также можно указать ссылку на Topic Telegram.",
 "زمان‌بندی را روشن/خاموش کنید، Backup دستی بگیرید یا مشخص کنید PG-Node هم همراه Backup ذخیره شود.":"Включайте или отключайте расписание, создавайте бэкап вручную или включайте PG-Node в бэкап.",
 "هنوز فعالیتی ثبت نشده.":"Действий пока нет.","Backup پیدا نشد.":"Бэкап не найден.","خواندن Backup ناموفق بود.":"Не удалось прочитать бэкап.","درخواست نامعتبر است.":"Недействительный запрос.","درخواست نامعتبر یا منقضی شده است. صفحه را دوباره باز کنید.":"Недействительный или просроченный запрос. Обновите страницу.","صفحه پیدا نشد.":"Страница не найдена.","تلاش دوباره":"Повторить","ثبت تغییرات":"Сохранить изменения","ذخیره همه تغییرات":"Сохранить все изменения","تنظیمات عمومی":"Общие настройки","تنظیمات حساب":"Настройки аккаунта","تنظیمات از کد اصلی جدا ذخیره می‌شوند":"Настройки хранятся отдельно от основного кода","توضیح داشبورد":"Описание панели","عنوان مصرف":"Заголовок трафика","عنوان فعالیت":"Заголовок действий","عنوان کوچک زیر برند":"Подзаголовок бренда","نمایش بخش‌ها":"Отображаемые разделы","اندازه فونت":"Размер шрифта","شفافیت Glass":"Прозрачность Glass","گردی کارت":"Радиус карточек","شدت سایه کارت":"Интенсивность тени","رنگ":"Цвет","بازه":"Интервал","آزاد":"Свободно","استفاده‌شده":"Использовано"
});
function setLanguage(lang){
  if(!maps[lang]) lang="en";
  const root=document.documentElement, body=document.body;
  root.lang=lang; root.dir=lang==="fa"?"rtl":"ltr";
  body.classList.toggle("lang-en",lang==="en"); body.classList.toggle("lang-ru",lang==="ru"); body.classList.toggle("lang-fa",lang==="fa");
  document.querySelectorAll("[data-lang-choice]").forEach(x=>x.classList.toggle("active",x.dataset.langChoice===lang));
  const menu=document.getElementById("languageMenu"); if(menu) menu.hidden=true;
  const btn=document.getElementById("languageToggle"); if(btn) btn.title=lang==="fa"?"زبان":lang==="ru"?"Язык":"Language";
  document.querySelectorAll("[data-lang-label]").forEach(function(x){const k=x.dataset.langLabel; x.textContent=k==="fa"?"فارسی":k==="ru"?"Русский":"English";});
  const dict=maps[lang];
  document.querySelectorAll("body *:not(script):not(style)").forEach(el=>{
    if(el.children.length) return;
    if(el.matches("input,textarea,select,option")) return;
    const raw=(el.dataset.i18nOriginal||el.textContent).trim(); if(!raw) return; el.dataset.i18nOriginal=raw;
    if(Object.prototype.hasOwnProperty.call(dict,raw)) el.textContent=raw.replace(raw,dict[raw]);
    else {
      let out=raw;
      Object.keys(dict).sort((a,b)=>b.length-a.length).forEach(k=>{if(out.includes(k)&&k.length>2) out=out.split(k).join(dict[k]);});
      if(out!==raw) el.textContent=out;
    }
  });
  // Translate common dynamic time fragments generated after page load.
  if(lang!=="fa"){
    document.querySelectorAll(".updated").forEach(el=>{el.textContent=el.textContent.replace("به‌روزرسانی",lang==="ru"?"Обновлено":"Updated").replace("اکنون",lang==="ru"?"Сейчас":"Now").replace("دقیقه",lang==="ru"?"мин.":"min").replace("ساعت",lang==="ru"?"ч.":"hr").replace("روز",lang==="ru"?"дн.":"days");});
  }
  try{localStorage.setItem(LANG_KEY,lang)}catch(e){}
}
const lb=document.getElementById("languageToggle"), lm=document.getElementById("languageMenu");
let current="en"; try{current=localStorage.getItem(LANG_KEY)||"en"}catch(e){}
if(!maps[current]) current="en";
if(lb) lb.addEventListener("click",e=>{e.stopPropagation();if(lm)lm.hidden=!lm.hidden});
if(lm) lm.querySelectorAll("[data-lang-choice]").forEach(x=>x.addEventListener("click",()=>setLanguage(x.dataset.langChoice)));
document.addEventListener("click",e=>{if(lm&&!lm.hidden&&!lm.contains(e.target)&&e.target!==lb)lm.hidden=true});
setLanguage(current);
})();"""

def page(title, body, logged=True, notice="", kind="ok"):
    nav = "" if not logged else f'''<div class="drawer-backdrop" id="drawerBackdrop"></div><aside class="drawer" id="drawer" aria-hidden="true"><div class="drawer-head"><img class="drawer-logo" src="/static/logo.png" alt="idontPG-backup"><div><h3>{html.escape(str(idont_load_ui_settings().get("site_name", APP)))}</h3><p>{html.escape(str(idont_load_ui_settings().get("brand_subtitle","Backup Control Center · durwinam")))}</p></div><button class="drawer-close" id="drawerClose" type="button" aria-label="بستن منو">×</button></div><div class="drawer-section">منوی اصلی</div><nav class="drawer-nav"><a class="drawer-link" href="/">{ui_icon("dashboard", "drawer-icon")}<span><strong>داشبورد</strong><small>نمای کلی سیستم</small></span></a><a class="drawer-link" href="/telegram">{ui_icon("telegram", "drawer-icon")}<span><strong>بکاپ تلگرام</strong><small>تنظیمات و ارسال</small></span></a><a class="drawer-link" href="/backup-settings">{ui_icon("settings", "drawer-icon")}<span><strong>تنظیمات بکاپ</strong><small>Scheduler و Backup</small></span></a><a class="drawer-link" href="/test">{ui_icon("test", "drawer-icon")}<span><strong>تست تلگرام</strong><small>بررسی اتصال</small></span></a><a class="drawer-link" href="/account">{ui_icon("account", "drawer-icon")}<span><strong>حساب کاربری</strong><small>مدیریت ورود</small></span></a>{''.join(f'<a class="drawer-link" href="{html.escape(b.get("url",""), quote=True)}" target="_blank" rel="noopener noreferrer">{ui_icon("link", "drawer-icon")}<span><strong>{html.escape(b.get("icon","🔗"))} {html.escape(b.get("name","دکمه"))}</strong><small>لینک سفارشی</small></span></a>' for b in idont_load_ui_settings().get("buttons",[]) if _safe_button_url(b.get("url","")))}<a class="drawer-link logout" href="/logout">{ui_icon("rocket", "drawer-icon")}<span><strong>خروج</strong><small>پایان نشست</small></span></a></nav></aside>'''
    notice_html = f'<div class="notice {kind}">{html.escape(notice)}</div>' if notice else ""
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#06070d"><title>{html.escape(title)} · {html.escape(str(idont_load_ui_settings().get("site_name", APP)))}</title><style>{CSS}{ui_theme_css()}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{{outline:2px solid #67e8f9;outline-offset:3px}}

/* v5.5.0 UI polish: clearer controls, focus states and responsive spacing */
.btn, button, input[type="submit"] {{
  border-radius: 14px;
  min-height: 44px;
  font-weight: 800;
  letter-spacing: .1px;
  transition: transform .18s ease, box-shadow .18s ease, filter .18s ease;
}}
.btn:hover, button:hover, input[type="submit"]:hover {{
  transform: translateY(-1px);
  filter: brightness(1.06);
}}
.btn:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {{
  outline: 2px solid rgba(0, 220, 255, .75);
  outline-offset: 2px;
}}
.card, .glass, .panel, .box {{
  backdrop-filter: blur(18px) saturate(130%);
  -webkit-backdrop-filter: blur(18px) saturate(130%);
}}
@media (max-width: 720px) {{
  .btn, button, input[type="submit"] {{ width: 100%; }}
}}
.backup-row{{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line)}}.backup-row:last-child{{border-bottom:0}}.compact{{margin:0!important;gap:8px}}.compact form{{margin:0}}.btn.danger{{border-color:rgba(239,68,68,.35)}}.health-list .meta-row strong{{font-size:12px}}.disk-meter{{padding-top:6px}}.disk-bar{{height:10px;border-radius:999px;background:rgba(127,127,127,.16);overflow:hidden;margin:8px 0 14px}}.disk-bar span{{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--pink),var(--red));transition:width .4s ease}}.resource-monitor{{display:grid;gap:18px}}.resource-summary{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.resource-stat{{position:relative;display:flex;align-items:center;gap:12px;padding:15px 16px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(135deg,rgba(255,255,255,.055),rgba(255,255,255,.018));box-shadow:inset 0 1px 0 rgba(255,255,255,.05);overflow:hidden}}.resource-stat:after{{content:"";position:absolute;inset:auto -25px -45px auto;width:100px;height:100px;border-radius:50%;filter:blur(25px);opacity:.22}}.resource-stat.cpu:after{{background:#6ee7ff}}.resource-stat.ram:after{{background:#ff4fa3}}.resource-stat.disk:after{{background:#ff9b4a}}.rs-icon{{width:40px;height:40px;display:grid;place-items:center;border-radius:13px;background:rgba(255,255,255,.055);font-size:20px}}.resource-stat.cpu .rs-icon{{color:#6ee7ff}}.resource-stat.ram .rs-icon{{color:#ff4fa3}}.resource-stat.disk .rs-icon{{color:#ff9b4a}}.resource-stat small{{display:block;color:var(--muted);font-size:9px;letter-spacing:1.2px;font-weight:800}}.resource-stat strong{{display:block;font-family:"Trebuchet MS","Segoe UI",Tahoma,sans-serif;font-size:22px;margin-top:3px;letter-spacing:.2px}}.resource-stat i{{position:absolute;left:0;bottom:0;width:42%;height:2px}}.resource-stat.cpu i{{background:linear-gradient(90deg,#6ee7ff,transparent)}}.resource-stat.ram i{{background:linear-gradient(90deg,#ff4fa3,transparent)}}.resource-stat.disk i{{background:linear-gradient(90deg,#ff9b4a,transparent)}}.resource-plot{{border:1px solid var(--line);border-radius:22px;background:linear-gradient(145deg,rgba(10,13,28,.66),rgba(41,14,48,.38));padding:18px 18px 12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 20px 60px rgba(0,0,0,.12);overflow:hidden}}.light .resource-plot{{background:linear-gradient(145deg,rgba(255,255,255,.55),rgba(255,224,242,.42))}}.plot-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}}.plot-kicker{{display:block;font-size:9px;letter-spacing:1.8px;color:#a78bfa;font-weight:900;margin-bottom:3px}}.plot-head b{{font-size:15px}}.live-dot{{display:flex;align-items:center;gap:7px;font-size:10px;letter-spacing:1px;font-weight:900;color:#6ee7b7}}.live-dot i{{width:7px;height:7px;border-radius:50%;background:#6ee7b7;box-shadow:0 0 12px #6ee7b7;animation:livePulse 1.5s ease-in-out infinite}}@keyframes livePulse{{50%{{opacity:.35;transform:scale(.72)}}}}.plot-body{{position:relative;padding-right:38px}}.y-axis{{position:absolute;right:0;top:0;bottom:25px;width:34px;display:flex;flex-direction:column;justify-content:space-between;align-items:flex-start;color:var(--muted);font-size:9px}}.telemetry{{display:block;width:100%;height:270px;overflow:visible}}.grid-lines line{{stroke:currentColor;stroke-opacity:.08;stroke-width:1}}.area{{stroke:none}}.cpu-area{{fill:url(#fillCpu)}}.ram-area{{fill:url(#fillRam)}}.disk-area{{fill:url(#fillDisk)}}.line{{fill:none;stroke-width:2.7;stroke-linecap:round;stroke-linejoin:round;filter:url(#glowC)}}.cpu-line{{stroke:#6ee7ff}}.ram-line{{stroke:#ff4fa3;filter:url(#glowP)}}.disk-line{{stroke:#ff9b4a}}.dot{{stroke:rgba(255,255,255,.9);stroke-width:2}}.cpu-dot{{fill:#6ee7ff}}.ram-dot{{fill:#ff4fa3}}.disk-dot{{fill:#ff9b4a}}.chart-legend{{display:flex;align-items:center;justify-content:flex-start;gap:18px;margin-top:2px;padding:0 4px;flex-wrap:wrap}}.legend{{display:inline-flex;align-items:center;gap:6px;font-size:10px;color:var(--muted);font-weight:800}}.legend i{{width:8px;height:8px;border-radius:50%;box-shadow:0 0 10px currentColor}}.legend.cpu{{color:#6ee7ff}}.legend.ram{{color:#ff4fa3}}.legend.disk{{color:#ff9b4a}}.updated{{margin-right:auto;font-size:9px;color:var(--muted)}}.mini-chart{{height:190px;display:flex;align-items:end;gap:8px;padding:10px 2px 4px}}.chart-col{{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:end;gap:6px}}.chart-value{{font-size:9px;color:var(--muted);white-space:nowrap;overflow:hidden;max-width:100%;text-overflow:ellipsis}}.chart-bar{{height:115px;width:min(28px,70%);display:flex;align-items:end;border-radius:10px 10px 4px 4px;background:rgba(139,92,246,.10);overflow:hidden}}.chart-bar span{{display:block;width:100%;border-radius:inherit;background:linear-gradient(180deg,var(--accent2),var(--accent),var(--pink));min-height:3px}}.chart-col small{{font-size:9px;color:var(--muted)}}@media(max-width:720px){{.backup-row{{align-items:stretch;flex-direction:column}}.backup-row .actions{{width:100%}}.mini-chart{{height:170px}}.chart-value{{font-size:8px}}.resource-summary{{grid-template-columns:1fr}}.resource-stat{{padding:12px 14px}}.resource-stat strong{{font-size:20px}}.resource-plot{{padding:15px 12px 10px;border-radius:18px}}.telemetry{{height:210px}}.plot-body{{padding-right:32px}}.chart-legend{{gap:12px}}.updated{{width:100%;margin-right:0}}}}</style></head><body class="lang-en"><div class="aurora"><i class="orb o1"></i><i class="orb o2"></i><i class="orb o3"></i></div><main class="container"><header class="topbar"><div class="brand"><img class="brand-logo" src="/static/logo.png" alt="IDONTPG Backup"><div><h1>{APP}</h1><p>{html.escape(str(idont_load_ui_settings().get("brand_subtitle","Backup Control Center · durwinam")))}</p></div></div><div class="top-actions"><button class="menu-toggle" id="menuToggle" type="button" aria-label="باز کردن منو" title="منو"><span class="hamb">☰</span></button>{ui_language_menu()}<div class="theme-picker"><button class="theme-toggle" id="themeToggle" type="button" aria-label="انتخاب تم" title="انتخاب تم">☀️</button><div class="theme-menu" id="themeMenu" hidden><button type="button" data-theme-choice="custom">🎨 <span>کاستوم</span></button><button type="button" data-theme-choice="light">☀️ <span>لایت</span></button><button type="button" data-theme-choice="dark">🌙 <span>دارک</span></button></div></div><div class="pill">v{VERSION} · Secure Glass UI</div></div></header>{nav}{notice_html}{body}<div class="footer">idontPG-backup · {VERSION} · durwinam</div></main><script>{ui_language_script()}(function(){{const key="idontpg-theme";const root=document.body;const btn=document.getElementById("themeToggle");const themeMenu=document.getElementById("themeMenu");const choices=themeMenu?themeMenu.querySelectorAll("[data-theme-choice]"):[];function apply(t){{root.classList.remove("light","custom");if(t==="light")root.classList.add("light");else if(t==="custom")root.classList.add("custom");if(btn){{btn.textContent=t==="light"?"☀️":t==="custom"?"🎨":"🌙";btn.title="انتخاب تم"}}choices.forEach(function(x){{x.classList.toggle("active",x.dataset.themeChoice===t)}});const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.setAttribute("content",t==="light"?"#fff1f8":t==="custom"?"var(--bg)":"#06070d")}}let t="dark";try{{t=localStorage.getItem(key)||"dark"}}catch(e){{}}if(t!=="light"&&t!=="custom"&&t!=="dark")t="dark";apply(t);if(btn)btn.addEventListener("click",function(e){{e.stopPropagation();if(themeMenu)themeMenu.hidden=!themeMenu.hidden}});choices.forEach(function(x){{x.addEventListener("click",function(){{t=x.dataset.themeChoice;apply(t);try{{localStorage.setItem(key,t)}}catch(e){{}}if(themeMenu)themeMenu.hidden=true}})}});document.addEventListener("click",function(e){{if(themeMenu&&!themeMenu.hidden&&!themeMenu.contains(e.target)&&e.target!==btn)themeMenu.hidden=true}});const menu=document.getElementById("menuToggle");const drawer=document.getElementById("drawer");const backdrop=document.getElementById("drawerBackdrop");const close=document.getElementById("drawerClose");function setMenu(open){{if(!drawer)return;drawer.classList.toggle("open",open);if(backdrop)backdrop.classList.toggle("open",open);if(menu)menu.classList.toggle("open",open);drawer.setAttribute("aria-hidden",open?"false":"true");document.body.style.overflow=open?"hidden":""}}if(menu)menu.addEventListener("click",function(){{setMenu(!drawer.classList.contains("open"))}});if(backdrop)backdrop.addEventListener("click",function(){{setMenu(false)}});if(close)close.addEventListener("click",function(){{setMenu(false)}});document.addEventListener("keydown",function(e){{if(e.key==="Escape")setMenu(false)}});if(drawer)drawer.querySelectorAll("a").forEach(function(a){{a.addEventListener("click",function(){{setMenu(false)}})}});const cd=document.getElementById("backupCountdown");if(cd){{let sec=parseInt(cd.textContent,10);if(Number.isFinite(sec)&&sec>=0){{const fmt=function(s){{s=Math.max(0,s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;return (d?d+" روز ":"")+String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(x).padStart(2,"0")}};cd.textContent=fmt(sec);setInterval(function(){{if(sec>0)sec--;cd.textContent=fmt(sec)}},1000)}}}}}})();</script></body></html>'''


def hidden_csrf(sid):
    return f'<input type="hidden" name="csrf" value="{html.escape(csrf_token(sid))}">'



# ===== IDONT ADMIN CUSTOMIZATION PANEL =====
IDONT_ADMIN_PATH = "/control-7Kq9M2xP4/"
IDONT_SETTINGS_DIR = os.environ.get("IDONTPG_SETTINGS_DIR", "/var/lib/idontpg/config")
IDONT_SETTINGS_FILE = os.path.join(IDONT_SETTINGS_DIR, "ui_settings.json")

IDONT_DEFAULT_SETTINGS = {
    "site_name":"IDONT",
    "brand_subtitle":"Backup Control Center · durwinam",
    "background":{"value":""},
    "logo":{"url":"","width":54},
    "theme":{"primary":"#8b5cf6","secondary":"#22d3ee","glow":0.72,"font_size":15,"glass_opacity":0.58,"blur":18,"radius":20,"animation_speed":1.0,"preset":"ultra-dark"},
    "emojis":{
        "active":{"icon":"●","color":"#22c55e","glow":0.72,"speed":1.35},
        "inactive":{"icon":"●","color":"#ef4444","glow":0.72,"speed":1.35},
        "warning":{"icon":"●","color":"#f59e0b","glow":0.72,"speed":1.35},
        "info":{"icon":"●","color":"#38bdf8","glow":0.72,"speed":1.35}
    },
    "texts":{"dashboard_title":"داشبورد","backup_title":"اطلاعات بکاپ","traffic_title":"حجم مصرفی","activity_title":"فعالیت‌های اخیر"},
    "sections":{"dashboard":True,"backup":True,"traffic":True,"activity":True,"server_stats":True},
    "activity_limit":3,
    "buttons":[]
}

def idont_load_ui_settings():
    try:
        os.makedirs(IDONT_SETTINGS_DIR, mode=0o700, exist_ok=True)
        if not os.path.exists(IDONT_SETTINGS_FILE):
            idont_save_ui_settings(IDONT_DEFAULT_SETTINGS)
            return dict(IDONT_DEFAULT_SETTINGS)
        with open(IDONT_SETTINGS_FILE,encoding="utf-8") as f: cur=json.load(f)
        def merge(a,b):
            out=dict(a)
            for k,v in b.items():
                out[k]=merge(out[k],v) if isinstance(v,dict) and isinstance(out.get(k),dict) else v
            return out
        return merge(IDONT_DEFAULT_SETTINGS,cur)
    except Exception:
        return dict(IDONT_DEFAULT_SETTINGS)

def idont_save_ui_settings(s):
    os.makedirs(IDONT_SETTINGS_DIR, mode=0o700, exist_ok=True)
    tmp=IDONT_SETTINGS_FILE+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(s,f,ensure_ascii=False,indent=2)
    os.replace(tmp,IDONT_SETTINGS_FILE)
    try: os.chmod(IDONT_SETTINGS_FILE,0o600)
    except Exception: pass

def idont_admin_html():
    s=idont_load_ui_settings(); e=html.escape
    return """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>مدیریت IDONT</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0%,rgba(139,92,246,.18),transparent 32%),#080b14;color:#eef2ff;font-family:system-ui,sans-serif}
.wrap{max-width:1050px;margin:auto;padding:28px 18px 60px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.card{margin:12px 0;padding:18px;border:1px solid rgba(255,255,255,.1);border-radius:20px;background:rgba(255,255,255,.07);backdrop-filter:blur(18px)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}.row{display:grid;grid-template-columns:90px 1fr 1fr 75px 75px;gap:7px;align-items:center;margin:8px 0}
input{width:100%;padding:10px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.18);color:inherit}
label{display:block;font-size:12px;opacity:.75;margin:9px 0 5px}.btn{display:inline-block;padding:10px 14px;border:0;border-radius:12px;background:linear-gradient(135deg,#8b5cf6,#22d3ee);color:#fff;text-decoration:none;font-weight:700;cursor:pointer}
@media(max-width:650px){.row{grid-template-columns:1fr 1fr}.row b{grid-column:1/-1}}
</style></head><body><main class="wrap">
<div class="top"><div><h1>⚙️ مدیریت IDONT</h1><small>تنظیمات از کد اصلی جدا ذخیره می‌شوند</small></div><a class="btn" href="/">بازگشت</a></div>
<form method="post">
<div class="grid">
<div class="card"><h2>🎛️ داشبورد</h2>
<label>نام پنل</label><input name="site_name" value="%s">
<label>تعداد فعالیت‌های اخیر</label><input name="activity_limit" type="number" min="1" max="20" value="%s">
<label>اندازه فونت</label><input name="font_size" type="number" min="11" max="22" value="%s">
<label>نمایش بخش‌ها</label>
<label><input type="checkbox" name="sec_dashboard" %s> داشبورد</label>
<label><input type="checkbox" name="sec_backup" %s> بکاپ</label>
<label><input type="checkbox" name="sec_traffic" %s> حجم مصرفی</label>
<label><input type="checkbox" name="sec_activity" %s> فعالیت‌های اخیر</label>
<label><input type="checkbox" name="sec_server_stats" %s> آمار سرور</label>
</div>
<div class="card"><h2>🎨 ظاهر</h2>
<label>رنگ اصلی</label><input name="primary" value="%s">
<label>رنگ دوم</label><input name="secondary" value="%s">
<label>Background</label><input name="background" value="%s">
<label>Logo URL</label><input name="logo_url" value="%s">
<label>عرض Logo</label><input name="logo_width" type="number" min="24" max="240" value="%s">
<label>شدت Glow</label><input name="global_glow" type="number" min="0" max="1" step=".01" value="%s">
</div></div>
<div class="card"><h2>😀 Emoji / Status</h2>
<div class="row"><b>وضعیت</b><b>آیکون</b><b>رنگ</b><b>Glow</b><b>سرعت</b></div>
%s</div>
<div class="card"><h2>📝 متن‌ها</h2>
<label>عنوان داشبورد</label><input name="txt_dashboard_title" value="%s">
<label>عنوان بکاپ</label><input name="txt_backup_title" value="%s">
<label>عنوان حجم مصرفی</label><input name="txt_traffic_title" value="%s">
<label>عنوان فعالیت‌های اخیر</label><input name="txt_activity_title" value="%s">
</div>
<div class="card"><h2>➕ افزودن دکمه</h2>
<label>نام</label><input name="new_button_name">
<label>آیکون</label><input name="new_button_icon" value="🔗" maxlength="8">
<label>لینک</label><input name="new_button_url" placeholder="https://...">
</div>
<div class="card"><h2>🔘 دکمه‌های فعلی</h2>%s</div>
</form><button class="btn" onclick="document.querySelector('form').submit()">💾 ذخیره تغییرات</button>
</main></body></html>""" % (
e(s["site_name"]),s["activity_limit"],s["theme"]["font_size"],
"checked" if s["sections"]["dashboard"] else "","checked" if s["sections"]["backup"] else "",
"checked" if s["sections"]["traffic"] else "","checked" if s["sections"]["activity"] else "",
"checked" if s["sections"]["server_stats"] else "",e(s["theme"]["primary"]),e(s["theme"]["secondary"]),
e(s["background"]["value"]),e(s["logo"]["url"]),s["logo"]["width"],s["theme"]["glow"],
"".join('<div class="row"><b>%s</b><input name="emoji_%s" value="%s"><input name="color_%s" value="%s"><input name="glow_%s" type="number" step=".01" min="0" max="1" value="%s"><input name="speed_%s" type="number" step=".01" min=".4" max="4" value="%s"></div>' %
(label,key,e(s["emojis"][key]["icon"]),key,e(s["emojis"][key]["color"]),key,s["emojis"][key]["glow"],key,s["emojis"][key]["speed"])
for key,label in [("active","فعال"),("inactive","غیرفعال"),("warning","هشدار"),("info","اطلاعات")]),
e(s["texts"]["dashboard_title"]),e(s["texts"]["backup_title"]),e(s["texts"]["traffic_title"]),e(s["texts"]["activity_title"]),
"".join('<div class="row"><input name="btn_name_%d" value="%s"><input name="btn_icon_%d" value="%s"><input name="btn_url_%d" value="%s"></div>' %
(i,e(b.get("name","")),i,e(b.get("icon","🔗")),i,e(b.get("url",""))) for i,b in enumerate(s["buttons"]))
)

def _ui_float(value, default, lo, hi):
    try:
        value = str(value or "").strip().replace(",", ".")
        value = value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        x = float(value)
        if x != x:
            return default
        return max(lo, min(hi, x))
    except Exception:
        return default

def _ui_int(value, default, lo, hi):
    try:
        value = str(value or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        return max(lo, min(hi, int(float(value))))
    except Exception:
        return default

def _safe_button_url(value):
    value = str(value or "").strip()
    try:
        u = urllib.parse.urlparse(value)
        if u.scheme.lower() in {"http", "https"} and u.netloc:
            return value[:500]
    except Exception:
        pass
    return ""

def idont_apply_ui_settings(form):
    s=idont_load_ui_settings()
    get=lambda k,d="": form.get(k,d)
    s["site_name"]=str(get("site_name",s["site_name"])).strip()[:80] or s["site_name"]
    s["activity_limit"]=_ui_int(get("activity_limit",s.get("activity_limit",3)),3,1,20)
    t=s.setdefault("theme",{})
    t["font_size"]=_ui_int(get("font_size",t.get("font_size",15)),15,11,22)
    t["primary"]=str(get("primary",t.get("primary","#8b5cf6"))).strip()[:24] or "#8b5cf6"
    t["secondary"]=str(get("secondary",t.get("secondary","#22d3ee"))).strip()[:24] or "#22d3ee"
    t["glass_opacity"]=_ui_float(get("glass_opacity",t.get("glass_opacity",.58)),.58,.25,.95)
    t["blur"]=_ui_int(get("blur",t.get("blur",18)),18,0,40)
    t["radius"]=_ui_int(get("radius",t.get("radius",20)),20,8,36)
    t["glow"]=_ui_float(get("glow",t.get("glow",.72)),.72,0,1)
    t["animation_speed"]=_ui_float(get("animation_speed",t.get("animation_speed",1)),1,.2,3)
    preset=str(form.get("theme_preset", "")).strip()
    presets={
        "ultra-dark":("#050507","#7c3aed","#22d3ee"),
        "aurora":("#07111b","#8b5cf6","#22d3ee"),
        "midnight":("#0b0613","#a855f7","#ec4899"),
        "cyber":("#03090a","#22c55e","#06b6d4"),
        "ocean":("#041018","#0ea5e9","#2dd4bf"),
        "ruby":("#100608","#f43f5e","#fb923c"),
    }
    if preset in presets:
        bg,a1,a2=presets[preset]
        t.update({"preset":preset,"primary":a1,"secondary":a2})
        s.setdefault("background",{})["value"]=bg
    else:
        t["preset"]=t.get("preset", "ultra-dark")
    s.setdefault("background",{})["value"]=str(get("background",s["background"].get("value",""))).strip()[:600]
    s.setdefault("logo",{})["url"]=str(get("logo_url",s["logo"].get("url",""))).strip()[:600]
    s["logo"]["width"]=_ui_int(get("logo_width",s["logo"].get("width",54)),54,24,240)
    for k in s["sections"]:
        s["sections"][k]=bool(form.get("sec_"+k))
    for k in s["emojis"]:
        em=s["emojis"][k]
        em["icon"]=str(get("emoji_"+k,em["icon"]))[:8]
        em["color"]=str(get("emoji_color_"+k,em["color"])).strip()[:24] or em["color"]
        em["glow"]=_ui_float(get("emoji_glow_"+k,em.get("glow",.72)),em.get("glow",.72),0,1)
        em["speed"]=_ui_float(get("emoji_speed_"+k,em.get("speed",1.35)),em.get("speed",1.35),.3,4)
    for k,n in [("hero_title","hero_title"),("hero_subtitle","hero_subtitle"),("dashboard_title","dashboard_title"),("backup_title","backup_title"),("traffic_title","traffic_title"),("activity_title","activity_title")]:
        if k in s.get("texts",{}):
            s["texts"][k]=str(get(n,s["texts"][k]))[:300 if k in {"hero_subtitle"} else 120]
    name=str(get("button_name",get("new_button_name",""))).strip()
    url=_safe_button_url(get("button_url",get("new_button_url","")))
    icon=str(get("button_icon",get("new_button_icon","🔗")))[:8]
    if name and url:
        s.setdefault("buttons",[]).append({"name":name[:60],"icon":icon,"url":url})
    s["buttons"]=s.get("buttons",[])[:20]
    for i,b in enumerate(s["buttons"]):
        b["name"]=str(get("btn_name_"+str(i),b.get("name","")))[:60]
        b["icon"]=str(get("btn_icon_"+str(i),b.get("icon","🔗")))[:8]
        candidate=_safe_button_url(get("btn_url_"+str(i),b.get("url","")))
        if candidate:
            b["url"]=candidate
    idont_save_ui_settings(s)
    return s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def sid(self):
        cleanup_sessions()
        sid = self.cookies().get("idontpg_session", "")
        return sid if sid in SESSIONS else None

    def logged(self):
        return self.sid() is not None

    def admin_logged(self):
        sid = self.sid()
        return bool(sid and SESSIONS.get(sid, {}).get("role") == "admin" and time.time() - SESSIONS[sid].get("created", 0) <= ADMIN_SESSION_TTL)

    def admin_login_page(self, error=""):
        notice = '<div class="notice bad">' + html.escape(error) + '</div>' if error else ''
        body = ('<section class="login"><div class="glass login-card"><div class="login-icon-wrap">' + ui_icon("lock", "card-icon login-icon") + '</div><h2>مدیریت خصوصی</h2><p class="sub">این بخش فقط برای مدیر اصلی است.</p>' + notice + '<form method="post" action="' + ADMIN_PATH + '"><div class="field"><label>نام کاربری ادمین</label><input name="admin_username" required></div><div class="field"><label>رمز ادمین</label><input type="password" name="admin_password" minlength="8" required></div><button class="btn primary full">ورود به مدیریت</button></form></div></section>')
        return page("Admin Login", body, False)

    def redirect(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def send_pasarguard_logo(self):
        candidates = [
            SCRIPT.parent / "web" / "static" / "pasarguard-logo.png",
            Path("/usr/local/share/idontPG-backup/pasarguard-logo.png"),
        ]
        for path in candidates:
            if path.is_file():
                try:
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except OSError:
                    pass
        self.send_response(404)
        self.end_headers()

    def send_logo(self):
        for path in LOGO_CANDIDATES:
            if path.is_file():
                try:
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return True
                except OSError:
                    pass
        self.send_response(404)
        self.end_headers()
        return False

    def send_html(self, content, status=200):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def form(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def require_csrf(self, data):
        sid = self.sid()
        return bool(sid and hmac.compare_digest(data.get("csrf", ""), csrf_token(sid)))

    def login_page(self, error=""):
        notice = f'<div class="notice bad">{html.escape(error)}</div>' if error else ''
        username = html.escape(canonical_username(load_cfg().get("username", "admin")))
        body = f'''<section class="login"><div class="glass login-card"><div class="login-icon-wrap">{ui_icon("lock", "card-icon login-icon")}</div><h2 style="font-size:28px;margin:16px 0 8px">ورود به پنل</h2><p class="sub" style="font-size:13px;line-height:1.8">برای ورود، نام کاربری و رمز عبور ادمین را وارد کنید.</p>{notice}<form method="post" action="/login"><div class="field"><label>نام کاربری</label><input type="text" name="username" value="{username}" autocomplete="username" required></div><div class="field"><label>رمز عبور</label><input type="password" name="password" minlength="8" autocomplete="current-password" required></div><button class="btn primary full" type="submit">ورود امن ←</button></form></div></section>'''
        return page("Login", body, False)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        c = load_cfg()
        if path == "/static/logo.png":
            self.send_logo(); return
        if path == "/static/pasarguard-logo.png":
            self.send_pasarguard_logo(); return
        if path == "/logout":
            sid = self.sid()
            was_admin = bool(sid and SESSIONS.get(sid, {}).get("role") == "admin")
            if sid: SESSIONS.pop(sid, None)
            self.send_response(302)
            self.send_header("Set-Cookie", "idontpg_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", ADMIN_PATH if was_admin else "/login")
            self.end_headers()
            return
        if not c.get("password_hash"):
            body = '''<section class="login"><div class="glass"><div class="login-icon-wrap">{ui_icon("lock", "card-icon login-icon")}</div><h2 style="font-size:28px;margin:16px 0 8px">راه‌اندازی اولیه</h2><p class="sub" style="font-size:13px;line-height:1.8">برای محافظت از پنل، نام کاربری ۵ تا ۳۲ کاراکتر و رمز حداقل ۸ کاراکتر، شامل حداقل ۲ حرف، ۱ عدد و یکی از # @ * بسازید.</p><form method="post" action="/setup"><div class="field"><label>نام کاربری</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" autocomplete="username" placeholder="admin" required></div><div class="field"><label>رمز ادمین</label><input type="password" name="password" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required></div><div class="field"><label>تکرار رمز</label><input type="password" name="password_confirm" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required></div><button class="btn primary full">ساخت حساب و ورود</button></form></div></section>'''
            self.send_html(page("First Run", body, False)); return
        if path == "/login":
            self.send_html(self.login_page()); return
        if path == ADMIN_PATH:
            if not self.admin_logged():
                self.send_html(self.admin_login_page()); return
            ui = idont_load_ui_settings(); e = html.escape
            presets = [("ultra-dark","Ultra Dark","#050507","#7c3aed","#22d3ee"),("aurora","Aurora Glass","#07111b","#8b5cf6","#22d3ee"),("midnight","Midnight Purple","#0b0613","#a855f7","#ec4899"),("cyber","Cyber Neon","#03090a","#22c55e","#06b6d4"),("ocean","Ocean Glass","#041018","#0ea5e9","#2dd4bf"),("ruby","Ruby Night","#100608","#f43f5e","#fb923c")]
            cards=[]
            for k,label,bg,a1,a2 in presets:
                active=' active' if ui.get('theme',{}).get('preset')==k else ''
                cards.append(f'<button class="theme-card{active}" type="submit" name="theme_preset" value="{k}" style="--preview-bg:{bg};--preview-a:{a1};--preview-b:{a2}"><span class="theme-preview"></span><b>{label}</b></button>')
            checked=lambda k:' checked' if ui.get('sections',{}).get(k,True) else ''
            admin_css = '<style>.admin-tabs{display:flex;gap:8px;margin:0 0 16px}.admin-tab{padding:10px 14px;border-radius:13px;border:1px solid var(--line);color:var(--text);text-decoration:none;background:var(--glass2)}.admin-tab.active{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent}.theme-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.theme-card{min-height:115px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.04);color:var(--text);padding:10px;text-align:right;cursor:pointer}.theme-card.active{outline:2px solid var(--accent2);box-shadow:0 0 28px rgba(34,211,238,.18)}.theme-preview{height:58px;border-radius:13px;display:block;margin-bottom:8px;background:radial-gradient(circle at 25% 25%,var(--preview-a),transparent 45%),radial-gradient(circle at 75% 65%,var(--preview-b),transparent 45%),var(--preview-bg)}.admin-section{margin-bottom:16px}.emoji-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.emoji-editor{display:grid;grid-template-columns:90px 1fr 1fr;gap:8px;padding:12px;border:1px solid var(--line);border-radius:15px}@media(max-width:700px){.theme-grid,.emoji-grid{grid-template-columns:1fr}.emoji-editor{grid-template-columns:1fr 1fr}.emoji-editor b{grid-column:1/-1}}</style>'
            body=f'''<section class="hero"><h2>{ui_icon("settings","hero-icon")} <span class="gradient">مرکز مدیریت</span></h2><p>شخصی‌سازی کامل ظاهر، داشبورد و اجزای وب‌پنل.</p></section>
{admin_css}
<div class="admin-tabs"><a class="admin-tab active" href="{ADMIN_PATH}">🎨 شخصی‌سازی</a><a class="admin-tab" href="/">← داشبورد</a></div>
<form method="post" action="{ADMIN_PATH}">{hidden_csrf(self.sid())}<div class="glass wide admin-section"><div class="card-head"><div><h3 class="title">🎨 شش تم آماده</h3><p class="sub">کاربر عادی هیچ گزینه‌ای برای انتخاب تم نمی‌بیند.</p></div></div><div class="theme-grid">{''.join(cards)}</div></div>
<div class="grid"><article class="glass card"><h3 class="title">🧩 ظاهر</h3><div class="field"><label>رنگ اصلی</label><input name="primary" value="{e(ui['theme']['primary'])}"></div><div class="field"><label>رنگ دوم</label><input name="secondary" value="{e(ui['theme']['secondary'])}"></div><div class="field"><label>پس‌زمینه</label><input name="background" value="{e(ui['background'].get('value',''))}"></div><div class="field"><label>شفافیت Glass</label><input name="glass_opacity" type="text" inputmode="decimal" value="{ui['theme'].get('glass_opacity',.58)}"></div><div class="field"><label>Blur</label><input name="blur" type="text" inputmode="numeric" value="{ui['theme'].get('blur',18)}"></div><div class="field"><label>گردی کارت</label><input name="radius" type="text" inputmode="numeric" value="{ui['theme'].get('radius',20)}"></div></article>
<article class="glass card"><h3 class="title">✨ Glow و حرکت</h3><div class="field"><label>شدت Glow</label><input name="glow" type="text" inputmode="decimal" value="{ui['theme'].get('glow',.72)}"></div><div class="field"><label>سرعت انیمیشن</label><input name="animation_speed" type="text" inputmode="decimal" value="{ui['theme'].get('animation_speed',1)}"></div><div class="field"><label>اندازه فونت</label><input name="font_size" type="text" inputmode="numeric" value="{ui['theme'].get('font_size',15)}"></div><div class="field"><label>Logo URL</label><input name="logo_url" value="{e(ui['logo'].get('url',''))}"></div><div class="field"><label>عرض Logo</label><input name="logo_width" type="text" inputmode="numeric" value="{ui['logo'].get('width',54)}"></div></article>
<article class="glass card"><h3 class="title">📝 متن و برند</h3><div class="field"><label>نام پنل</label><input name="site_name" value="{e(ui['site_name'])}"></div><div class="field"><label>عنوان داشبورد</label><input name="hero_title" value="{e(ui['texts'].get('hero_title','کنترل کامل Backup'))}"></div><div class="field"><label>توضیح داشبورد</label><input name="hero_subtitle" value="{e(ui['texts'].get('hero_subtitle',''))}"></div><div class="field"><label>عنوان Backup</label><input name="backup_title" value="{e(ui['texts']['backup_title'])}"></div><div class="field"><label>عنوان مصرف</label><input name="traffic_title" value="{e(ui['texts']['traffic_title'])}"></div><div class="field"><label>عنوان فعالیت</label><input name="activity_title" value="{e(ui['texts']['activity_title'])}"></div></article>
<article class="glass card"><h3 class="title">🖥️ چیدمان و تجربه کاربر</h3><div class="field"><label>عرض محتوای داشبورد</label><input name="content_width" type="text" inputmode="numeric" value="{ui["theme"].get("content_width",1180)}"></div><div class="field"><label>فاصله بین کارت‌ها</label><input name="card_gap" type="text" inputmode="numeric" value="{ui["theme"].get("card_gap",16)}"></div><div class="field"><label>شدت سایه کارت</label><input name="shadow_strength" type="text" inputmode="decimal" value="{ui["theme"].get("shadow_strength",0.5)}"></div><div class="field"><label>عنوان کوچک زیر برند</label><input name="brand_subtitle" value="{e(ui.get("brand_subtitle","Backup Control Center · durwinam"))}"></div></article>
<article class="glass card"><h3 class="title">👁️ بخش‌های داشبورد</h3><label class="toggle"><span>Backup</span><input type="checkbox" name="sec_backup"{checked('backup')}></label><label class="toggle"><span>حجم مصرفی</span><input type="checkbox" name="sec_traffic"{checked('traffic')}></label><label class="toggle"><span>فعالیت‌ها</span><input type="checkbox" name="sec_activity"{checked('activity')}></label><label class="toggle"><span>آمار سرور</span><input type="checkbox" name="sec_server_stats"{checked('server_stats')}></label><div class="field"><label>تعداد فعالیت‌ها</label><input name="activity_limit" type="text" inputmode="numeric" value="{ui.get('activity_limit',3)}"></div></article></div>
<div class="glass wide admin-section"><h3 class="title">😀 Emoji Status</h3><div class="emoji-grid">'''
            for k,label in [("active","فعال"),("inactive","غیرفعال"),("warning","هشدار"),("info","اطلاعات")]:
                em=ui['emojis'][k]
                body+=f'<div class="emoji-editor"><b>{label}</b><input name="emoji_{k}" value="{e(em["icon"])}"><input name="emoji_color_{k}" value="{e(em["color"])}"><input name="emoji_glow_{k}" type="text" inputmode="decimal" value="{em.get("glow",.72)}"><input name="emoji_speed_{k}" type="text" inputmode="decimal" value="{em.get("speed",1.35)}"></div>'
            body+=f'''</div></div><div class="glass wide admin-section"><h3 class="title">➕ دکمه سفارشی</h3><div class="field"><label>نام</label><input name="button_name"></div><div class="field"><label>آیکون</label><input name="button_icon" value="🔗"></div><div class="field"><label>لینک</label><input name="button_url" placeholder="https://..."></div><div class="actions"><button class="btn primary" type="submit" name="save_settings" value="1">💾 ذخیره همه تغییرات</button></div></div></form>'''
            self.send_html(page("Admin", body)); return

        if not self.logged():
            self.redirect("/login"); return

        if path == "/api/resources":
            self.send_json(get_server_resource_usage()); return

        status = scheduler_status()
        if path == "/":
            ui_now = idont_load_ui_settings()
            token = c.get("token") or "تنظیم نشده"
            masked = (token[:8] + "••••••") if len(token) > 8 else token
            status_class = "on" if status == "active" else "off"
            panel_info = get_panel_info()
            backup_info = get_backup_info()
            backup_used = backup_info["size"]
            panel_used = get_panel_storage_usage()
            panel_status_class = "on" if panel_info["status"] == "Online" else "off"
            panel_url = html.escape(panel_info["url"])
            body = f'''<section class="hero"><h2>{html.escape(ui_now.get("texts",{}).get("hero_title","کنترل کامل Backup"))}</h2><p>{html.escape(ui_now.get("texts",{}).get("hero_subtitle","همه‌چیز برای مدیریت Backup، ارسال به Telegram و زمان‌بندی خودکار، داخل یک پنل شیشه‌ای و سریع."))}</p></section>
<div class="grid">
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("backup", "card-icon")}<div><h3 class="title">{html.escape(ui_now.get("texts",{}).get("backup_title","اطلاعات Backup"))}</h3><p class="sub">Backup Information</p></div></div></div>
<div class="meta"><div class="meta-row"><span>{ui_icon("backup", "meta-icon")} تعداد Backup</span><strong>{backup_info["count"]}</strong></div><div class="meta-row"><span>{ui_icon("disk", "meta-icon")} حجم کل Backupها</span><strong>{html.escape(backup_info["size"])}</strong></div><div class="meta-row"><span>{ui_icon("clock", "meta-icon")} آخرین Backup</span><strong title="{html.escape(backup_info["latest"])}">{html.escape(backup_info["latest_time"])}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px;min-width:0"><span class="panel-brand-icon card-icon" aria-label="PasarGuard"><img src="/static/pasarguard-logo.png" alt="PasarGuard" onerror="this.style.display='none';this.parentElement.classList.add('logo-fallback')"><span class="logo-fallback-text">PG</span></span><div style="min-width:0"><h3 class="title">اطلاعات پنل</h3><p class="sub">Panel Information</p></div></div><span class="status-wrap">{status_badge('آنلاین' if panel_info['status'] == 'Online' else 'آفلاین', 'ok' if panel_info['status'] == 'Online' else 'bad')}</span></div>
<div class="meta"><div class="meta-row"><span class="meta-label">{ui_icon("link", "meta-icon")} لینک پنل</span><a href="{panel_url}" target="_blank" rel="noopener noreferrer" style="max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{panel_url}</a></div><div class="meta-row has-status"><span class="meta-label">{status_dot("ok")} وضعیت پنل</span><strong>{html.escape(panel_info['status'])}</strong></div><div class="meta-row"><span class="meta-label">{ui_icon("traffic", "meta-icon")} مصرف واقعی Node</span><strong title="دریافت مستقیم از PasarGuard Node">{html.escape(panel_used)}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("telegram", "card-icon")}<div><h3 class="title">بکاپ تلگرام</h3><p class="sub">Telegram Backup</p></div></div>{status_badge('فعال' if status == 'active' else 'متوقف', 'ok' if status == 'active' else 'bad')}</div>
<div class="meta"><div class="meta-row"><span>Bot Token</span><span>{html.escape(masked)}</span></div><div class="meta-row"><span>Chat ID</span><span>{html.escape(c.get('chat') or 'تنظیم نشده')}</span></div><div class="meta-row"><span>Topic ID</span><span>{html.escape(c.get('topic') or '—')}</span></div><div class="meta-row"><span>بازه</span><span>{html.escape(str(c.get('interval','24')))} ساعت</span></div></div>
<div class="actions"><a class="btn primary" href="/telegram">{ui_icon("settings", "inline-icon")} تنظیمات Telegram</a><a class="btn" href="/test">{ui_icon("test", "inline-icon")} ارسال تست</a></div></article>
<article class="glass card backup-controls"><div class="card-head"><div style="display:flex;gap:12px;min-width:0">{ui_icon("settings", "card-icon")}<div style="min-width:0"><h3 class="title">تنظیمات Backup</h3><p class="sub">Backup Controls</p></div></div>{status_badge("فعال" if status == "active" else "متوقف", "ok" if status == "active" else "bad")}</div>
<p class="empty">زمان‌بندی را روشن/خاموش کنید، Backup دستی بگیرید یا مشخص کنید PG-Node هم همراه Backup ذخیره شود.</p><div class="actions backup-actions"><a class="btn primary" href="/backup-settings">مدیریت Backup</a><form method="post" action="/backup" style="display:flex;min-width:0">{hidden_csrf(self.sid())}<button class="btn good" type="submit">{ui_icon("backup", "inline-icon")} Backup دستی</button></form></div></article>
<article class="glass wide"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("test", "card-icon")}<div><h3 class="title">ارسال تست پیام به Telegram</h3><p class="sub">قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.</p></div></div></div><div class="actions"><a class="btn primary" href="/test">{ui_icon("test", "inline-icon")} ارسال پیام تست</a><span class="sub" style="align-self:center">با Chat ID و Topic فعلی ارسال می‌شود.</span></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("clock", "card-icon")}<div><h3 class="title">Backup بعدی</h3><p class="sub">Next Scheduled Backup</p></div></div>{status_badge('فعال' if status == 'active' else 'متوقف', 'ok' if status == 'active' else 'bad')}</div><div class="meta"><div class="meta-row"><span>{ui_icon("clock", "meta-icon")} زمان باقی‌مانده</span><strong id="backupCountdown">{html.escape(str(_next_backup_seconds(c.get('interval','24'), backup_info.get('latest_mtime')) if status == 'active' else '—'))}</strong></div><div class="meta-row"><span>{ui_icon("settings", "meta-icon")} وضعیت Scheduler</span><strong>{'فعال' if status == 'active' else 'متوقف'}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("activity", "card-icon")}<div><h3 class="title">{html.escape(ui_now.get("texts",{}).get("activity_title","آخرین فعالیت‌ها"))}</h3><p class="sub">Recent Activity</p></div></div></div><div class="activity-list">{''.join(f'<div class="meta-row activity-{html.escape(str(a.get("kind","ok")))}"><span>{html.escape(str(a.get("message","")))}</span><strong>{html.escape(_relative_time(a.get("time")))}</strong></div>' for a in get_recent_activities()[:max(1,min(20,int(ui_now.get("activity_limit",3) or 3)))] ) or '<div class="empty">هنوز فعالیتی ثبت نشده.</div>'}</div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("health", "card-icon")}<div><h3 class="title">Health Check</h3><p class="sub">وضعیت سرویس‌ها</p></div></div></div><div class="health-list">{''.join(f'<div class="meta-row has-status"><span class="meta-label">{status_dot("ok" if ok else "bad")} {html.escape(name)}</span><strong>{html.escape(detail)}</strong></div>' for name,ok,detail in get_health_info(c,panel_info))}</div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("disk", "card-icon")}<div><h3 class="title">فضای دیسک</h3><p class="sub">Disk Monitor</p></div></div></div><div class="disk-meter"><div class="disk-bar"><span style="width:{get_disk_info()["percent"]}%"></span></div><div class="meta-row"><span>استفاده‌شده</span><strong>{html.escape(str(get_disk_info()["used"]))}</strong></div><div class="meta-row"><span>آزاد</span><strong>{html.escape(str(get_disk_info()["free"]))}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("chart", "card-icon")}<div><h3 class="title">Backup هفت روز اخیر</h3><p class="sub">Backup Activity</p></div></div></div><div class="mini-chart">{''.join(f'<div class="chart-col"><div class="chart-value">{html.escape(_format_bytes(size)) if size else "0 B"}</div><div class="chart-bar"><span style="height:{pct}%"></span></div><small>{html.escape(label)}</small></div>' for label,size,pct in get_backup_chart())}</div><div class="actions"><a class="btn" href="/backups">{ui_icon("backup", "inline-icon")} مدیریت Backupها</a></div></article>
<article class="glass wide"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("chart", "card-icon")}<div><h3 class="title">منابع سرور مجازی</h3><p class="sub">CPU · RAM · Disk · پایش لحظه‌ای</p></div></div></div>{_resource_chart_html()}</article>
</div>'''
            self.send_html(page("Dashboard", body)); return

        if path == "/backups":
            archives=_backup_archives(); rows=[]
            for item,size,mtime in archives:
                name=html.escape(item.name); q=urllib.parse.quote(item.name,safe="")
                rows.append(f'<div class="backup-row"><div><strong>📦 {name}</strong><div class="sub">{html.escape(time.strftime("%Y-%m-%d %H:%M",time.localtime(mtime)))} · {html.escape(_format_bytes(size))}</div></div><div class="actions compact"><a class="btn" href="/backup-download?name={q}">{ui_icon("download", "inline-icon")} دانلود</a><form method="post" action="/backup-delete">{hidden_csrf(self.sid())}<input type="hidden" name="name" value="{name}"><button class="btn danger" type="submit">{ui_icon("trash", "inline-icon")} حذف</button></form></div></div>')
            body=f"""<section class="hero"><h2>{ui_icon("backup", "hero-icon")} <span class="gradient">مدیریت Backupها</span></h2><p>Backupهای موجود را مشاهده، دانلود یا حذف کنید.</p></section><div class="glass wide"><div class="backup-list">{''.join(rows) or '<div class="empty">هنوز Backupای پیدا نشد.</div>'}</div><div class="actions"><a class="btn" href="/">← داشبورد</a><form method="post" action="/backup">{hidden_csrf(self.sid())}<button class="btn primary" type="submit">{ui_icon("rocket", "inline-icon")} ساخت Backup جدید</button></form></div></div>"""
            self.send_html(page("Backup Manager",body)); return

        if path == "/backup-download":
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query); requested=Path(query.get("name",[""])[0]).name
            target=next((item for item,_,_ in _backup_archives() if item.name==requested),None)
            if not target or not target.is_file():
                self.send_html(page("Not Found",'<div class="glass"><div class="notice bad">Backup پیدا نشد.</div></div>'),404); return
            try:
                data=target.read_bytes(); self.send_response(200); self.send_header("Content-Type","application/zip"); self.send_header("Content-Length",str(len(data))); self.send_header("Content-Disposition",f'attachment; filename="{target.name}"'); self.end_headers(); self.wfile.write(data)
            except OSError:
                self.send_html(page("Backup",'<div class="glass"><div class="notice bad">خواندن Backup ناموفق بود.</div></div>'),500)
            return


        if path == "/account":
            body = f'''<section class="hero"><h2>{ui_icon("account", "hero-icon")} <span class="gradient">حساب کاربری</span></h2><p>نام کاربری و رمز ورود را مدیریت کنید.</p></section><div class="glass wide"><form method="post" action="/account">{hidden_csrf(self.sid())}<div class="field"><label>نام کاربری</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" value="{html.escape(canonical_username(c.get("username", "admin")))}" autocomplete="username" required><div class="hint">فقط حروف انگلیسی، عدد و خط تیره؛ ۵ تا ۳۲ کاراکتر.</div></div><div class="field"><label>رمز عبور جدید</label><input type="password" name="password" minlength="8" maxlength="128" autocomplete="new-password"><div class="hint">برای تغییر رمز، حداقل ۸ کاراکتر وارد کنید. اگر قصد تغییر رمز ندارید، این بخش را خالی بگذارید.</div></div><div class="field"><label>تکرار رمز جدید</label><input type="password" name="password_confirm" minlength="8" maxlength="128" autocomplete="new-password"></div><div class="actions"><button class="btn primary" type="submit">{ui_icon("settings", "inline-icon")} ذخیره تغییرات</button></div></form></div>'''
            self.send_html(page("Account", body)); return

        if path == "/telegram":
            body = f'''<section class="hero"><h2>{ui_icon("telegram", "hero-icon")} <span class="gradient">بکاپ تلگرام</span></h2><p>اطلاعات ربات، مقصد، Topic، پروکسی و زمان‌بندی را تنظیم کنید؛ سپس Scheduler را شروع کنید.</p></section><div class="glass wide"><form method="post" action="/telegram">{hidden_csrf(self.sid())}<div class="grid"><div class="field" style="grid-column:span 6"><label>Telegram Bot Token</label><input name="token" value="{html.escape(c.get('token',''))}" placeholder="123456:ABC..." required><div class="hint">توکن BotFather را وارد کنید.</div></div><div class="field" style="grid-column:span 6"><label>Chat ID</label><input name="chat" value="{html.escape(c.get('chat',''))}" placeholder="-1001234567890" required></div><div class="field" style="grid-column:span 6"><label>Topic / Thread ID</label><input name="topic" value="{html.escape(c.get('topic',''))}" placeholder="12345"><div class="hint">شماره Topic را وارد کنید؛ لینک Topic تلگرام هم قابل قبول است.</div></div><div class="field" style="grid-column:span 6"><label>Telegram Proxy</label><input name="proxy" value="{html.escape(c.get('proxy',''))}" placeholder="socks5://127.0.0.1:1080"><div class="hint">اختیاری. اگر Proxy ندارید خالی بگذارید.</div></div></div><div class="actions"><button class="btn primary" type="submit">{ui_icon("settings", "inline-icon")} ذخیره تنظیمات</button><a class="btn" href="/test">{ui_icon("test", "inline-icon")} تست اتصال</a><a class="btn" href="/">← برگشت</a></div></form></div>'''
            self.send_html(page("Telegram Backup", body)); return

        if path == "/backup-settings":
            checked = "checked" if c.get("node") else ""
            body = f'''<section class="hero"><h2>{ui_icon("settings", "hero-icon")} تنظیمات <span class="gradient">Backup</span></h2><p>کنترل Scheduler و اجرای Backup دستی.</p></section><div class="grid"><article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("clock", "card-icon")}<div><h3 class="title">زمان‌بندی خودکار</h3><p class="sub">Scheduler</p></div></div><span class="status {'on' if status=='active' else 'off'}">{html.escape(status)}</span></div><form method="post" action="/backup-settings">{hidden_csrf(self.sid())}<div class="field"><label>بازه Backup (ساعت)</label><input name="interval" type="number" step="0.5" min="0.5" max="720" value="{html.escape(str(c.get('interval','24')))}" required></div><label class="toggle"><span>شامل PG-Node شود</span><input type="checkbox" name="node" {checked}></label><div class="actions"><button class="btn primary" type="submit">{ui_icon("rocket", "inline-icon")} ذخیره و شروع</button><button class="btn danger" type="submit" formaction="/stop" formmethod="post">{ui_icon("activity", "inline-icon")} توقف</button></div></form></article><article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("backup", "card-icon")}<div><h3 class="title">Backup دستی</h3><p class="sub">Manual Backup</p></div></div></div><p class="empty">همین حالا یک Backup کامل بگیرید و طبق تنظیمات Telegram برای مقصد فعلی ارسال کنید.</p><form method="post" action="/backup">{hidden_csrf(self.sid())}<button class="btn good full">{ui_icon("rocket", "inline-icon")} شروع Backup دستی</button></form></article></div>'''
            self.send_html(page("Backup Settings", body)); return

        if path == "/test":
            body = f'''<section class="hero"><h2>{ui_icon("test", "hero-icon")} تست <span class="gradient">Telegram</span></h2><p>یک پیام آزمایشی با تنظیمات فعلی ارسال می‌شود.</p></section><div class="glass wide"><div class="meta"><div class="meta-row"><span>Chat ID</span><span>{html.escape(c.get('chat') or 'تنظیم نشده')}</span></div><div class="meta-row"><span>Topic ID</span><span>{html.escape(c.get('topic') or '—')}</span></div><div class="meta-row"><span>Proxy</span><span>{html.escape(c.get('proxy') or 'بدون Proxy')}</span></div></div><form method="post" action="/test">{hidden_csrf(self.sid())}<div class="actions"><button class="btn primary">{ui_icon("test", "inline-icon")} ارسال پیام تست</button><a class="btn" href="/">← برگشت</a></div></form></div>'''
            self.send_html(page("Telegram Test", body)); return

        self.send_html(page("404", '<div class="glass"><h2>404</h2><p class="empty">صفحه پیدا نشد.</p></div>'), 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        c = load_cfg()
        data = self.form()

        if path == "/setup":
            # First-run setup is one-time only. Once a password exists, this route
            # can never be used to replace the admin credentials.
            if c.get("password_hash"):
                self.send_html(page("Setup", '<div class="glass"><div class="notice bad">راه‌اندازی اولیه قبلاً انجام شده است.</div><a class="btn" href="/login">ورود به پنل</a></div>', False), 403); return
            username = data.get("username", "").strip()
            pw = data.get("password", "")
            confirm = data.get("password_confirm", "")
            if not valid_username(username):
                self.send_html(page("Setup", '<section class="login"><div class="glass"><div class="notice bad">نام کاربری باید ۵ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد یا خط تیره باشد.</div><a class="btn" href="/">تلاش دوباره</a></div></section>', False)); return
            if not valid_password(pw):
                self.send_html(page("Setup", '<section class="login"><div class="glass"><div class="notice bad">رمز باید حداقل ۸ کاراکتر، شامل حداقل ۲ حرف، ۱ عدد و یکی از # @ * باشد.</div><a class="btn" href="/">تلاش دوباره</a></div></section>', False)); return
            if pw != confirm:
                self.send_html(page("Setup", '<section class="login"><div class="glass"><div class="notice bad">تکرار رمز عبور با رمز جدید یکسان نیست.</div><a class="btn" href="/">تلاش دوباره</a></div></section>', False)); return
            salt, digest = hash_password(pw)
            c.update({"username": username, "password_salt": salt, "password_hash": digest})
            save_cfg(c)
            self.redirect("/login"); return

        if path == "/login":
            username_ok = hmac.compare_digest(data.get("username", "").strip(), canonical_username(c.get("username", "admin")))
            if username_ok and check_password(data.get("password", ""), c):
                sid = secrets.token_urlsafe(32)
                SESSIONS[sid] = {"created": time.time(), "csrf": secrets.token_urlsafe(24), "role": "user"}
                self.send_response(302)
                self.send_header("Set-Cookie", "idontpg_session=" + sid + "; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self.send_html(self.login_page("نام کاربری یا رمز عبور اشتباه است."), 401)
            return

        if path == ADMIN_PATH:
            if not self.admin_logged():
                ip=self.client_address[0] if self.client_address else "unknown"; now=time.time(); recent=[x for x in ADMIN_LOGIN_ATTEMPTS.get(ip,[]) if now-x<900]
                if len(recent)>=5: self.send_html(self.admin_login_page("تلاش زیاد؛ ۱۵ دقیقه دیگر دوباره امتحان کنید."),429); return
                ok=hmac.compare_digest(data.get("admin_username","").strip(),canonical_username(c.get("username","admin"))) and check_password(data.get("admin_password",""),c)
                if not ok:
                    recent.append(now); ADMIN_LOGIN_ATTEMPTS[ip]=recent; self.send_html(self.admin_login_page("نام کاربری یا رمز ادمین اشتباه است."),401); return
                ADMIN_LOGIN_ATTEMPTS.pop(ip,None); sid=secrets.token_urlsafe(32); SESSIONS[sid]={"created":time.time(),"csrf":secrets.token_urlsafe(24),"role":"admin"}
                self.send_response(303); self.send_header("Set-Cookie",f"idontpg_session={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=7200"); self.send_header("Location",ADMIN_PATH); self.end_headers(); return
            if not self.require_csrf(data):
                self.send_html(page("Security",'<div class="glass"><div class="notice bad">درخواست نامعتبر است.</div></div>'),403); return
            try:
                ui=idont_apply_ui_settings(data)
                t=ui.setdefault("theme",{})
                t["content_width"]=_ui_int(data.get("content_width",t.get("content_width",1180)),1180,860,1500)
                t["card_gap"]=_ui_int(data.get("card_gap",t.get("card_gap",16)),16,8,32)
                t["shadow_strength"]=_ui_float(data.get("shadow_strength",t.get("shadow_strength",.5)),.5,0,1)
                ui["brand_subtitle"]=str(data.get("brand_subtitle",ui.get("brand_subtitle","Backup Control Center · durwinam")))[:120]
                idont_save_ui_settings(ui)
                self.send_html(page("Admin",'<div class="glass"><div class="notice ok">تنظیمات با موفقیت ذخیره شد و روی کل وب‌پنل اعمال شد.</div><a class="btn primary" href="'+ADMIN_PATH+'">بازگشت به مدیریت</a></div>'))
            except Exception as exc:
                self.send_html(page("Admin",f'<div class="glass"><div class="notice bad">ذخیره تنظیمات ناموفق بود: {html.escape(str(exc))}</div><a class="btn" href="{ADMIN_PATH}">تلاش دوباره</a></div>'),500)
            return

        if not self.logged():
            self.redirect("/login"); return
        if not self.require_csrf(data):
            self.send_html(page("Security", '<div class="glass"><div class="notice bad">درخواست نامعتبر یا منقضی شده است. صفحه را دوباره باز کنید.</div></div>'), 403); return

        if path == "/account":
            username = data.get("username", "").strip()
            pw = data.get("password", "")
            confirm = data.get("password_confirm", "")
            if not valid_username(username):
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">نام کاربری باید ۵ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد یا خط تیره باشد.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
            c["username"] = username
            if pw or confirm:
                if not valid_password(pw):
                    self.send_html(page("Account", '<div class="glass"><div class="notice bad">رمز جدید باید حداقل ۸ کاراکتر، شامل حروف انگلیسی، حداقل یک حرف بزرگ، یک عدد و یک کاراکتر ویژه باشد.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
                if pw != confirm:
                    self.send_html(page("Account", '<div class="glass"><div class="notice bad">تکرار رمز جدید با رمز عبور یکسان نیست.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
                salt, digest = hash_password(pw)
                c.update({"password_salt": salt, "password_hash": digest})
            save_cfg(c)
            self.send_html(page("Account", '<div class="glass"><div class="notice ok">تنظیمات حساب با موفقیت ذخیره شد.</div><div class="actions"><a class="btn primary" href="/">داشبورد</a><a class="btn" href="/account">حساب کاربری</a></div></div>')); return


        if path == "/telegram":
            c.update({"token": data.get("token", "").strip(), "chat": data.get("chat", "").strip(), "topic": data.get("topic", "").strip(), "proxy": data.get("proxy", "").strip()})
            save_cfg(c)
            self.send_html(page("Telegram", '<div class="glass"><div class="notice ok">تنظیمات Telegram با موفقیت ذخیره شد.</div><a class="btn" href="/telegram">برگشت به Telegram</a></div>')); return

        if path == "/test":
            ok, msg = telegram_test(c)
            kind = "ok" if ok else "bad"
            body = f'<div class="glass"><div class="notice {kind}">{status_dot("ok" if ok else "bad")} {"پیام تست با موفقیت ارسال شد." if ok else "ارسال پیام تست ناموفق بود."}<br><span class="empty">{html.escape(str(msg))}</span></div><div class="actions"><a class="btn" href="/test">تلاش دوباره</a><a class="btn" href="/">داشبورد</a></div></div>'
            self.send_html(page("Telegram Test", body, notice="", kind=kind)); return

        if path == "/backup-settings":
            try:
                interval = min(720, max(0.5, float(data.get("interval", "24"))))
            except Exception:
                interval = 24
            c["interval"] = str(interval)
            c["node"] = data.get("node") == "on"
            save_cfg(c)
            p = scheduler_service("restart")
            ok = p.returncode == 0
            self.send_html(page("Backup Settings", f'<div class="glass"><div class="notice {"ok" if ok else "bad"}">{"Scheduler ذخیره و شروع شد." if ok else "Scheduler ذخیره شد ولی شروع آن با خطا مواجه شد."}</div><a class="btn" href="/backup-settings">برگشت</a></div>')); return

        if path == "/stop":
            p = scheduler_service("stop")
            _record_activity("Scheduler متوقف شد", "ok" if p.returncode == 0 else "bad")
            self.redirect("/backup-settings"); return

        if path == "/backup-delete":
            requested=Path(data.get("name","")).name
            target=next((item for item,_,_ in _backup_archives() if item.name==requested),None)
            if not target or not target.is_file():
                self.send_html(page("Backup",'<div class="glass"><div class="notice bad">Backup پیدا نشد.</div></div>'),404); return
            try:
                target.unlink(); _record_activity(f"Backup حذف شد: {requested}","ok"); self.redirect("/backups")
            except OSError as exc:
                self.send_html(page("Backup",f'<div class="glass"><div class="notice bad">حذف Backup ناموفق بود: {html.escape(str(exc))}</div></div>'),500)
            return

        if path == "/backup":
            try:
                ok, msg = make_backup(send=True)
            except Exception as e:
                ok, msg = False, str(e)
            body = f'<div class="glass"><div class="notice {"ok" if ok else "bad"}">{status_dot("ok" if ok else "bad")} {("Backup با موفقیت ساخته و ارسال شد." if ok else "Backup ناموفق بود.")}<br><span class="empty">{html.escape(str(msg))}</span></div><div class="actions"><a class="btn" href="/backup-settings">تنظیمات Backup</a><a class="btn" href="/">داشبورد</a></div></div>'
            self.send_html(page("Manual Backup", body)); return

        self.send_html(page("404", '<div class="glass"><h2>404</h2></div>'), 404)


def worker():
    # Wait one configured interval before the first scheduled run.
    # Manual backups remain available from the web panel at any time.
    while True:
        c = load_cfg()
        try:
            interval = max(0.5, float(c.get("interval", 24)))
        except Exception:
            interval = 24
        time.sleep(interval * 3600)
        c = load_cfg()
        try:
            if c.get("token") and c.get("chat"):
                core = load_core()
                archive = core.create_backup(bool(c.get("node", False)))
                if archive and os.path.exists(archive):
                    _record_backup_created(archive)
                if archive:
                    ok, msg = send_archive(core, archive, c, f"idontPG-backup · Scheduled · {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    print(("[+]" if ok else "[-]"), msg, flush=True)
            else:
                print("[!] Telegram settings are incomplete; scheduled backup skipped.", flush=True)
        except Exception as e:
            print("[-] Scheduled backup failed:", e, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    args = ap.parse_args()
    if args.worker:
        worker(); return
    print(f"{APP} Web Panel v{VERSION} listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
