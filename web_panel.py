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
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "idontPG-backup"
VERSION = "5.5.4"
HOST = os.environ.get("IDONTPG_HOST", "0.0.0.0")
PORT = int(os.environ.get("IDONTPG_PORT", "5000"))
STATE_DIR = Path("/etc/idontPG-backup")
CONFIG = STATE_DIR / "web.json"
SCRIPT = Path(__file__).resolve()
LOGO_CANDIDATES = [SCRIPT.parent / "web" / "static" / "logo.png", Path("/usr/local/share/idontPG-backup/logo.png")]
CORE_CANDIDATES = [
    Path("/usr/local/bin/idontPG-backup"),
    Path("/usr/local/bin/PG-Backup"),
    SCRIPT.parent / "pg_backup.py",
]
SESSIONS = {}
SESSION_TTL = 12 * 60 * 60


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
        ACTIVITY_FILE.write_text(json.dumps(items[:5], ensure_ascii=False), encoding="utf-8")
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
            if isinstance(raw, list): items = raw[:5]
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
            "Accept": "application/json",
            "User-Agent": "idontPG-backup/5.5.4",
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


def get_panel_storage_usage():
    """Return total user traffic reported by PasarGuard's Node-backed users.

    PasarGuard exposes user usage through GET /api/users; each user contains
    ``used_traffic`` populated from the connected Node(s). Walk every page so
    the dashboard does not accidentally show only the first 100 users.
    """
    try:
        token = _pg_api_token()
        if not token:
            return "قابل دریافت نیست"
        return _format_bytes(_sum_all_panel_users(token))
    except Exception:
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



# ── Neon UI icon system (v5.5.4) ───────────────────────────────────────
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
    # Animated glow is reserved for actual status indicators.
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
.container{width:min(1180px,calc(100% - 34px));margin:0 auto;padding:28px 0 56px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:28px}.brand{display:flex;align-items:center;gap:14px}.brand-logo{width:58px;height:58px;object-fit:cover;border-radius:16px;border:1px solid rgba(255,255,255,.16);box-shadow:0 12px 40px rgba(34,211,238,.20),0 0 28px rgba(139,92,246,.16);transition:.25s ease}.brand-logo:hover{transform:translateY(-2px) scale(1.03);box-shadow:0 16px 50px rgba(34,211,238,.30),0 0 36px rgba(139,92,246,.24)}.brand h1{font-size:21px;margin:0}.brand p{margin:3px 0 0;color:var(--muted);font-size:13px}.pill{border:1px solid var(--line);background:rgba(255,255,255,.05);backdrop-filter:blur(18px);padding:9px 13px;border-radius:999px;color:var(--muted);font-size:12px}.top-actions{display:flex;align-items:center;gap:9px}.theme-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);cursor:pointer;font-size:18px;transition:.25s ease}.theme-toggle:hover{transform:translateY(-2px) rotate(4deg);border-color:rgba(236,72,153,.45);box-shadow:0 10px 30px rgba(236,72,153,.16)}.hero{margin-bottom:22px}.hero h2{font-size:clamp(30px,5vw,54px);line-height:1.02;margin:0 0 10px;letter-spacing:-1.8px}.gradient{background:linear-gradient(90deg,#fff,#c4b5fd,#67e8f9,#f9a8d4);-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{color:var(--muted);max-width:720px;margin:0;line-height:1.7}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px;min-width:0}.light .glass{background:linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.48))}.light .meta-row{background:rgba(255,255,255,.62)}.light .btn{background:rgba(255,255,255,.64);color:#17131f}.light .btn:hover{background:rgba(255,255,255,.86);color:#0f0b16}.light .toggle{background:rgba(255,255,255,.58);border-color:rgba(124,58,237,.18);color:#17131f}.light .notice{background:rgba(255,255,255,.62);color:#17131f}.light .gradient{background:linear-gradient(90deg,#17131f,#4c1d95,#9d174d,#991b1b);-webkit-background-clip:text;background-clip:text;color:transparent}.light .sub,.light .empty,.light .hint,.light .pill,.light .brand p,.light .footer,.light .meta-row span:first-child{color:#17131f}.light .status{color:#17131f}.light .status.on{color:#111827;background:rgba(52,211,153,.18)}.light .status.off{color:#17131f;background:rgba(124,58,237,.08)}.light .btn.good,.light .btn.danger{color:#111827}.light .field label,.light .field input,.light .field select{color:#17131f}.light .field input::placeholder,.light .field select::placeholder{color:#4b3f52}.light .field input,.light .field select{background:rgba(255,255,255,.64)}.light .btn.primary{color:#17131f;text-shadow:none}.light .notice.ok,.light .notice.bad{color:#17131f}.light .theme-toggle{color:#17131f}.light .panel-brand-icon{overflow:hidden}.panel-brand-icon{overflow:hidden;display:grid;place-items:center}.panel-brand-icon img{width:100%;height:100%;object-fit:contain;border-radius:inherit;filter:brightness(0) invert(1);padding:7px}.light .panel-brand-icon img{filter:brightness(0);}
.icon{color:#17131f}.light .brand h1,.light .title{color:#17131f}.light .meta-row strong,.light .meta-row a,.light .meta-row span:last-child{color:#17131f}.hero h2{overflow-wrap:anywhere;word-break:break-word}.grid{overflow:visible}.glass{min-width:0;overflow:hidden;background:linear-gradient(145deg,var(--glass),rgba(255,255,255,.025));border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-radius:24px;padding:22px}.card{grid-column:span 6;min-width:0;transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease}.card:hover{transform:translateY(-4px);border-color:rgba(236,72,153,.42);box-shadow:0 28px 90px rgba(0,0,0,.5)}.wide{grid-column:span 12}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:18px;min-width:0}.card-head>div{min-width:0;flex:1 1 auto}.card-head>.status,.card-head>.status-wrap{flex:0 0 auto;white-space:nowrap}.status-wrap{display:inline-flex;align-items:center}.card-head .title{overflow-wrap:anywhere;word-break:break-word}.icon{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.2);font-size:21px}.title{font-size:18px;font-weight:750;margin:0 0 4px}.sub{font-size:12px;color:var(--muted);margin:0}.status{font-size:11px;padding:7px 10px;border-radius:999px;border:1px solid var(--line)}.status.on{color:var(--good);background:rgba(52,211,153,.08)}.status.off{color:var(--muted)}.meta{display:grid;gap:10px;margin:18px 0}.meta-row{display:flex;justify-content:space-between;gap:12px;min-width:0;overflow:hidden;padding:11px 12px;border-radius:14px;background:var(--glass2);border:1px solid var(--line);transition:.2s}.meta-row:hover{transform:translateX(-2px);border-color:rgba(236,72,153,.28)}/* v5.5.4 final icon/status system: icons are decorative; status glow is state-only. */
.neo-icon{position:relative;display:inline-grid;place-items:center;flex:0 0 44px;width:44px;height:44px;border-radius:15px;color:#8fdcff;background:linear-gradient(145deg,rgba(93,225,255,.13),rgba(124,58,237,.18) 58%,rgba(236,72,153,.10));border:1px solid rgba(120,180,255,.22);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 0 22px rgba(76,201,240,.08),0 8px 28px rgba(124,58,237,.08);transition:transform .25s ease,box-shadow .25s ease,color .25s ease,border-color .25s ease;overflow:visible}
.neo-icon:before{content:"";position:absolute;inset:5px;border-radius:11px;background:radial-gradient(circle,rgba(255,255,255,.07),transparent 68%);pointer-events:none}.neo-icon:after{display:none!important}.neo-icon svg{position:relative;z-index:1;width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 5px currentColor)}@keyframes iconSpark{0%,100%{transform:scale(.72);opacity:.45}50%{transform:scale(1.18);opacity:1}}
.neo-icon.card-icon{flex:0 0 50px;width:50px;height:50px;border-radius:17px}
.neo-icon.card-icon svg{width:25px;height:25px;stroke-width:1.7}
.neo-icon.meta-icon{flex-basis:18px;width:18px;height:18px;border:0;background:transparent;box-shadow:none;border-radius:0;color:#9fb5d7;overflow:visible}.neo-icon.meta-icon:before,.neo-icon.meta-icon:after,.neo-icon.inline-icon:before,.neo-icon.inline-icon:after{display:none}
.neo-icon.meta-icon svg{width:17px;height:17px;filter:drop-shadow(0 0 3px currentColor);stroke-width:1.8}
.neo-icon.inline-icon{flex-basis:18px;width:18px;height:18px;border:0;background:transparent;box-shadow:none;color:currentColor;border-radius:0}
.neo-icon.inline-icon svg{width:18px;height:18px;filter:none}
.neo-icon:hover{transform:translateY(-2px) scale(1.03);box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 0 26px rgba(76,201,240,.14)}
.icon-backup,.icon-cpu{color:#67e8f9}.icon-panel,.icon-ram{color:#8ab4ff}.icon-telegram,.icon-link{color:#6ea8ff}.icon-settings,.icon-dashboard{color:#b58cff}.icon-account{color:#ff8bc7}.icon-clock{color:#ffd166}.icon-health{color:#59e39a}.icon-chart,.icon-disk{color:#ff9f68}.icon-activity,.icon-traffic{color:#65e6d1}.icon-test{color:#ffe36e}.icon-download{color:#70d7ff}.icon-trash{color:#ff7f96}.icon-rocket{color:#ff78c8}
.status{display:inline-flex;align-items:center;gap:7px;font-size:11px;padding:7px 10px;border-radius:999px;border:1px solid var(--line);white-space:nowrap}
.status-dot{display:inline-block!important;width:7px!important;height:7px!important;min-width:7px!important;max-width:7px!important;min-height:7px!important;max-height:7px!important;flex:0 0 7px!important;padding:0!important;margin:0!important;border-radius:50%!important;background:#7c8aa5;box-shadow:0 0 0 3px rgba(124,138,165,.08),0 0 10px rgba(124,138,165,.35);animation:statusPulse 1.8s ease-in-out infinite}
.status-dot.status-ok{background:#43e39a;box-shadow:0 0 0 3px rgba(67,227,154,.10),0 0 13px rgba(67,227,154,.85)}
.status-dot.status-info{background:#61b7ff;box-shadow:0 0 0 3px rgba(97,183,255,.10),0 0 13px rgba(97,183,255,.82)}
.status-dot.status-warn{background:#ffd45c;box-shadow:0 0 0 3px rgba(255,212,92,.10),0 0 13px rgba(255,212,92,.82)}
.status-dot.status-bad{background:#ff5e78;box-shadow:0 0 0 3px rgba(255,94,120,.10),0 0 13px rgba(255,94,120,.85)}
.status-dot.status-off{background:#8a96ad;box-shadow:0 0 0 3px rgba(138,150,173,.08),0 0 10px rgba(138,150,173,.35);animation:none}.logo-fallback-text{display:none;font-weight:900;font-size:16px;letter-spacing:-.5px;color:#8fdcff;text-shadow:0 0 12px #6ee7ff}.logo-fallback .logo-fallback-text{display:block}.logo-fallback img{display:none!important}.panel-brand-icon{position:relative}.panel-brand-icon:after{display:none!important}.status.status-ok .status-dot{animation:statusPulse 1.8s ease-in-out infinite}.status.status-bad .status-dot{animation:statusPulse 1.2s ease-in-out infinite}.status.status-warn .status-dot{animation:statusPulse 1.5s ease-in-out infinite}
.status.status-ok{color:#8ff0c2;background:rgba(67,227,154,.075);border-color:rgba(67,227,154,.20)}
.status.status-info{color:#9ed4ff;background:rgba(97,183,255,.075);border-color:rgba(97,183,255,.20)}
.status.status-warn{color:#ffe58d;background:rgba(255,212,92,.075);border-color:rgba(255,212,92,.20)}
.status.status-bad{color:#ff9aad;background:rgba(255,94,120,.075);border-color:rgba(255,94,120,.20)}
.status.status-off{color:var(--muted);background:rgba(124,138,165,.055);border-color:var(--line)}
@keyframes statusPulse{0%,100%{transform:scale(.92);opacity:.82}50%{transform:scale(1.16);opacity:1}}
.meta-row span:first-child{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:7px;min-width:0;overflow:hidden}.meta-row span:first-child .neo-icon{flex:0 0 18px}.meta-row strong,.meta-row a{flex:0 1 auto;color:var(--text)}.meta-row a{color:var(--accent2);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}.meta-row span:last-child,.meta-row strong,.meta-row a{font-size:12px;max-width:65%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}.actions .btn{min-height:44px}.actions form{display:flex}.actions form .btn{height:100%}.btn{display:inline-flex;min-width:132px;max-width:100%;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);border-radius:14px;padding:12px 15px;color:var(--text);text-decoration:none;font-weight:700;font-size:13px;cursor:pointer;background:var(--glass2);transition:.22s;position:relative;overflow:hidden;white-space:normal;text-align:center}.btn:before{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 20%,rgba(255,255,255,.18),transparent 80%);transform:translateX(-120%);transition:.55s}.btn:hover:before{transform:translateX(120%)}.btn:hover{transform:translateY(-2px);background:rgba(255,255,255,.14);border-color:rgba(236,72,153,.28);box-shadow:0 10px 24px rgba(124,58,237,.16)}.btn:active{transform:translateY(0) scale(.98)}.btn.primary{border-color:transparent;background:linear-gradient(135deg,#7c3aed,#db2777,#ef4444);background-size:180% 180%;animation:buttonGlow 6s ease infinite;box-shadow:0 10px 28px rgba(219,39,119,.22)}@keyframes buttonGlow{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}.btn.good{border-color:rgba(52,211,153,.2);background:rgba(52,211,153,.09);color:#b7f7dc}.btn.danger{border-color:rgba(251,113,133,.2);background:rgba(251,113,133,.08);color:#fecdd3}.btn.full{width:100%}form{margin:0}.field{margin-bottom:16px}.field label{display:block;font-size:12px;color:var(--text);margin-bottom:7px}.field input,.field select{width:100%;border:1px solid var(--line);background:var(--glass2);color:var(--text);border-radius:14px;padding:13px 14px;outline:none;font-size:13px}.field input:focus,.field select:focus{border-color:rgba(103,232,249,.65);box-shadow:0 0 0 4px rgba(34,211,238,.08)}.hint{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.6}.toggle{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px;border-radius:16px;background:rgba(0,0,0,.14);border:1px solid rgba(255,255,255,.07);margin-bottom:14px}.toggle input{accent-color:#db2777;width:18px;height:18px}.notice{margin-bottom:16px;padding:14px 16px;border-radius:16px;border:1px solid var(--line);background:rgba(255,255,255,.055);color:#dbeafe}.notice.ok{border-color:rgba(52,211,153,.25);background:rgba(52,211,153,.07)}.notice.bad{border-color:rgba(251,113,133,.25);background:rgba(251,113,133,.07)}.footer{text-align:center;color:#667085;font-size:11px;padding-top:28px}.login{width:min(460px,100%);margin:9vh auto}.login .glass{padding:30px}.empty{color:var(--muted);font-size:13px;line-height:1.7}.activity-list{display:grid;gap:9px}.activity-list .meta-row{margin:0}.activity-bad{border-color:rgba(251,113,133,.30)}.backup-controls .card-head{min-width:0}.backup-controls .empty{max-width:100%;overflow-wrap:anywhere}.backup-actions{width:100%;min-width:0}.backup-actions .btn{min-width:0;max-width:100%}.backup-actions form{min-width:0;max-width:100%}.backup-actions form .btn{min-width:0}@media(min-width:801px){.backup-actions .btn{min-width:0}.backup-actions form{flex:1}.backup-actions form .btn{width:100%}.backup-actions>a.btn{flex:1}}@media(max-width:800px){.card-head{gap:10px;align-items:center}.card-head>.status{font-size:10px;padding:6px 8px}.meta-row{align-items:center}.meta-row span:first-child{flex:1 1 52%;}.meta-row span:last-child,.meta-row strong,.meta-row a{flex:0 1 48%;max-width:48%;}.top-actions{margin-right:auto}.actions .btn,.actions form{width:100%}.actions form .btn{width:100%}.card,.wide{grid-column:span 12}.topbar{align-items:flex-start}.pill{display:none}.container{width:calc(100% - 22px);max-width:1180px;padding-top:18px;min-width:0}.glass{border-radius:20px;padding:18px}.grid{grid-template-columns:minmax(0,1fr);width:100%;}.card,.wide{grid-column:1/-1;width:100%;}.topbar{flex-wrap:wrap;}.top-actions{margin-right:0;}.actions{min-width:0;}.meta-row a,.meta-row span:last-child,.meta-row strong{max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}.brand{min-width:0;}.brand>div{min-width:0;}}

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

"""



def page(title, body, logged=True, notice="", kind="ok"):
    nav = "" if not logged else f'''<div class="drawer-backdrop" id="drawerBackdrop"></div><aside class="drawer" id="drawer" aria-hidden="true"><div class="drawer-head"><img class="drawer-logo" src="/static/logo.png" alt="idontPG-backup"><div><h3>idontPG-backup</h3><p>Backup Control Center</p></div><button class="drawer-close" id="drawerClose" type="button" aria-label="بستن منو">×</button></div><div class="drawer-section">منوی اصلی</div><nav class="drawer-nav"><a class="drawer-link" href="/">{ui_icon("dashboard", "drawer-icon")}<span><strong>داشبورد</strong><small>نمای کلی سیستم</small></span></a><a class="drawer-link" href="/telegram">{ui_icon("telegram", "drawer-icon")}<span><strong>بکاپ تلگرام</strong><small>تنظیمات و ارسال</small></span></a><a class="drawer-link" href="/backup-settings">{ui_icon("settings", "drawer-icon")}<span><strong>تنظیمات بکاپ</strong><small>Scheduler و Backup</small></span></a><a class="drawer-link" href="/test">{ui_icon("test", "drawer-icon")}<span><strong>تست تلگرام</strong><small>بررسی اتصال</small></span></a><a class="drawer-link" href="/account">{ui_icon("account", "drawer-icon")}<span><strong>حساب کاربری</strong><small>مدیریت ورود</small></span></a><a class="drawer-link logout" href="/logout">{ui_icon("rocket", "drawer-icon")}<span><strong>خروج</strong><small>پایان نشست</small></span></a></nav></aside>'''
    notice_html = f'<div class="notice {kind}">{html.escape(notice)}</div>' if notice else ""
    return f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#06070d"><title>{html.escape(title)} · {APP}</title><style>{CSS}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{{outline:2px solid #67e8f9;outline-offset:3px}}

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
.backup-row{{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line)}}.backup-row:last-child{{border-bottom:0}}.compact{{margin:0!important;gap:8px}}.compact form{{margin:0}}.btn.danger{{border-color:rgba(239,68,68,.35)}}.health-list .meta-row strong{{font-size:12px}}.disk-meter{{padding-top:6px}}.disk-bar{{height:10px;border-radius:999px;background:rgba(127,127,127,.16);overflow:hidden;margin:8px 0 14px}}.disk-bar span{{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--pink),var(--red));transition:width .4s ease}}.resource-monitor{{display:grid;gap:18px}}.resource-summary{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.resource-stat{{position:relative;display:flex;align-items:center;gap:12px;padding:15px 16px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(135deg,rgba(255,255,255,.055),rgba(255,255,255,.018));box-shadow:inset 0 1px 0 rgba(255,255,255,.05);overflow:hidden}}.resource-stat:after{{content:"";position:absolute;inset:auto -25px -45px auto;width:100px;height:100px;border-radius:50%;filter:blur(25px);opacity:.22}}.resource-stat.cpu:after{{background:#6ee7ff}}.resource-stat.ram:after{{background:#ff4fa3}}.resource-stat.disk:after{{background:#ff9b4a}}.rs-icon{{width:40px;height:40px;display:grid;place-items:center;border-radius:13px;background:rgba(255,255,255,.055);font-size:20px}}.resource-stat.cpu .rs-icon{{color:#6ee7ff}}.resource-stat.ram .rs-icon{{color:#ff4fa3}}.resource-stat.disk .rs-icon{{color:#ff9b4a}}.resource-stat small{{display:block;color:var(--muted);font-size:9px;letter-spacing:1.2px;font-weight:800}}.resource-stat strong{{display:block;font-family:"Trebuchet MS","Segoe UI",Tahoma,sans-serif;font-size:22px;margin-top:3px;letter-spacing:.2px}}.resource-stat i{{position:absolute;left:0;bottom:0;width:42%;height:2px}}.resource-stat.cpu i{{background:linear-gradient(90deg,#6ee7ff,transparent)}}.resource-stat.ram i{{background:linear-gradient(90deg,#ff4fa3,transparent)}}.resource-stat.disk i{{background:linear-gradient(90deg,#ff9b4a,transparent)}}.resource-plot{{border:1px solid var(--line);border-radius:22px;background:linear-gradient(145deg,rgba(10,13,28,.66),rgba(41,14,48,.38));padding:18px 18px 12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 20px 60px rgba(0,0,0,.12);overflow:hidden}}.light .resource-plot{{background:linear-gradient(145deg,rgba(255,255,255,.55),rgba(255,224,242,.42))}}.plot-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}}.plot-kicker{{display:block;font-size:9px;letter-spacing:1.8px;color:#a78bfa;font-weight:900;margin-bottom:3px}}.plot-head b{{font-size:15px}}.live-dot{{display:flex;align-items:center;gap:7px;font-size:10px;letter-spacing:1px;font-weight:900;color:#6ee7b7}}.live-dot i{{width:7px;height:7px;border-radius:50%;background:#6ee7b7;box-shadow:0 0 12px #6ee7b7;animation:livePulse 1.5s ease-in-out infinite}}@keyframes livePulse{{50%{{opacity:.35;transform:scale(.72)}}}}.plot-body{{position:relative;padding-right:38px}}.y-axis{{position:absolute;right:0;top:0;bottom:25px;width:34px;display:flex;flex-direction:column;justify-content:space-between;align-items:flex-start;color:var(--muted);font-size:9px}}.telemetry{{display:block;width:100%;height:270px;overflow:visible}}.grid-lines line{{stroke:currentColor;stroke-opacity:.08;stroke-width:1}}.area{{stroke:none}}.cpu-area{{fill:url(#fillCpu)}}.ram-area{{fill:url(#fillRam)}}.disk-area{{fill:url(#fillDisk)}}.line{{fill:none;stroke-width:2.7;stroke-linecap:round;stroke-linejoin:round;filter:url(#glowC)}}.cpu-line{{stroke:#6ee7ff}}.ram-line{{stroke:#ff4fa3;filter:url(#glowP)}}.disk-line{{stroke:#ff9b4a}}.dot{{stroke:rgba(255,255,255,.9);stroke-width:2}}.cpu-dot{{fill:#6ee7ff}}.ram-dot{{fill:#ff4fa3}}.disk-dot{{fill:#ff9b4a}}.chart-legend{{display:flex;align-items:center;justify-content:flex-start;gap:18px;margin-top:2px;padding:0 4px;flex-wrap:wrap}}.legend{{display:inline-flex;align-items:center;gap:6px;font-size:10px;color:var(--muted);font-weight:800}}.legend i{{width:8px;height:8px;border-radius:50%;box-shadow:0 0 10px currentColor}}.legend.cpu{{color:#6ee7ff}}.legend.ram{{color:#ff4fa3}}.legend.disk{{color:#ff9b4a}}.updated{{margin-right:auto;font-size:9px;color:var(--muted)}}.mini-chart{{height:190px;display:flex;align-items:end;gap:8px;padding:10px 2px 4px}}.chart-col{{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:end;gap:6px}}.chart-value{{font-size:9px;color:var(--muted);white-space:nowrap;overflow:hidden;max-width:100%;text-overflow:ellipsis}}.chart-bar{{height:115px;width:min(28px,70%);display:flex;align-items:end;border-radius:10px 10px 4px 4px;background:rgba(139,92,246,.10);overflow:hidden}}.chart-bar span{{display:block;width:100%;border-radius:inherit;background:linear-gradient(180deg,var(--accent2),var(--accent),var(--pink));min-height:3px}}.chart-col small{{font-size:9px;color:var(--muted)}}@media(max-width:720px){{.backup-row{{align-items:stretch;flex-direction:column}}.backup-row .actions{{width:100%}}.mini-chart{{height:170px}}.chart-value{{font-size:8px}}.resource-summary{{grid-template-columns:1fr}}.resource-stat{{padding:12px 14px}}.resource-stat strong{{font-size:20px}}.resource-plot{{padding:15px 12px 10px;border-radius:18px}}.telemetry{{height:210px}}.plot-body{{padding-right:32px}}.chart-legend{{gap:12px}}.updated{{width:100%;margin-right:0}}}}</style></head><body><div class="aurora"><i class="orb o1"></i><i class="orb o2"></i><i class="orb o3"></i></div><main class="container"><header class="topbar"><div class="brand"><img class="brand-logo" src="/static/logo.png" alt="IDONTPG Backup"><div><h1>{APP}</h1><p>Backup Control Center · durwinam</p></div></div><div class="top-actions"><button class="menu-toggle" id="menuToggle" type="button" aria-label="باز کردن منو" title="منو"><span class="hamb">☰</span></button><button class="theme-toggle" id="themeToggle" type="button" aria-label="تغییر تم" title="تغییر تم">🌙</button><div class="pill">v{VERSION} · Secure Glass UI</div></div></header>{nav}{notice_html}{body}<div class="footer">idontPG-backup · {VERSION} · durwinam</div></main><script>(function(){{const key="idontpg-theme";const root=document.body;const btn=document.getElementById("themeToggle");function apply(t){{root.classList.toggle("light",t==="light");if(btn){{btn.textContent=t==="light"?"🌙":"☀️";btn.title=t==="light"?"فعال‌سازی تم دارک":"فعال‌سازی تم لایت"}}}}let t="dark";try{{t=localStorage.getItem(key)||"dark"}}catch(e){{}}apply(t);if(btn)btn.addEventListener("click",function(){{t=root.classList.contains("light")?"dark":"light";apply(t);try{{localStorage.setItem(key,t)}}catch(e){{}}}});const meta=document.querySelector('meta[name="theme-color"]');if(meta){{const obs=new MutationObserver(function(){{meta.setAttribute("content",root.classList.contains("light")?"#fff1f8":"#06070d")}});obs.observe(root,{{attributes:true,attributeFilter:["class"]}});meta.setAttribute("content",root.classList.contains("light")?"#fff1f8":"#06070d")}}const menu=document.getElementById("menuToggle");const drawer=document.getElementById("drawer");const backdrop=document.getElementById("drawerBackdrop");const close=document.getElementById("drawerClose");function setMenu(open){{if(!drawer)return;drawer.classList.toggle("open",open);if(backdrop)backdrop.classList.toggle("open",open);if(menu)menu.classList.toggle("open",open);drawer.setAttribute("aria-hidden",open?"false":"true");document.body.style.overflow=open?"hidden":""}}if(menu)menu.addEventListener("click",function(){{setMenu(!drawer.classList.contains("open"))}});if(backdrop)backdrop.addEventListener("click",function(){{setMenu(false)}});if(close)close.addEventListener("click",function(){{setMenu(false)}});document.addEventListener("keydown",function(e){{if(e.key==="Escape")setMenu(false)}});if(drawer)drawer.querySelectorAll("a").forEach(function(a){{a.addEventListener("click",function(){{setMenu(false)}})}});const cd=document.getElementById("backupCountdown");if(cd){{let sec=parseInt(cd.textContent,10);if(Number.isFinite(sec)&&sec>=0){{const fmt=function(s){{s=Math.max(0,s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;return (d?d+" روز ":"")+String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(x).padStart(2,"0")}};cd.textContent=fmt(sec);setInterval(function(){{if(sec>0)sec--;cd.textContent=fmt(sec)}},1000)}}}}}})();</script></body></html>'''


def hidden_csrf(sid):
    return f'<input type="hidden" name="csrf" value="{html.escape(csrf_token(sid))}">'


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
        body = f'''<section class="login"><div class="glass"><div class="icon">🔒</div><h2 style="font-size:28px;margin:16px 0 8px">ورود به پنل</h2><p class="sub" style="font-size:13px;line-height:1.8">برای ورود، نام کاربری و رمز عبور ادمین را وارد کنید.</p>{notice}<form method="post" action="/login"><div class="field"><label>نام کاربری</label><input type="text" name="username" value="{username}" autocomplete="username" required></div><div class="field"><label>رمز عبور</label><input type="password" name="password" minlength="8" autocomplete="current-password" required></div><button class="btn primary full" type="submit">ورود امن ←</button></form></div></section>'''
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
            if sid: SESSIONS.pop(sid, None)
            self.send_response(302)
            self.send_header("Set-Cookie", "idontpg_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if not c.get("password_hash"):
            body = '''<section class="login"><div class="glass"><div class="icon">🚀</div><h2 style="font-size:28px;margin:16px 0 8px">راه‌اندازی اولیه</h2><p class="sub" style="font-size:13px;line-height:1.8">برای محافظت از پنل، نام کاربری ۵ تا ۳۲ کاراکتر و رمز حداقل ۸ کاراکتر، شامل حداقل ۲ حرف، ۱ عدد و یکی از # @ * بسازید.</p><form method="post" action="/setup"><div class="field"><label>نام کاربری</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" autocomplete="username" placeholder="admin" required></div><div class="field"><label>رمز ادمین</label><input type="password" name="password" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required></div><div class="field"><label>تکرار رمز</label><input type="password" name="password_confirm" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required></div><button class="btn primary full">ساخت حساب و ورود</button></form></div></section>'''
            self.send_html(page("First Run", body, False)); return
        if path == "/login":
            self.send_html(self.login_page()); return
        if not self.logged():
            self.redirect("/login"); return

        if path == "/api/resources":
            self.send_json(get_server_resource_usage()); return

        status = scheduler_status()
        if path == "/":
            token = c.get("token") or "تنظیم نشده"
            masked = (token[:8] + "••••••") if len(token) > 8 else token
            status_class = "on" if status == "active" else "off"
            panel_info = get_panel_info()
            backup_info = get_backup_info()
            backup_used = backup_info["size"]
            panel_used = get_panel_storage_usage()
            panel_status_class = "on" if panel_info["status"] == "Online" else "off"
            panel_url = html.escape(panel_info["url"])
            body = f'''<section class="hero"><h2>کنترل کامل <span class="gradient">Backup</span></h2><p>همه‌چیز برای مدیریت Backup، ارسال به Telegram و زمان‌بندی خودکار، داخل یک پنل شیشه‌ای و سریع.</p></section>
<div class="grid">
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("backup", "card-icon")}<div><h3 class="title">اطلاعات Backup</h3><p class="sub">Backup Information</p></div></div></div>
<div class="meta"><div class="meta-row"><span>{ui_icon("backup", "meta-icon")} تعداد Backup</span><strong>{backup_info["count"]}</strong></div><div class="meta-row"><span>{ui_icon("disk", "meta-icon")} حجم کل Backupها</span><strong>{html.escape(backup_info["size"])}</strong></div><div class="meta-row"><span>{ui_icon("clock", "meta-icon")} آخرین Backup</span><strong title="{html.escape(backup_info["latest"])}">{html.escape(backup_info["latest_time"])}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px;min-width:0"><span class="panel-brand-icon card-icon" aria-label="PasarGuard"><img src="/static/pasarguard-logo.png" alt="PasarGuard" onerror="this.style.display='none';this.parentElement.classList.add('logo-fallback')"><span class="logo-fallback-text">PG</span></span><div style="min-width:0"><h3 class="title">اطلاعات پنل</h3><p class="sub">Panel Information</p></div></div><span class="status-wrap">{status_badge('آنلاین' if panel_info['status'] == 'Online' else 'آفلاین', 'ok' if panel_info['status'] == 'Online' else 'bad')}</span></div>
<div class="meta"><div class="meta-row"><span>{ui_icon("link", "meta-icon")} لینک پنل</span><a href="{panel_url}" target="_blank" rel="noopener noreferrer" style="max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{panel_url}</a></div><div class="meta-row"><span>{status_dot("ok")} وضعیت پنل</span><strong>{html.escape(panel_info['status'])}</strong></div><div class="meta-row"><span>{ui_icon("traffic", "meta-icon")} مصرف کلی پنل</span><strong>{html.escape(panel_used)}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("telegram", "card-icon")}<div><h3 class="title">بکاپ تلگرام</h3><p class="sub">Telegram Backup</p></div></div><span class="status {status_class}">{status_badge('فعال' if status == 'active' else 'متوقف', 'ok' if status == 'active' else 'bad')}</span></div>
<div class="meta"><div class="meta-row"><span>Bot Token</span><span>{html.escape(masked)}</span></div><div class="meta-row"><span>Chat ID</span><span>{html.escape(c.get('chat') or 'تنظیم نشده')}</span></div><div class="meta-row"><span>Topic ID</span><span>{html.escape(c.get('topic') or '—')}</span></div><div class="meta-row"><span>بازه</span><span>{html.escape(str(c.get('interval','24')))} ساعت</span></div></div>
<div class="actions"><a class="btn primary" href="/telegram">{ui_icon("settings", "inline-icon")} تنظیمات Telegram</a><a class="btn" href="/test">{ui_icon("test", "inline-icon")} ارسال تست</a></div></article>
<article class="glass card backup-controls"><div class="card-head"><div style="display:flex;gap:12px;min-width:0">{ui_icon("settings", "card-icon")}<div style="min-width:0"><h3 class="title">تنظیمات Backup</h3><p class="sub">Backup Controls</p></div></div><span class="status {status_class}">{status_badge("فعال" if status == "active" else "متوقف", "ok" if status == "active" else "bad")}</span></div>
<p class="empty">زمان‌بندی را روشن/خاموش کنید، Backup دستی بگیرید یا مشخص کنید PG-Node هم همراه Backup ذخیره شود.</p><div class="actions backup-actions"><a class="btn primary" href="/backup-settings">مدیریت Backup</a><form method="post" action="/backup" style="display:flex;min-width:0">{hidden_csrf(self.sid())}<button class="btn good" type="submit">{ui_icon("backup", "inline-icon")} Backup دستی</button></form></div></article>
<article class="glass wide"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("test", "card-icon")}<div><h3 class="title">ارسال تست پیام به Telegram</h3><p class="sub">قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.</p></div></div></div><div class="actions"><a class="btn primary" href="/test">{ui_icon("test", "inline-icon")} ارسال پیام تست</a><span class="sub" style="align-self:center">با Chat ID و Topic فعلی ارسال می‌شود.</span></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("clock", "card-icon")}<div><h3 class="title">Backup بعدی</h3><p class="sub">Next Scheduled Backup</p></div></div><span class="status {status_class}">{status_badge('فعال' if status == 'active' else 'متوقف', 'ok' if status == 'active' else 'bad')}</span></div><div class="meta"><div class="meta-row"><span>{ui_icon("clock", "meta-icon")} زمان باقی‌مانده</span><strong id="backupCountdown">{html.escape(str(_next_backup_seconds(c.get('interval','24'), backup_info.get('latest_mtime')) if status == 'active' else '—'))}</strong></div><div class="meta-row"><span>{ui_icon("settings", "meta-icon")} وضعیت Scheduler</span><strong>{'فعال' if status == 'active' else 'متوقف'}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("activity", "card-icon")}<div><h3 class="title">آخرین فعالیت‌ها</h3><p class="sub">Recent Activity</p></div></div></div><div class="activity-list">{''.join(f'<div class="meta-row activity-{html.escape(str(a.get("kind","ok")))}"><span>{html.escape(str(a.get("message","")))}</span><strong>{html.escape(_relative_time(a.get("time")))}</strong></div>' for a in get_recent_activities()) or '<div class="empty">هنوز فعالیتی ثبت نشده.</div>'}</div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px">{ui_icon("health", "card-icon")}<div><h3 class="title">Health Check</h3><p class="sub">وضعیت سرویس‌ها</p></div></div></div><div class="health-list">{''.join(f'<div class="meta-row"><span>{status_dot("ok" if ok else "bad")} {html.escape(name)}</span><strong>{html.escape(detail)}</strong></div>' for name,ok,detail in get_health_info(c,panel_info))}</div></article>
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
            body = f'''<section class="hero"><h2>{ui_icon("account", "hero-icon")} <span class="gradient">حساب کاربری</span></h2><p>نام کاربری و رمز عبور ورود به Web Panel را تغییر دهید.</p></section><div class="glass wide"><form method="post" action="/account">{hidden_csrf(self.sid())}<div class="field"><label>نام کاربری فعلی</label><input value="{html.escape(canonical_username(c.get("username", "admin")))}" readonly><input type="hidden" name="username" value="{html.escape(canonical_username(c.get("username", "admin")))}"></div><div class="field"><label>رمز عبور فعلی</label><input type="password" name="current_password" autocomplete="current-password" required></div><div class="field"><label>نام کاربری جدید</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" value="{html.escape(canonical_username(c.get("username", "admin")))}" autocomplete="username" required><div class="hint">فقط حروف انگلیسی، عدد و خط تیره؛ ۵ تا ۳۲ کاراکتر.</div></div><div class="field"><label>رمز عبور جدید</label><input type="password" name="password" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required><div class="hint">حداقل ۸ کاراکتر، حداقل ۲ حرف انگلیسی، حداقل ۱ حرف بزرگ انگلیسی، ۱ عدد و ۱ کاراکتر ویژه مثل # @ *</div></div><div class="field"><label>تکرار رمز جدید</label><input type="password" name="password_confirm" minlength="8" autocomplete="new-password" pattern="(?=.*[A-Z])(?=.*[a-zA-Z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).{8,}" required></div><div class="actions"><button class="btn primary" type="submit">{ui_icon("settings", "inline-icon")} ذخیره تغییرات</button><a class="btn" href="/">← برگشت</a></div></form></div>'''
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
                SESSIONS[sid] = {"created": time.time(), "csrf": secrets.token_urlsafe(24)}
                self.send_response(302)
                self.send_header("Set-Cookie", "idontpg_session=" + sid + "; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self.send_html(self.login_page("نام کاربری یا رمز عبور اشتباه است."), 401)
            return

        if not self.logged():
            self.redirect("/login"); return
        if not self.require_csrf(data):
            self.send_html(page("Security", '<div class="glass"><div class="notice bad">درخواست نامعتبر یا منقضی شده است. صفحه را دوباره باز کنید.</div></div>'), 403); return

        if path == "/account":
            current = data.get("current_password", "")
            username = data.get("username", "").strip()
            pw = data.get("password", "")
            confirm = data.get("password_confirm", "")
            if not check_password(current, c):
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">رمز عبور فعلی صحیح نیست.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 401); return
            if not valid_username(username):
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">نام کاربری باید ۵ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد یا خط تیره باشد.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
            if not valid_password(pw):
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">رمز جدید باید حداقل ۸ کاراکتر، شامل حداقل ۲ حرف انگلیسی، ۱ حرف بزرگ انگلیسی، ۱ عدد و ۱ کاراکتر ویژه باشد.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
            if pw != confirm:
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">تکرار رمز جدید با رمز عبور یکسان نیست.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
            salt, digest = hash_password(pw)
            c.update({"username": username, "password_salt": salt, "password_hash": digest})
            save_cfg(c)
            SESSIONS.clear()
            self.send_html(page("Account", '<div class="glass"><div class="notice ok">اطلاعات ورود با موفقیت تغییر کرد. برای ورود مجدد به صفحه Login بروید.</div><a class="btn primary" href="/login">ورود مجدد</a></div>', False)); return

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
