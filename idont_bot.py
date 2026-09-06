#!/usr/bin/env python3
"""idontPG-backup Telegram Management Bot.

Backup-only management interface for authorized Telegram administrators.
Uses the Telegram Bot API over HTTPS and the native Bot API 10.x button styles.
Web Panel 2FA QR support uses the optional qrcode package installed by install.sh.
"""
import base64
import fcntl
import html
import importlib.util
import json
import os
import re
import shutil
import secrets
import hashlib
import hmac
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = "idontPG-backup"
VERSION = "5.8.9"
STATE_DIR = Path("/etc/idontPG-backup")
WEB_CONFIG = STATE_DIR / "web.json"
BOT_CONFIG = STATE_DIR / "telegram_bot.json"
LOCK_FILE = STATE_DIR / "telegram_bot_backup.lock"
CREDS_DIR = Path("/etc/pasarguard-backup")
CORE_PATH = Path("/usr/local/bin/idontPG-backup")
WEB_PATH = Path("/usr/local/bin/idontPG-backup-web.py")
LAUNCH_TOKEN_FILE = STATE_DIR / "miniapp_launch_tokens.json"
PENDING_INPUT_FILE = STATE_DIR / "telegram_bot_pending.json"


def load_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path, data, mode=0o600):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def issue_miniapp_token(uid):
    try:
        data=load_json(LAUNCH_TOKEN_FILE,{})
        now=time.time(); data={k:v for k,v in data.items() if isinstance(v,dict) and float(v.get("expires",0))>now}
        import secrets
        token=secrets.token_urlsafe(32); data[token]={"uid":int(uid),"expires":now+43200}; save_json(LAUNCH_TOKEN_FILE,data); return token
    except Exception: return ""

def totp_code(secret, for_time=None):
    import struct
    clean=str(secret or "").replace(" ","").upper(); raw=base64.b32decode(clean+"="*((8-len(clean)%8)%8),casefold=True); counter=int((time.time() if for_time is None else for_time)//30); digest=hmac.new(raw,struct.pack(">Q",counter),hashlib.sha1).digest(); off=digest[-1]&15
    return f"{((int.from_bytes(digest[off:off+4],'big')&0x7fffffff)%1000000):06d}"
def totp_valid(secret,code):
    code=str(code or "").strip().replace(" ","")
    return bool(re.fullmatch(r"\d{6}",code) and secret and any(hmac.compare_digest(totp_code(secret,time.time()+i*30),code) for i in (-1,0,1)))
def new_totp_secret(): return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
def recovery_codes(): return [secrets.token_hex(4).upper() for _ in range(8)]
def load_pending(): return load_json(PENDING_INPUT_FILE,{})
def save_pending(d): save_json(PENDING_INPUT_FILE,d)

def load_config():
    web = load_json(WEB_CONFIG, {})
    bot = load_json(BOT_CONFIG, {})
    token = str(bot.get("token") or web.get("token") or "").strip()
    # Web Panel-managed allowlist takes precedence and is capped at 3 admins.
    explicit = web.get("telegram_admin_ids")
    admin_ids = []
    if isinstance(explicit, list) and explicit:
        for value in explicit[:3]:
            s = str(value).strip()
            if s.lstrip("-").isdigit():
                admin_ids.append(int(s))
        admin_ids = sorted(set(admin_ids))[:3]
    else:
        for value in bot.get("admin_ids", []) if isinstance(bot.get("admin_ids", []), list) else []:
            s = str(value).strip()
            if s.lstrip("-").isdigit():
                admin_ids.append(int(s))
        # Legacy safe fallback for installations configured before the allowlist UI.
        chat = str(web.get("chat") or "").strip()
        if chat.lstrip("-").isdigit():
            admin_ids.append(int(chat))
        if CREDS_DIR.is_dir():
            for p in CREDS_DIR.glob("*.json"):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    cid = str(d.get("chat") or "").strip()
                    if cid.lstrip("-").isdigit():
                        admin_ids.append(int(cid))
                except Exception:
                    pass
        admin_ids = sorted(set(admin_ids))[:3]
    mini_url = str(bot.get("mini_app_url") or os.environ.get("IDONT_PG_WEB_URL") or "").strip().rstrip("/")
    if not mini_url:
        mini_url = str(web.get("web_url") or "").strip().rstrip("/")
    return {"token": token, "admin_ids": admin_ids, "mini_app_url": mini_url, "include_node": bool(bot.get("include_node", web.get("node", False)))}


def tg_call(token, method, payload=None, timeout=35):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    if not obj.get("ok"):
        raise RuntimeError(obj.get("description") or "Telegram API error")
    return obj.get("result")


def tg_json(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    raw = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json", "User-Agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=35) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    if not obj.get("ok"):
        raise RuntimeError(obj.get("description") or "Telegram API error")
    return obj.get("result")


def styled_button(text, style=None, **extra):
    d = {"text": text}
    if style in {"primary", "success", "danger"}:
        d["style"] = style
    d.update(extra)
    return d


def menu_keyboard(mini_url="", mini_token=""):
    rows = [
        [styled_button("💾 آخرین بکاپ", "primary"), styled_button("📊 فعالیت‌های اخیر", "success")],
        [styled_button("🖥 دستگاه‌های من", "primary"), styled_button("🗑 حذف خودکار", "danger")],
        [styled_button("🚀 بکاپ دستی", "success"), styled_button("📈 وضعیت سرور", "primary")],
        [styled_button("🔔 اعلان‌ها", "success"), styled_button("👤 تغییر اطلاعات", "primary", callback_data="account_info")],
        [styled_button("⚙️ تنظیمات", "primary")],
    ]
    if mini_url.startswith("https://"):
        sep="&" if "?" in mini_url else "?"
        launch=(sep+"launch_token="+urllib.parse.quote(mini_token,safe="")) if mini_token else ""
        rows.append([styled_button("🌐 باز کردن Mini App", "primary", web_app={"url": mini_url + "/miniapp" + launch})])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "یک گزینه را انتخاب کنید…"}


def send_message(token, chat_id, text, keyboard=True):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = json.dumps(menu_keyboard(load_config().get("mini_app_url", ""), issue_miniapp_token(chat_id)), ensure_ascii=False)
    try:
        return tg_json(token, "sendMessage", payload)
    except Exception:
        # Some older clients/API paths can reject new keyboard style fields.
        if keyboard:
            payload["reply_markup"] = json.dumps({"keyboard": [[{"text": b["text"]} for b in row] for row in menu_keyboard()["keyboard"]], "resize_keyboard": True})
        raise


def send_plain(token, chat_id, text):
    return tg_json(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


def human_size(n):
    try:
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
    except Exception:
        return "—"


def history_items():
    paths = [STATE_DIR / "backup_history.json", Path("/var/lib/idontPG-backup/backup_history.json")]
    out, seen = [], set()
    for p in paths:
        data = load_json(p, [])
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name and name not in seen:
                out.append(item); seen.add(name)
    out.sort(key=lambda x: float(x.get("mtime") or 0), reverse=True)
    return out


def backup_stats():
    items = history_items()
    # If history is unavailable, count known backup archives on disk.
    if not items:
        for root in (Path("/var/lib/pasarguard-backup"), Path("/opt/pasarguard/backup"), Path("/var/backups")):
            if root.is_dir():
                for p in root.glob("*.zip"):
                    try: items.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
                    except OSError: pass
        items.sort(key=lambda x: float(x.get("mtime") or 0), reverse=True)
    return items


def recent_activity():
    data = load_json(STATE_DIR / "audit_logs.json", [])
    return data[:8] if isinstance(data, list) else []


def login_sessions():
    data = load_json(STATE_DIR / "login_logs.json", [])
    return data[:8] if isinstance(data, list) else []


def fmt_time(ts):
    try: return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception: return "—"


def rel(ts):
    try:
        d = max(0, int(time.time() - float(ts)))
        if d < 60: return "همین الان"
        if d < 3600: return f"{d//60} دقیقه پیش"
        if d < 86400: return f"{d//3600} ساعت پیش"
        return f"{d//86400} روز پیش"
    except Exception: return "—"


def last_backup_text(items):
    if not items:
        return "هیچ Backup ثبت‌شده‌ای پیدا نشد."
    x = items[0]
    return (f"<b>💾 آخرین Backup</b>\n\n"
            f"📦 <code>{html.escape(str(x.get('name') or 'Backup'))}</code>\n"
            f"📏 حجم: <b>{human_size(x.get('size') or 0)}</b>\n"
            f"🕐 زمان: {html.escape(fmt_time(x.get('mtime') or x.get('time') or 0))}\n"
            f"✅ وضعیت: موفق")


def activity_text():
    rows = recent_activity()
    if not rows: return "<b>📊 فعالیت‌های اخیر</b>\n\nفعالیتی ثبت نشده است."
    lines = ["<b>📊 فعالیت‌های اخیر</b>", ""]
    for x in rows[:8]:
        kind = str(x.get("kind") or "ok")
        icon = "❌" if kind == "bad" else ("⚠️" if kind == "warn" else "✅")
        lines.append(f"{icon} {html.escape(str(x.get('message') or 'رویداد'))} · <i>{rel(x.get('time') or 0)}</i>")
    return "\n".join(lines)


def sessions_text():
    rows = login_sessions()
    if not rows: return "<b>🖥 دستگاه‌های من</b>\n\nهیچ نشست ثبت‌شده‌ای وجود ندارد."
    lines = ["<b>🖥 دستگاه‌های من</b>", "", "نشست‌های ثبت‌شده در Web Panel:"]
    for x in rows[:6]:
        lines.append(f"• {html.escape(str(x.get('device') or 'Unknown'))} · <code>{html.escape(str(x.get('ip') or 'unknown'))}</code> · {rel(x.get('time') or 0)}")
    lines.append("\n⚠️ Telegram Bot نمی‌تواند فهرست نشست‌های خود Telegram را بخواند؛ این بخش نشست‌های سیستم idontPG را نشان می‌دهد.")
    return "\n".join(lines)


def autod_delete_text():
    c = load_json(WEB_CONFIG, {})
    enabled = bool(c.get("telegram_auto_delete"))
    hours = float(c.get("telegram_auto_delete_hours") or 0)
    status = f"فعال · {hours:g} ساعت" if enabled else "خاموش"
    return f"<b>🗑 حذف خودکار Telegram</b>\n\nوضعیت: <b>{status}</b>\n🛡 پنج Backup جدید Telegram همیشه محافظت می‌شوند."


def server_text():
    try:
        load1, _, _ = os.getloadavg()
        cpu = f"Load {load1:.2f}"
    except Exception: cpu = "—"
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1); mem[k] = int(v.strip().split()[0])
        used = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
        ram = f"{used/1024/1024:.1f} / {mem.get('MemTotal',0)/1024/1024:.1f} GB"
    except Exception: ram = "—"
    try:
        d = shutil.disk_usage("/"); disk = f"{human_size(d.used)} / {human_size(d.total)}"
    except Exception: disk = "—"
    def svc(name):
        try: return "🟢 Running" if subprocess.run(["systemctl", "is-active", "--quiet", name], timeout=3).returncode == 0 else "🔴 Offline"
        except Exception: return "⚪ Unknown"
    return (f"<b>📈 وضعیت سرور</b>\n\n🧠 CPU: <b>{cpu}</b>\n💾 RAM: <b>{ram}</b>\n💿 Disk: <b>{disk}</b>\n\n"
            f"Web Panel: {svc('idontpg-backup-web.service')}\nScheduler: {svc('idontpg-backup-web-scheduler.service')}\n")


def settings_text():
    c = load_json(WEB_CONFIG, {})
    interval = c.get("interval", "24")
    node = "فعال" if c.get("node") else "خاموش"
    return (f"<b>⚙️ تنظیمات</b>\n\n⏱ Scheduler: <b>{html.escape(str(interval))}h</b>\n"
            f"🧩 PG-Node: <b>{node}</b>\n🤖 Telegram: {'متصل' if c.get('token') and c.get('chat') else 'تنظیم نشده'}\n"
            f"🔒 دسترسی Bot: <b>فقط Admin IDهای مجاز</b>\n🛡️ 2FA: <b>{'فعال' if c.get('two_factor_enabled') else 'خاموش'}</b>")


def notifications_text():
    rows = recent_activity()
    important = [x for x in rows if str(x.get("kind")) in {"bad", "warn"}]
    if not important: return "<b>🔔 اعلان‌ها</b>\n\n✅ اعلان مهمی وجود ندارد."
    return "<b>🔔 اعلان‌ها</b>\n\n" + "\n".join(f"⚠️ {html.escape(str(x.get('message') or 'رویداد'))}" for x in important[:8])


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if not spec or not spec.loader: raise RuntimeError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def manual_backup(token, chat_id, cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("یک Backup دیگر در حال اجراست.")
        web = load_web_module()
        if not hasattr(web, "make_backup"):
            raise RuntimeError("هسته Web Panel Backup در دسترس نیست.")
        ok, details = web.make_backup(send=True, audit_context={"username":f"telegram:{chat_id}"[:80],"ip":"telegram","device":"Telegram Bot"})
        if not ok:
            raise RuntimeError(f"Backup ساخته شد ولی ارسال Telegram ناموفق بود: {details}")
        items = web._load_backup_history() if hasattr(web, "_load_backup_history") else []
        return str(items[0].get("name")) if items else "Backup"


def load_web_module():
    candidates=[WEB_PATH,Path("/usr/local/share/idontPG-backup/web_panel.py"),Path("/opt/idontPG-backup/web_panel.py"),Path(__file__).resolve().with_name("web_panel.py")]
    last=None
    for p in candidates:
        if p.is_file():
            try: return load_module(p,"idontpg_web_bot")
            except Exception as e: last=e
    raise RuntimeError(f"هسته Web Panel Backup در دسترس نیست: {last or 'file not found'}")

def send_inline(token,chat_id,text,buttons):
    return tg_json(token,"sendMessage",{"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True,"reply_markup":json.dumps({"inline_keyboard":buttons},ensure_ascii=False)})

def resend_backup(token,chat_id,name):
    web=load_web_module(); c=web.load_cfg(); core=web.load_core(); target=next((p for p,_,_ in web._backup_archives() if p.name==name),None)
    if not target: raise RuntimeError("فایل Backup روی سرور پیدا نشد.")
    return web.send_archive(core,target,c,f"PasarGuard + PG-Node Auto Backup\nDate: {time.strftime('%Y-%m-%d %H:%M:%S')}\ndurwinam")

def start_message(user):
    first = html.escape(str(user.get("first_name") or "Admin"))
    return (f"<b>🛡️ {APP}</b>\n\nسلام <b>{first}</b> 👋\n"
            f"مرکز مدیریت Backup سرور آماده است.\n\n"
            f"🔐 دسترسی شما تأیید شد. از منوی پایین یک عملیات را انتخاب کنید.")


def denied_message():
    return (f"<b>⛔ دسترسی غیرمجاز</b>\n\n"
            f"این Bot فقط برای مدیران مجاز <b>{APP}</b> فعال است.\n"
            f"Telegram ID شما در لیست دسترسی نیست.")


def handle_callback(token,cfg,update):
    q=update.get("callback_query") or {}; user=q.get("from") or {}; uid=int(user.get("id") or 0); data=str(q.get("data") or ""); chat_id=int(((q.get("message") or {}).get("chat") or {}).get("id") or uid)
    if uid not in load_config()["admin_ids"]: return
    try: tg_call(token,"answerCallbackQuery",{"callback_query_id":q.get("id"),"text":"در حال اجرا…"})
    except Exception: pass
    if data.startswith("resend:"):
        try: ok,msg=resend_backup(token,chat_id,data.split(":",1)[1]); send_plain(token,chat_id,"✅ <b>Backup دوباره ارسال شد.</b>" if ok else "❌ <b>ارسال مجدد ناموفق بود.</b>\n"+html.escape(str(msg)))
        except Exception as e: send_plain(token,chat_id,"❌ <b>ارسال مجدد ناموفق بود</b>\n\n<code>"+html.escape(str(e))+"</code>")
    elif data=="scheduler":
        p=load_pending(); p[str(uid)]="scheduler"; save_pending(p); send_plain(token,chat_id,"⏱ مقدار Scheduler را بر حسب ساعت بفرست.\nمثال: <code>0.5</code> یا <code>1</code>")
    elif data=="node":
        try:
            web=load_web_module(); c=web.load_cfg(); c["node"]=not bool(c.get("node")); web.save_cfg(c); send_plain(token,chat_id,"🧩 PG-Node: <b>"+("فعال" if c["node"] else "غیرفعال")+"</b>")
        except Exception as e: send_plain(token,chat_id,"❌ تغییر PG-Node ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")

    elif data=="account_info":
        send_inline(token,chat_id,"<b>👤 مدیریت اطلاعات حساب</b>\n\nاز منوی زیر گزینه موردنظر را انتخاب کنید:",[[styled_button("👤 تغییر یوزرنیم","primary",callback_data="account_username")],[styled_button("🔐 تغییر رمز عبور","danger",callback_data="account_password")],[styled_button("🛡️ تأیید دو مرحله‌ای","success",callback_data="2fa_menu")],[styled_button("◀️ بازگشت","primary",callback_data="account_back")]])
    elif data=="2fa_menu":
        web=load_web_module(); c=web.load_cfg()
        if c.get("two_factor_enabled"): send_inline(token,chat_id,"<b>🛡️ تأیید دو مرحله‌ای</b>\n\nوضعیت: <b>فعال</b>",[[styled_button("❌ غیرفعال کردن","danger",callback_data="2fa_disable")],[styled_button("◀️ بازگشت","primary",callback_data="account_info")]])
        else: send_inline(token,chat_id,"<b>🛡️ تأیید دو مرحله‌ای</b>\n\n2FA اختیاری است و خاموش است.",[[styled_button("✅ فعال‌سازی 2FA","success",callback_data="2fa_start")],[styled_button("◀️ بازگشت","primary",callback_data="account_info")]])
    elif data=="2fa_start":
        try:
            web=load_web_module(); c=web.load_cfg(); secret=new_totp_secret(); c["two_factor_pending_secret"]=secret; c["two_factor_pending_created"]=time.time(); web.save_cfg(c)
            label=urllib.parse.quote(f"{web.APP}:{web.canonical_username(c.get('username','admin'))}",safe=""); issuer=urllib.parse.quote(web.APP); uri=f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
            p=load_pending(); p[str(uid)]={"action":"2fa_verify"}; save_pending(p)
            send_plain(token,chat_id,f"🛡️ <b>فعال‌سازی 2FA</b>\n\nSecret را در Authenticator وارد کن:\n<code>{secret}</code>\n\nحالا کد ۶ رقمی را همینجا ارسال کن.\n\n<code>{uri}</code>")
        except Exception as e: send_plain(token,chat_id,"❌ خطا: <code>"+html.escape(str(e))+"</code>")
    elif data=="2fa_disable":
        p=load_pending(); p[str(uid)]="2fa_disable"; save_pending(p); send_plain(token,chat_id,"🔐 رمز عبور فعلی را ارسال کن تا 2FA غیرفعال شود.")
    elif data=="account_back": send_message(token,chat_id,start_message(user))
    elif data=="account_username":
        p=load_pending(); p[str(uid)]="username"; save_pending(p); send_plain(token,chat_id,"👤 <b>تغییر یوزرنیم</b>\n\nیوزرنیم جدید را ارسال کن.")
    elif data=="account_password":
        p=load_pending(); p[str(uid)]="password_current"; save_pending(p); send_plain(token,chat_id,"🔐 <b>تغییر رمز عبور</b>\n\nابتدا رمز عبور فعلی را ارسال کن.")

def handle_update(token, cfg, update):
    # Reload the allowlist on every update so Web Panel changes apply without a bot restart.
    cfg = load_config()
    msg = update.get("message") or {}
    if not msg: return
    user = msg.get("from") or {}
    chat = msg.get("chat") or {}
    uid = int(user.get("id") or 0)
    chat_id = int(chat.get("id") or 0)
    if uid not in cfg["admin_ids"]:
        if msg.get("text", "").strip().lower().startswith("/start"):
            send_plain(token, chat_id, denied_message())
        return
    text = str(msg.get("text") or "").strip()
    pending=load_pending(); action=str(pending.get(str(uid)) or "")
    if action=="username" and text and not text.startswith("/"):
        if not re.fullmatch(r"[A-Za-z0-9-]{5,32}",text): send_plain(token,chat_id,"⚠️ یوزرنیم نامعتبر است."); return
        try:
            web=load_web_module(); c=web.load_cfg(); c["username"]=text; web.save_cfg(c); pending.pop(str(uid),None); save_pending(pending); send_plain(token,chat_id,"✅ یوزرنیم با موفقیت تغییر کرد.")
        except Exception as e: send_plain(token,chat_id,"❌ تغییر یوزرنیم ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")
        return
    if action=="password_current" and text and not text.startswith("/"):
        try:
            web=load_web_module(); c=web.load_cfg()
            if not web.check_password(text,c):
                send_plain(token,chat_id,"❌ رمز عبور فعلی اشتباه است."); return
            pending[str(uid)]="password_new"; save_pending(pending)
            send_plain(token,chat_id,"🔐 رمز فعلی تأیید شد.\n\nحالا <b>رمز عبور جدید</b> را ارسال کن.")
        except Exception as e: send_plain(token,chat_id,"❌ بررسی رمز ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")
        return
    if action=="password_new" and text and not text.startswith("/"):
        if not (len(text)>=8 and len(re.findall(r"[A-Za-z]",text))>=2 and re.search(r"[0-9]",text) and re.search(r"[^A-Za-z0-9]",text)):
            send_plain(token,chat_id,"⚠️ رمز باید حداقل ۸ کاراکتر، ۲ حرف، ۱ عدد و ۱ کاراکتر خاص داشته باشد."); return
        try:
            web=load_web_module(); c=web.load_cfg(); salt,digest=web.hash_password(text); c.update({"password_salt":salt,"password_hash":digest}); web.save_cfg(c)
            try: web._invalidate_user_sessions()
            except Exception: pass
            pending.pop(str(uid),None); save_pending(pending); send_plain(token,chat_id,"✅ رمز عبور با موفقیت تغییر کرد.\n\n🔒 نشست‌های قبلی Web Panel نیز باطل شدند.")
        except Exception as e: send_plain(token,chat_id,"❌ تغییر رمز ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")
        return
    if action=="2fa_verify" and text and not text.startswith("/"):
        try:
            web=load_web_module(); c=web.load_cfg(); secret=str(c.get("two_factor_pending_secret") or "")
            if not totp_valid(secret,text): send_plain(token,chat_id,"⚠️ کد 2FA اشتباه است."); return
            codes=recovery_codes(); c.update({"two_factor_enabled":True,"two_factor_secret":secret,"two_factor_pending_secret":"","two_factor_pending_created":0,"two_factor_recovery_codes":codes}); web.save_cfg(c); pending.pop(str(uid),None); save_pending(pending); send_plain(token,chat_id,"✅ <b>2FA فعال شد.</b>\n\nRecovery Codeها را در جای امن ذخیره کن:\n<code>"+"\n".join(codes)+"</code>")
        except Exception as e: send_plain(token,chat_id,"❌ فعال‌سازی 2FA ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")
        return
    if action=="2fa_disable" and text and not text.startswith("/"):
        try:
            web=load_web_module(); c=web.load_cfg()
            if not web.check_password(text,c): send_plain(token,chat_id,"❌ رمز عبور اشتباه است."); return
            c.update({"two_factor_enabled":False,"two_factor_secret":"","two_factor_pending_secret":"","two_factor_pending_created":0,"two_factor_recovery_codes":[]}); web.save_cfg(c); pending.pop(str(uid),None); save_pending(pending); send_plain(token,chat_id,"✅ 2FA غیرفعال شد.")
        except Exception as e: send_plain(token,chat_id,"❌ غیرفعال‌سازی ناموفق بود.\n<code>"+html.escape(str(e))+"</code>")
        return
    if action=="scheduler" and text and not text.startswith("/"):
        try: val=float(text); assert 0.5<=val<=720
        except Exception: send_plain(token,chat_id,"⚠️ مقدار Scheduler باید بین 0.5 تا 720 ساعت باشد. مثال: <code>0.5</code>"); return
        web=load_web_module(); c=web.load_cfg(); c["interval"]=str(val).rstrip("0").rstrip(".") if val%1 else str(int(val)); web.save_cfg(c)
        try: web.scheduler_service("restart")
        except Exception: pass
        pending.pop(str(uid),None); save_pending(pending); send_plain(token,chat_id,f"✅ Scheduler روی <b>{html.escape(c['interval'])}h</b> تنظیم شد."); return
    if text.startswith("/start") or text.startswith("/menu"):
        # If the admin was added from the Web Panel before starting the bot,
        # deliver the registration confirmation now that Telegram permits the bot
        # to message this user.
        if text.startswith("/start"):
            web_cfg = load_json(WEB_CONFIG, {})
            pending = []
            for value in (web_cfg.get("telegram_admin_welcome_pending") or []):
                try:
                    pending.append(int(str(value).strip()))
                except Exception:
                    pass
            if uid in pending:
                welcome = (
                    "👑 <b>تبریک!</b>\n\n"
                    "کاربر گرامی، شما با موفقیت به عنوان <b>ادمین idontPG-backup</b> ثبت شدید.\n\n"
                    "🛡️ اکنون دسترسی مدیریت Backup این سرور را از طریق ربات Telegram دارید."
                )
                try:
                    send_plain(token, chat_id, welcome)
                    web_cfg["telegram_admin_welcome_pending"] = [x for x in pending if x != uid]
                    save_json(WEB_CONFIG, web_cfg)
                except Exception:
                    pass
        send_message(token, chat_id, start_message(user)); return
    if text.startswith("/id"):
        send_plain(token, chat_id, f"Telegram ID: <code>{uid}</code>"); return
    if text == "💾 آخرین بکاپ":
        items=backup_stats(); msg=last_backup_text(items)
        if items: send_inline(token,chat_id,msg,[[styled_button("📨 ارسال مجدد به گروه","primary",callback_data="resend:"+str(items[0].get("name") or ""))]])
        else: send_plain(token,chat_id,msg)
        return
    if text == "📊 فعالیت‌های اخیر": send_plain(token, chat_id, activity_text()); return
    if text == "🖥 دستگاه‌های من": send_plain(token, chat_id, sessions_text()); return
    if text == "🗑 حذف خودکار": send_plain(token, chat_id, autod_delete_text()); return
    if text == "📈 وضعیت سرور": send_plain(token, chat_id, server_text()); return
    if text == "🔔 اعلان‌ها": send_plain(token, chat_id, notifications_text()); return
    if text == "⚙️ تنظیمات": send_inline(token,chat_id,settings_text(),[[styled_button("⏱ تغییر Scheduler","primary",callback_data="scheduler")],[styled_button("🧩 تغییر PG-Node","success",callback_data="node")],[styled_button("👤 تغییر اطلاعات","primary",callback_data="account_info")]]); return
    if text == "🚀 بکاپ دستی":
        send_plain(token, chat_id, "⏳ <b>در حال ساخت Backup…</b>\nلطفاً تا پایان عملیات صبر کن.")
        try:
            name = manual_backup(token, chat_id, cfg)
            send_plain(token, chat_id, f"✅ <b>Backup موفق بود</b>\n\n📦 <code>{html.escape(name)}</code>\n🚀 فایل به Telegram ارسال شد.")
        except Exception as e:
            send_plain(token, chat_id, f"❌ <b>Backup ناموفق بود</b>\n\n<code>{html.escape(str(e))}</code>")
        return
    if text == "🌐 باز کردن Mini App":
        if cfg.get("mini_app_url", "").startswith("https://"):
            send_plain(token, chat_id, "🌐 دکمه <b>باز کردن Mini App</b> را از منوی پایین بزن.")
        else:
            send_plain(token, chat_id, "⚠️ Mini App نیاز به URL امن HTTPS دارد.")
        return
    send_message(token, chat_id, "یک گزینه از منوی پایین انتخاب کن.")


def main():
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        print("Telegram Bot is not configured: missing token.")
        return 0
    if not cfg.get("admin_ids"):
        print("Telegram Bot is not configured: no admin Telegram IDs.")
        return 0
    offset = 0
    print(f"{APP} Telegram Management Bot v{VERSION} started. Admins: {cfg['admin_ids']}")
    while True:
        try:
            updates = tg_json(token, "getUpdates", {"timeout": 50, "offset": offset, "allowed_updates": ["message", "callback_query"]}) or []
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    if update.get("callback_query"): handle_callback(token,cfg,update)
                    else: handle_update(token,cfg,update)
                except Exception as e: print(f"update error: {e}", flush=True)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"poll error: {e}", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    raise SystemExit(main())
