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
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = "idontPG-backup"
VERSION = "5.5.3"
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
    # Keep password rules intentionally simple: minimum 8 characters.
    # No character-class regex is used so browsers cannot reject valid passwords.
    return len(str(value or "")) >= 8


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
    archive = core.create_backup(bool(c.get("node", False)))
    if not send:
        return True, f"Backup ساخته شد: {archive}"
    return send_archive(core, archive, c, f"idontPG-backup · {time.strftime('%Y-%m-%d %H:%M:%S')}")


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
    """Find idontPG backup archives reliably, including systemd-created backups."""
    # pg_backup.py writes archives to os.getcwd(). Under systemd that is
    # normally /, but keep the common backup locations as fallbacks.
    roots = [
        Path.cwd(), SCRIPT.parent, Path("/root"), Path("/tmp"),
        Path("/opt/pasarguard"), Path("/var/backups"),
        Path("/var/lib/idontPG-backup"), Path("/usr/local/share/idontPG-backup"),
    ]
    seen = set()
    found = []

    def add_file(item):
        try:
            if not item.is_file() or item.name.startswith("."):
                return
            if not item.name.startswith("backup_") or item.suffix.lower() != ".zip":
                return
            st = item.stat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                return
            seen.add(key)
            found.append((item, st.st_size, st.st_mtime))
        except OSError:
            return

    for root in roots:
        try:
            if not root.is_dir():
                continue
            # Always check the root itself.
            for item in root.glob("backup_*.zip"):
                add_file(item)
            # Also cover backups placed one or two directories below a
            # service-specific directory. This avoids an expensive whole-
            # filesystem recursive scan while handling custom working dirs.
            if root != Path("/"):
                for item in root.glob("*/backup_*.zip"):
                    add_file(item)
                for item in root.glob("*/*/backup_*.zip"):
                    add_file(item)
        except OSError:
            continue

    found.sort(key=lambda x: x[2], reverse=True)
    return found

def get_backup_info():
    """Return useful information about retained backup archives."""
    archives = _backup_archives()
    total = sum(size for _, size, _ in archives)
    latest = archives[0] if archives else None
    latest_name = latest[0].name if latest else "هنوز Backup ساخته نشده"
    latest_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest[2])) if latest else "—"
    return {
        "count": len(archives),
        "size": _format_bytes(total),
        "latest": latest_name,
        "latest_time": latest_time,
    }


def get_backup_storage_usage():
    return get_backup_info()["size"]


def _read_panel_env():
    env_path = Path("/opt/pasarguard/.env")
    data = {}
    try:
        if not env_path.is_file():
            return data
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            data[key.strip()] = value
    except Exception:
        pass
    return data


def _sqlite_db_candidates(env):
    url = env.get("SQLALCHEMY_DATABASE_URL", "")
    candidates = [
        Path("/var/lib/pasarguard/db.sqlite3"),
        Path("/var/lib/pasarguard/db.sqlite"),
        Path("/opt/pasarguard/db.sqlite3"),
        Path("/opt/pasarguard/db.sqlite"),
    ]
    if url.startswith("sqlite") and "///" in url:
        raw = url.split("///", 1)[1].split("?", 1)[0]
        if raw:
            db_path = Path(raw)
            if not db_path.is_absolute():
                db_path = Path("/opt/pasarguard") / db_path
            candidates.insert(0, db_path)
    seen = set()
    return [p for p in candidates if not (str(p) in seen or seen.add(str(p)))]


def _panel_traffic_from_sqlite():
    """Read PasarGuard's panel-wide traffic counters from the system table.

    PasarGuard stores the global panel counters as system.uplink and
    system.downlink. Their sum is the total panel traffic shown by the panel.
    User-level sums are only a fallback for older schemas.
    """
    env = _read_panel_env()
    queries = [
        "SELECT COALESCE(SUM(uplink),0) + COALESCE(SUM(downlink),0) FROM system",
        "SELECT COALESCE((SELECT SUM(used_traffic) FROM users),0) + "
        "COALESCE((SELECT SUM(used_traffic_at_reset) FROM user_usage_logs),0)",
        "SELECT COALESCE(SUM(used_traffic),0) FROM users",
    ]
    for db in _sqlite_db_candidates(env):
        if not db.is_file():
            continue
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for query in queries:
                if query.endswith("FROM system") and "system" not in tables:
                    continue
                if "user_usage_logs" in query and "user_usage_logs" not in tables:
                    continue
                if "users" in query and "users" not in tables:
                    continue
                try:
                    row = con.execute(query).fetchone()
                    if row is not None and row[0] is not None:
                        return max(0, int(row[0]))
                except Exception:
                    continue
        except Exception:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    return None

def _panel_traffic_from_postgres():
    """Read PasarGuard's global traffic counters from its configured DB.

    PasarGuard can use PostgreSQL/TimescaleDB and the database service is not
    guaranteed to be named ``postgres``.  We therefore parse the exact
    SQLALCHEMY_DATABASE_URL first, then try the common compose DB services and
    finally any services reported by ``docker compose config --services``.
    """
    from urllib.parse import urlparse, unquote

    env = _read_panel_env()
    url = env.get("SQLALCHEMY_DATABASE_URL", "")
    user = env.get("DB_USER", "pasarguard")
    dbname = env.get("DB_NAME", "pasarguard")
    host = ""
    port = ""
    password = env.get("DB_PASSWORD", "")

    if url:
        try:
            parsed = urlparse(url)
            if parsed.username:
                user = unquote(parsed.username)
            if parsed.password:
                password = unquote(parsed.password)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = str(parsed.port)
            if parsed.path and parsed.path != "/":
                dbname = unquote(parsed.path.lstrip("/"))
        except Exception:
            pass

    queries = [
        "SELECT COALESCE(SUM(uplink),0) + COALESCE(SUM(downlink),0) FROM system;",
        "SELECT COALESCE((SELECT SUM(used_traffic) FROM users),0) + "
        "COALESCE((SELECT SUM(used_traffic_at_reset) FROM user_usage_logs),0);",
        "SELECT COALESCE(SUM(used_traffic),0) FROM users;",
    ]

    # First try a local psql connection when the configured DB points at the host.
    if shutil.which("psql") and host in ("", "localhost", "127.0.0.1", "::1"):
        for q in queries:
            envp = os.environ.copy()
            if password:
                envp["PGPASSWORD"] = password
            cmd = ["psql", "-h", host or "127.0.0.1", "-U", user, "-d", dbname, "-tA", "-c", q]
            if port:
                cmd[2:2] = ["-p", port]
            try:
                proc = subprocess.run(cmd, cwd="/opt/pasarguard", env=envp,
                                      capture_output=True, text=True, timeout=7)
                if proc.returncode == 0:
                    lines = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
                    if lines and lines[-1].lstrip("-").isdigit():
                        return max(0, int(lines[-1]))
            except Exception:
                pass

    services = []
    configured = os.environ.get("IDONTPG_PG_DB_SERVICE", "")
    if configured:
        services.append(configured)
    services += ["postgres", "timescaledb", "db", "database", "postgresql"]

    # Discover the actual database service from the PasarGuard compose file.
    try:
        proc = subprocess.run(
            ["docker", "compose", "config", "--services"],
            cwd="/opt/pasarguard", capture_output=True, text=True, timeout=7,
        )
        if proc.returncode == 0:
            services += [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    except Exception:
        pass

    seen = set()
    for svc in services:
        if svc in seen:
            continue
        seen.add(svc)
        for q in queries:
            commands = [
                ["docker", "compose", "exec", "-T", svc, "psql", "-U", user, "-d", dbname, "-tA", "-c", q],
                ["docker", "compose", "exec", "-T", svc, "psql", "-U", "pasarguard", "-d", "pasarguard", "-tA", "-c", q],
            ]
            for cmd in commands:
                try:
                    proc = subprocess.run(cmd, cwd="/opt/pasarguard",
                                          capture_output=True, text=True, timeout=7)
                    if proc.returncode == 0:
                        lines = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
                        if lines and lines[-1].lstrip("-").isdigit():
                            return max(0, int(lines[-1]))
                except Exception:
                    continue
    return None

def get_panel_storage_usage():
    """Return total lifetime traffic consumed by all PasarGuard users."""
    traffic = _panel_traffic_from_sqlite()
    if traffic is None:
        traffic = _panel_traffic_from_postgres()
    return _format_bytes(traffic) if traffic is not None else "قابل دریافت نیست"

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
.container{width:min(1180px,calc(100% - 34px));margin:0 auto;padding:28px 0 56px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:28px}.brand{display:flex;align-items:center;gap:14px}.brand-logo{width:58px;height:58px;object-fit:cover;border-radius:16px;border:1px solid rgba(255,255,255,.16);box-shadow:0 12px 40px rgba(34,211,238,.20),0 0 28px rgba(139,92,246,.16);transition:.25s ease}.brand-logo:hover{transform:translateY(-2px) scale(1.03);box-shadow:0 16px 50px rgba(34,211,238,.30),0 0 36px rgba(139,92,246,.24)}.brand h1{font-size:21px;margin:0}.brand p{margin:3px 0 0;color:var(--muted);font-size:13px}.pill{border:1px solid var(--line);background:rgba(255,255,255,.05);backdrop-filter:blur(18px);padding:9px 13px;border-radius:999px;color:var(--muted);font-size:12px}.top-actions{display:flex;align-items:center;gap:9px}.theme-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);cursor:pointer;font-size:18px;transition:.25s ease}.theme-toggle:hover{transform:translateY(-2px) rotate(4deg);border-color:rgba(236,72,153,.45);box-shadow:0 10px 30px rgba(236,72,153,.16)}.hero{margin-bottom:22px}.hero h2{font-size:clamp(30px,5vw,54px);line-height:1.02;margin:0 0 10px;letter-spacing:-1.8px}.gradient{background:linear-gradient(90deg,#fff,#c4b5fd,#67e8f9,#f9a8d4);-webkit-background-clip:text;background-clip:text;color:transparent}.hero p{color:var(--muted);max-width:720px;margin:0;line-height:1.7}.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px;min-width:0}.light .glass{background:linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.48))}.light .meta-row{background:rgba(255,255,255,.62)}.light .btn{background:rgba(255,255,255,.64);color:#17131f}.light .btn:hover{background:rgba(255,255,255,.86);color:#0f0b16}.light .toggle{background:rgba(255,255,255,.58);border-color:rgba(124,58,237,.18);color:#17131f}.light .notice{background:rgba(255,255,255,.62);color:#17131f}.light .gradient{background:linear-gradient(90deg,#17131f,#4c1d95,#9d174d,#991b1b);-webkit-background-clip:text;background-clip:text;color:transparent}.light .sub,.light .empty,.light .hint,.light .pill,.light .brand p,.light .footer,.light .meta-row span:first-child{color:#17131f}.light .status{color:#17131f}.light .status.on{color:#111827;background:rgba(52,211,153,.18)}.light .status.off{color:#17131f;background:rgba(124,58,237,.08)}.light .btn.good,.light .btn.danger{color:#111827}.light .field label,.light .field input,.light .field select{color:#17131f}.light .field input::placeholder,.light .field select::placeholder{color:#4b3f52}.light .field input,.light .field select{background:rgba(255,255,255,.64)}.light .btn.primary{color:#17131f;text-shadow:none}.light .notice.ok,.light .notice.bad{color:#17131f}.light .theme-toggle{color:#17131f}.light .icon{color:#17131f}.light .brand h1,.light .title{color:#17131f}.light .meta-row strong,.light .meta-row a,.light .meta-row span:last-child{color:#17131f}.hero h2{overflow-wrap:anywhere;word-break:break-word}.grid{overflow:visible}.glass{min-width:0;overflow:hidden;background:linear-gradient(145deg,var(--glass),rgba(255,255,255,.025));border:1px solid var(--line);box-shadow:var(--shadow);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-radius:24px;padding:22px}.card{grid-column:span 6;min-width:0;transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease}.card:hover{transform:translateY(-4px);border-color:rgba(236,72,153,.42);box-shadow:0 28px 90px rgba(0,0,0,.5)}.wide{grid-column:span 12}.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:18px}.icon{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.2);font-size:21px}.title{font-size:18px;font-weight:750;margin:0 0 4px}.sub{font-size:12px;color:var(--muted);margin:0}.status{font-size:11px;padding:7px 10px;border-radius:999px;border:1px solid var(--line)}.status.on{color:var(--good);background:rgba(52,211,153,.08)}.status.off{color:var(--muted)}.meta{display:grid;gap:10px;margin:18px 0}.meta-row{display:flex;justify-content:space-between;gap:12px;min-width:0;overflow:hidden;padding:11px 12px;border-radius:14px;background:var(--glass2);border:1px solid var(--line);transition:.2s}.meta-row:hover{transform:translateX(-2px);border-color:rgba(236,72,153,.28)}.meta-row span:first-child{color:var(--muted);font-size:12px}.meta-row span:last-child,.meta-row strong,.meta-row a{font-size:12px;max-width:65%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}.actions .btn{min-height:44px}.actions form{display:flex}.actions form .btn{height:100%}.btn{display:inline-flex;min-width:132px;max-width:100%;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);border-radius:14px;padding:12px 15px;color:var(--text);text-decoration:none;font-weight:700;font-size:13px;cursor:pointer;background:var(--glass2);transition:.22s;position:relative;overflow:hidden;white-space:normal;text-align:center}.btn:before{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 20%,rgba(255,255,255,.18),transparent 80%);transform:translateX(-120%);transition:.55s}.btn:hover:before{transform:translateX(120%)}.btn:hover{transform:translateY(-2px);background:rgba(255,255,255,.14);border-color:rgba(236,72,153,.28);box-shadow:0 10px 24px rgba(124,58,237,.16)}.btn:active{transform:translateY(0) scale(.98)}.btn.primary{border-color:transparent;background:linear-gradient(135deg,#7c3aed,#db2777,#ef4444);background-size:180% 180%;animation:buttonGlow 6s ease infinite;box-shadow:0 10px 28px rgba(219,39,119,.22)}@keyframes buttonGlow{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}.btn.good{border-color:rgba(52,211,153,.2);background:rgba(52,211,153,.09);color:#b7f7dc}.btn.danger{border-color:rgba(251,113,133,.2);background:rgba(251,113,133,.08);color:#fecdd3}.btn.full{width:100%}form{margin:0}.field{margin-bottom:16px}.field label{display:block;font-size:12px;color:var(--text);margin-bottom:7px}.field input,.field select{width:100%;border:1px solid var(--line);background:var(--glass2);color:var(--text);border-radius:14px;padding:13px 14px;outline:none;font-size:13px}.field input:focus,.field select:focus{border-color:rgba(103,232,249,.65);box-shadow:0 0 0 4px rgba(34,211,238,.08)}.hint{font-size:11px;color:var(--muted);margin-top:6px;line-height:1.6}.toggle{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px;border-radius:16px;background:rgba(0,0,0,.14);border:1px solid rgba(255,255,255,.07);margin-bottom:14px}.toggle input{accent-color:#db2777;width:18px;height:18px}.notice{margin-bottom:16px;padding:14px 16px;border-radius:16px;border:1px solid var(--line);background:rgba(255,255,255,.055);color:#dbeafe}.notice.ok{border-color:rgba(52,211,153,.25);background:rgba(52,211,153,.07)}.notice.bad{border-color:rgba(251,113,133,.25);background:rgba(251,113,133,.07)}.footer{text-align:center;color:#667085;font-size:11px;padding-top:28px}.login{width:min(460px,100%);margin:9vh auto}.login .glass{padding:30px}.empty{color:var(--muted);font-size:13px;line-height:1.7}.backup-controls .card-head{min-width:0}.backup-controls .empty{max-width:100%;overflow-wrap:anywhere}.backup-actions{width:100%;min-width:0}.backup-actions .btn{min-width:0;max-width:100%}.backup-actions form{min-width:0;max-width:100%}.backup-actions form .btn{min-width:0}@media(min-width:801px){.backup-actions .btn{min-width:0}.backup-actions form{flex:1}.backup-actions form .btn{width:100%}.backup-actions>a.btn{flex:1}}@media(max-width:800px){.top-actions{margin-right:auto}.actions .btn,.actions form{width:100%}.actions form .btn{width:100%}.card,.wide{grid-column:span 12}.topbar{align-items:flex-start}.pill{display:none}.container{width:calc(100% - 22px);max-width:1180px;padding-top:18px;min-width:0}.glass{border-radius:20px;padding:18px}.grid{grid-template-columns:minmax(0,1fr);width:100%;}.card,.wide{grid-column:1/-1;width:100%;}.topbar{flex-wrap:wrap;}.top-actions{margin-right:0;}.actions{min-width:0;}.meta-row a,.meta-row span:last-child,.meta-row strong{max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}.brand{min-width:0;}.brand>div{min-width:0;}}

/* v5.5.3 glass navigation drawer + mobile hero fix */
.menu-toggle{min-width:46px;width:46px;height:42px;padding:0;border:1px solid var(--line);border-radius:14px;color:var(--text);background:var(--glass2);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);cursor:pointer;font-size:21px;line-height:1;transition:.25s ease;display:inline-flex;align-items:center;justify-content:center}
.menu-toggle:hover{transform:translateY(-2px);border-color:rgba(236,72,153,.45);box-shadow:0 10px 30px rgba(124,58,237,.18)}
.menu-toggle .hamb{display:inline-block;transition:transform .3s ease}.menu-toggle.open .hamb{transform:rotate(90deg)}
.drawer-backdrop{position:fixed;inset:0;background:rgba(3,5,12,.46);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);opacity:0;visibility:hidden;transition:.28s ease;z-index:90}.drawer-backdrop.open{opacity:1;visibility:visible}
.drawer{position:fixed;top:14px;right:14px;bottom:14px;width:min(330px,calc(100vw - 28px));z-index:100;border:1px solid var(--line);border-radius:28px;background:linear-gradient(145deg,rgba(15,18,31,.88),rgba(34,19,55,.72));box-shadow:0 28px 100px rgba(0,0,0,.55),0 0 70px rgba(124,58,237,.16);backdrop-filter:blur(30px) saturate(145%);-webkit-backdrop-filter:blur(30px) saturate(145%);transform:translateX(calc(100% + 30px));transition:transform .34s cubic-bezier(.2,.8,.2,1);overflow:auto;padding:18px}.drawer.open{transform:translateX(0)}
.drawer-head{display:flex;align-items:center;gap:12px;padding:8px 6px 18px;border-bottom:1px solid var(--line);margin-bottom:14px}.drawer-logo{width:52px;height:52px;border-radius:15px;object-fit:cover;border:1px solid rgba(255,255,255,.14);box-shadow:0 10px 30px rgba(124,58,237,.18)}.drawer-head h3{margin:0;font-size:17px}.drawer-head p{margin:3px 0 0;color:var(--muted);font-size:11px}.drawer-close{margin-right:auto;width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--glass2);color:var(--text);cursor:pointer;font-size:18px}
.drawer-section{font-size:11px;color:var(--muted);padding:8px 8px 7px}.drawer-nav{display:grid;gap:7px}.drawer-link{display:flex;align-items:center;gap:12px;min-height:48px;padding:11px 13px;border-radius:15px;border:1px solid transparent;color:var(--text);text-decoration:none;background:rgba(255,255,255,.035);transition:.2s ease;position:relative;overflow:hidden}.drawer-link:before{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent,rgba(236,72,153,.10),rgba(124,58,237,.10),transparent);transform:translateX(-110%);transition:.5s ease}.drawer-link:hover:before{transform:translateX(110%)}.drawer-link:hover{transform:translateX(-2px);border-color:rgba(236,72,153,.25);background:rgba(255,255,255,.07);box-shadow:0 10px 30px rgba(124,58,237,.10)}.drawer-icon{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;background:rgba(139,92,246,.13);border:1px solid rgba(139,92,246,.18);font-size:17px;flex:0 0 auto}.drawer-link strong{font-size:13px}.drawer-link small{display:block;color:var(--muted);font-size:10px;margin-top:2px}.drawer-link.logout{border-color:rgba(251,113,133,.16);background:rgba(251,113,133,.055)}
body.light .drawer{background:linear-gradient(145deg,rgba(255,255,255,.86),rgba(252,236,255,.78));box-shadow:0 28px 90px rgba(124,58,237,.22),0 0 60px rgba(236,72,153,.10)}body.light .drawer-link{color:#17131f;background:rgba(255,255,255,.46)}body.light .drawer-link:hover{background:rgba(255,255,255,.72)}body.light .drawer-head h3,body.light .drawer-link strong{color:#17131f}body.light .drawer-close,body.light .menu-toggle{color:#17131f;background:rgba(255,255,255,.58)}body.light .drawer-section,body.light .drawer-head p,body.light .drawer-link small{color:#4b3f52}body.light .drawer-backdrop{background:rgba(70,25,75,.20)}
@media(max-width:800px){.hero{margin-top:8px;margin-bottom:18px}.hero h2{font-size:clamp(25px,8vw,34px);line-height:1.25;letter-spacing:-.8px;max-width:100%;overflow-wrap:anywhere;word-break:break-word}.hero p{font-size:12px;line-height:1.8}.top-actions{gap:7px}}

"""



def page(title, body, logged=True, notice="", kind="ok"):
    nav = "" if not logged else '''<div class="drawer-backdrop" id="drawerBackdrop"></div><aside class="drawer" id="drawer" aria-hidden="true"><div class="drawer-head"><img class="drawer-logo" src="/static/logo.png" alt="idontPG-backup"><div><h3>idontPG-backup</h3><p>Backup Control Center</p></div><button class="drawer-close" id="drawerClose" type="button" aria-label="بستن منو">×</button></div><div class="drawer-section">منوی اصلی</div><nav class="drawer-nav"><a class="drawer-link" href="/"><span class="drawer-icon">🏠</span><span><strong>داشبورد</strong><small>نمای کلی سیستم</small></span></a><a class="drawer-link" href="/telegram"><span class="drawer-icon">📨</span><span><strong>بکاپ تلگرام</strong><small>تنظیمات و ارسال</small></span></a><a class="drawer-link" href="/backup-settings"><span class="drawer-icon">🛠️</span><span><strong>تنظیمات بکاپ</strong><small>Scheduler و Backup</small></span></a><a class="drawer-link" href="/test"><span class="drawer-icon">🧪</span><span><strong>تست تلگرام</strong><small>بررسی اتصال</small></span></a><a class="drawer-link" href="/account"><span class="drawer-icon">👤</span><span><strong>حساب کاربری</strong><small>مدیریت ورود</small></span></a><a class="drawer-link logout" href="/logout"><span class="drawer-icon">🚪</span><span><strong>خروج</strong><small>پایان نشست</small></span></a></nav></aside>'''
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
</style></head><body><div class="aurora"><i class="orb o1"></i><i class="orb o2"></i><i class="orb o3"></i></div><main class="container"><header class="topbar"><div class="brand"><img class="brand-logo" src="/static/logo.png" alt="IDONTPG Backup"><div><h1>{APP}</h1><p>Backup Control Center · durwinam</p></div></div><div class="top-actions"><button class="menu-toggle" id="menuToggle" type="button" aria-label="باز کردن منو" title="منو"><span class="hamb">☰</span></button><button class="theme-toggle" id="themeToggle" type="button" aria-label="تغییر تم" title="تغییر تم">🌙</button><div class="pill">v{VERSION} · Secure Glass UI</div></div></header>{nav}{notice_html}{body}<div class="footer">idontPG-backup · {VERSION} · durwinam</div></main><script>(function(){{const key="idontpg-theme";const root=document.body;const btn=document.getElementById("themeToggle");function apply(t){{root.classList.toggle("light",t==="light");if(btn){{btn.textContent=t==="light"?"🌙":"☀️";btn.title=t==="light"?"فعال‌سازی تم دارک":"فعال‌سازی تم لایت"}}}}let t="dark";try{{t=localStorage.getItem(key)||"dark"}}catch(e){{}}apply(t);if(btn)btn.addEventListener("click",function(){{t=root.classList.contains("light")?"dark":"light";apply(t);try{{localStorage.setItem(key,t)}}catch(e){{}}}});const meta=document.querySelector('meta[name="theme-color"]');if(meta){{const obs=new MutationObserver(function(){{meta.setAttribute("content",root.classList.contains("light")?"#fff1f8":"#06070d")}});obs.observe(root,{{attributes:true,attributeFilter:["class"]}});meta.setAttribute("content",root.classList.contains("light")?"#fff1f8":"#06070d")}}const menu=document.getElementById("menuToggle");const drawer=document.getElementById("drawer");const backdrop=document.getElementById("drawerBackdrop");const close=document.getElementById("drawerClose");function setMenu(open){{if(!drawer)return;drawer.classList.toggle("open",open);if(backdrop)backdrop.classList.toggle("open",open);if(menu)menu.classList.toggle("open",open);drawer.setAttribute("aria-hidden",open?"false":"true");document.body.style.overflow=open?"hidden":""}}if(menu)menu.addEventListener("click",function(){{setMenu(!drawer.classList.contains("open"))}});if(backdrop)backdrop.addEventListener("click",function(){{setMenu(false)}});if(close)close.addEventListener("click",function(){{setMenu(false)}});document.addEventListener("keydown",function(e){{if(e.key==="Escape")setMenu(false)}});if(drawer)drawer.querySelectorAll("a").forEach(function(a){{a.addEventListener("click",function(){{setMenu(false)}})}});}})();</script></body></html>'''


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
        if path == "/logout":
            sid = self.sid()
            if sid: SESSIONS.pop(sid, None)
            self.send_response(302)
            self.send_header("Set-Cookie", "idontpg_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/login")
            self.end_headers()
            return
        if not c.get("password_hash"):
            body = '''<section class="login"><div class="glass"><div class="icon">🚀</div><h2 style="font-size:28px;margin:16px 0 8px">راه‌اندازی اولیه</h2><p class="sub" style="font-size:13px;line-height:1.8">برای محافظت از پنل، نام کاربری ۵ تا ۳۲ کاراکتر و رمز عبور حداقل ۸ کاراکتر باشد.</p><form method="post" action="/setup"><div class="field"><label>نام کاربری</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" autocomplete="username" placeholder="admin" required></div><div class="field"><label>رمز ادمین</label><input type="password" name="password" minlength="8" maxlength="128" autocomplete="new-password" required></div><div class="field"><label>تکرار رمز</label><input type="password" name="password_confirm" minlength="8" maxlength="128" autocomplete="new-password" required></div><button class="btn primary full">ساخت حساب و ورود</button></form></div></section>'''
            self.send_html(page("First Run", body, False)); return
        if path == "/login":
            self.send_html(self.login_page()); return
        if not self.logged():
            self.redirect("/login"); return

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
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px"><div class="icon">🛡️</div><div><h3 class="title">اطلاعات Backup</h3><p class="sub">Backup Information</p></div></div></div>
<div class="meta"><div class="meta-row"><span>📦 تعداد Backup</span><strong>{backup_info["count"]}</strong></div><div class="meta-row"><span>💾 حجم کل Backupها</span><strong>{html.escape(backup_info["size"])}</strong></div><div class="meta-row"><span>🕒 آخرین Backup</span><strong title="{html.escape(backup_info["latest"])}">{html.escape(backup_info["latest_time"])}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px;min-width:0"><div class="icon">🌐</div><div style="min-width:0"><h3 class="title">اطلاعات پنل</h3><p class="sub">Panel Information</p></div></div><span class="status {panel_status_class}">{'● آنلاین' if panel_info['status'] == 'Online' else '○ آفلاین'}</span></div>
<div class="meta"><div class="meta-row"><span>🔗 لینک پنل</span><a href="{panel_url}" target="_blank" rel="noopener noreferrer" style="max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{panel_url}</a></div><div class="meta-row"><span>🟢 وضعیت پنل</span><strong>{html.escape(panel_info['status'])}</strong></div><div class="meta-row"><span>💾 حجم استفاده‌شده</span><strong>{html.escape(panel_used)}</strong></div></div></article>
<article class="glass card"><div class="card-head"><div style="display:flex;gap:12px"><div class="icon">📨</div><div><h3 class="title">بکاپ تلگرام</h3><p class="sub">Telegram Backup</p></div></div><span class="status {status_class}">{'● فعال' if status == 'active' else '○ متوقف'}</span></div>
<div class="meta"><div class="meta-row"><span>Bot Token</span><span>{html.escape(masked)}</span></div><div class="meta-row"><span>Chat ID</span><span>{html.escape(c.get('chat') or 'تنظیم نشده')}</span></div><div class="meta-row"><span>Topic ID</span><span>{html.escape(c.get('topic') or '—')}</span></div><div class="meta-row"><span>بازه</span><span>{html.escape(str(c.get('interval','24')))} ساعت</span></div></div>
<div class="actions"><a class="btn primary" href="/telegram">⚙️ تنظیمات Telegram</a><a class="btn" href="/test">🧪 ارسال تست</a></div></article>
<article class="glass card backup-controls"><div class="card-head"><div style="display:flex;gap:12px;min-width:0"><div class="icon">⚙️</div><div style="min-width:0"><h3 class="title">تنظیمات Backup</h3><p class="sub">Backup Controls</p></div></div><span class="status {status_class}">{html.escape(status)}</span></div>
<p class="empty">زمان‌بندی را روشن/خاموش کنید، Backup دستی بگیرید یا مشخص کنید PG-Node هم همراه Backup ذخیره شود.</p><div class="actions backup-actions"><a class="btn primary" href="/backup-settings">مدیریت Backup</a><form method="post" action="/backup" style="display:flex;min-width:0">{hidden_csrf(self.sid())}<button class="btn good" type="submit">📦 Backup دستی</button></form></div></article>
<article class="glass wide"><div class="card-head"><div style="display:flex;gap:12px"><div class="icon">📡</div><div><h3 class="title">ارسال تست پیام به Telegram</h3><p class="sub">قبل از فعال‌کردن Scheduler اتصال را بررسی کنید.</p></div></div></div><div class="actions"><a class="btn primary" href="/test">🧪 ارسال پیام تست</a><span class="sub" style="align-self:center">با Chat ID و Topic فعلی ارسال می‌شود.</span></div></article>
</div>'''
            self.send_html(page("Dashboard", body)); return

        if path == "/account":
            body = f'''<section class="hero"><h2>🔐 <span class="gradient">حساب کاربری</span></h2><p>نام کاربری و رمز عبور ورود به Web Panel را تغییر دهید.</p></section><div class="glass wide"><form method="post" action="/account">{hidden_csrf(self.sid())}<div class="field"><label>نام کاربری فعلی</label><input value="{html.escape(canonical_username(c.get("username", "admin")))}" readonly><input type="hidden" name="username" value="{html.escape(canonical_username(c.get("username", "admin")))}"></div><div class="field"><label>رمز عبور فعلی</label><input type="password" name="current_password" autocomplete="current-password" required></div><div class="field"><label>نام کاربری جدید</label><input type="text" name="username" minlength="5" maxlength="32" pattern="[A-Za-z0-9-]+" value="{html.escape(canonical_username(c.get("username", "admin")))}" autocomplete="username" required><div class="hint">فقط حروف انگلیسی، عدد و خط تیره؛ ۵ تا ۳۲ کاراکتر.</div></div><div class="field"><label>رمز عبور جدید</label><input type="password" name="password" minlength="8" maxlength="128" autocomplete="new-password" required><div class="hint">حداقل ۸ کاراکتر.</div></div><div class="field"><label>تکرار رمز جدید</label><input type="password" name="password_confirm" minlength="8" maxlength="128" autocomplete="new-password" required></div><div class="actions"><button class="btn primary" type="submit">💾 ذخیره تغییرات</button><a class="btn" href="/">← برگشت</a></div></form></div>'''
            self.send_html(page("Account", body)); return

        if path == "/telegram":
            body = f'''<section class="hero"><h2>🤖 <span class="gradient">بکاپ تلگرام</span></h2><p>اطلاعات ربات، مقصد، Topic، پروکسی و زمان‌بندی را تنظیم کنید؛ سپس Scheduler را شروع کنید.</p></section><div class="glass wide"><form method="post" action="/telegram">{hidden_csrf(self.sid())}<div class="grid"><div class="field" style="grid-column:span 6"><label>Telegram Bot Token</label><input name="token" value="{html.escape(c.get('token',''))}" placeholder="123456:ABC..." required><div class="hint">توکن BotFather را وارد کنید.</div></div><div class="field" style="grid-column:span 6"><label>Chat ID</label><input name="chat" value="{html.escape(c.get('chat',''))}" placeholder="-1001234567890" required></div><div class="field" style="grid-column:span 6"><label>Topic / Thread ID</label><input name="topic" value="{html.escape(c.get('topic',''))}" placeholder="12345"><div class="hint">شماره Topic را وارد کنید؛ لینک Topic تلگرام هم قابل قبول است.</div></div><div class="field" style="grid-column:span 6"><label>Telegram Proxy</label><input name="proxy" value="{html.escape(c.get('proxy',''))}" placeholder="socks5://127.0.0.1:1080"><div class="hint">اختیاری. اگر Proxy ندارید خالی بگذارید.</div></div></div><div class="actions"><button class="btn primary" type="submit">💾 ذخیره تنظیمات</button><a class="btn" href="/test">✈️ تست اتصال</a><a class="btn" href="/">← برگشت</a></div></form></div>'''
            self.send_html(page("Telegram Backup", body)); return

        if path == "/backup-settings":
            checked = "checked" if c.get("node") else ""
            body = f'''<section class="hero"><h2>⚙️ تنظیمات <span class="gradient">Backup</span></h2><p>کنترل Scheduler و اجرای Backup دستی.</p></section><div class="grid"><article class="glass card"><div class="card-head"><div style="display:flex;gap:12px"><div class="icon">⏳</div><div><h3 class="title">زمان‌بندی خودکار</h3><p class="sub">Scheduler</p></div></div><span class="status {'on' if status=='active' else 'off'}">{html.escape(status)}</span></div><form method="post" action="/backup-settings">{hidden_csrf(self.sid())}<div class="field"><label>بازه Backup (ساعت)</label><input name="interval" type="number" step="0.5" min="0.5" max="720" value="{html.escape(str(c.get('interval','24')))}" required></div><label class="toggle"><span>شامل PG-Node شود</span><input type="checkbox" name="node" {checked}></label><div class="actions"><button class="btn primary" type="submit">▶️ ذخیره و شروع</button><button class="btn danger" type="submit" formaction="/stop" formmethod="post">⏹️ توقف</button></div></form></article><article class="glass card"><div class="card-head"><div style="display:flex;gap:12px"><div class="icon">📦</div><div><h3 class="title">Backup دستی</h3><p class="sub">Manual Backup</p></div></div></div><p class="empty">همین حالا یک Backup کامل بگیرید و طبق تنظیمات Telegram برای مقصد فعلی ارسال کنید.</p><form method="post" action="/backup">{hidden_csrf(self.sid())}<button class="btn good full">🚀 شروع Backup دستی</button></form></article></div>'''
            self.send_html(page("Backup Settings", body)); return

        if path == "/test":
            body = f'''<section class="hero"><h2>✈️ تست <span class="gradient">Telegram</span></h2><p>یک پیام آزمایشی با تنظیمات فعلی ارسال می‌شود.</p></section><div class="glass wide"><div class="meta"><div class="meta-row"><span>Chat ID</span><span>{html.escape(c.get('chat') or 'تنظیم نشده')}</span></div><div class="meta-row"><span>Topic ID</span><span>{html.escape(c.get('topic') or '—')}</span></div><div class="meta-row"><span>Proxy</span><span>{html.escape(c.get('proxy') or 'بدون Proxy')}</span></div></div><form method="post" action="/test">{hidden_csrf(self.sid())}<div class="actions"><button class="btn primary">🧪 ارسال پیام تست</button><a class="btn" href="/">← برگشت</a></div></form></div>'''
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
                self.send_html(page("Setup", '<section class="login"><div class="glass"><div class="notice bad">رمز عبور باید حداقل ۸ کاراکتر باشد.</div><a class="btn" href="/">تلاش دوباره</a></div></section>', False)); return
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
                self.send_html(page("Account", '<div class="glass"><div class="notice bad">رمز جدید باید حداقل ۸ کاراکتر باشد.</div><a class="btn" href="/account">تلاش دوباره</a></div>'), 400); return
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
            body = f'<div class="glass"><div class="notice {kind}">{"✅ پیام تست با موفقیت ارسال شد." if ok else "❌ ارسال پیام تست ناموفق بود."}<br><span class="empty">{html.escape(str(msg))}</span></div><div class="actions"><a class="btn" href="/test">تلاش دوباره</a><a class="btn" href="/">داشبورد</a></div></div>'
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
            self.redirect("/backup-settings"); return

        if path == "/backup":
            try:
                ok, msg = make_backup(send=True)
            except Exception as e:
                ok, msg = False, str(e)
            body = f'<div class="glass"><div class="notice {"ok" if ok else "bad"}">{("✅ Backup با موفقیت ساخته و ارسال شد." if ok else "❌ Backup ناموفق بود.")}<br><span class="empty">{html.escape(str(msg))}</span></div><div class="actions"><a class="btn" href="/backup-settings">تنظیمات Backup</a><a class="btn" href="/">داشبورد</a></div></div>'
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
