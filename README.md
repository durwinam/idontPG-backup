# 🛡️ idontPG-backup

<div align="center">

### Professional Backup, Restore & Migration Utility for PasarGuard & PG-Node

Backup • Restore • Migration • Telegram Automation • All 5 Backends • Docker

</div>

---

<div dir="rtl" align="center">

[معرفی](#-معرفی) • [قابلیت‌ها](#-قابلیت‌ها)  • [نصب](#-نصب-و-اجرا) • [انتقال به سرور جدید](#-انتقال-به-سرور-جدید) • [بکاپ تلگرام](#-بکاپ-خودکار-تلگرام) • [امنیت](#-امنیت-security)

</div>

---

<div dir="rtl">

# 🚀 معرفی

**idontPG-backup** یک ابزار حرفه‌ای برای تهیه نسخه پشتیبان، ریستور و مهاجرت سرویس‌های **PasarGuard** و **PG-Node** است — با تشخیص خودکار بک‌اند، انتقال کامل بین سرورها، ارسال خودکار به تلگرام و بازیابی سریع.

* 📦 بکاپ PasarGuard تنها یا PasarGuard + PG-Node
* 🗄️ بکاپ و ریستور **همه‌ی دیتابیس‌های پاسارگارد** (Multi-Database)
* 🎯 **تشخیص خودکار بک‌اند** دیتابیس — بدون سؤال از کاربر
* 🚀 انتقال مستقیم و کامل به سرور جدید (Zero-Downtime Migration)
* 🤖 ارسال خودکار بکاپ به تلگرام + تقسیم خودکار فایل‌های حجیم (>50MB)
* ⏰ بکاپ زمان‌بندی‌شده با چند شِدیولر هم‌زمان (screen / tmux / systemd)
* 🐳 مدیریت خودکار Docker Stack + نصب خودکار وابستگی‌ها
* 🔐 انتقال امن از طریق SSH با رمز `getpass` (بدون echo)
* 🛡️ سخت‌گیری امنیتی کامل: بدون command injection، بدون Zip-Slip، بدون credential leak
* ✅ **بررسی سلامت آرشیو پیش از هر عملیات مخرب** (v5.4.0)
  — یک بکاپ ناقص هرگز باعث پاک‌شدن سرور مقصد یا نصب فعلی نمی‌شود
--

# 🗄️ بک‌اندهای دیتابیس پشتیبانی‌شده

نوع دیتابیس به‌صورت خودکار از `/opt/pasarguard/.env` و `docker-compose.yml` تشخیص داده می‌شود:

| بک‌اند | بکاپ | ریستور |
| --- | --- | --- |
| **SQLite** | کپی فایل `db.sqlite3` | کپی با مسیر مقصد validate‌شده |
| **PostgreSQL** | `pg_dump` + `pg_dumpall` (همه‌ی DBها) | `psql` (per-database) |
| **TimescaleDB** | مثل PostgreSQL + ثبت نسخه‌ی extension | `psql` (per-database) |
| **MySQL / MariaDB** | `mysqldump --databases` (خودکفا، شامل `CREATE DATABASE`) | `mysql` با fallback خودکار چند credential |

### فرآیند تشخیص

```text
خواندن .env و پارس SQLALCHEMY_DATABASE_URL
        │
        ▼
اسکن docker-compose.yml (تشخیص timescaledb از postgres، mariadb از mysql)
        │
        ▼
Validate نام سرویس Docker  →  انتخاب ابزار و کانتینر مناسب
```

---


# 🌐 Web Panel

The project also includes a modern dependency-free glass web panel that does not require Flask or Node.js. It is installed as a systemd service and listens on port **5000**.

```text
http://SERVER_IP:5000
```

On first launch, create an admin password. From the panel you can configure:

- Telegram Bot Token
- Telegram Chat ID
- Telegram Topic / Thread ID (`message_thread_id`)
- Optional Telegram proxy
- Backup interval
- Include PG-Node or not
- Start/stop the Telegram backup scheduler
- Run a manual backup and send it to Telegram
- Send a Telegram test message

The web panel stores its configuration under `/etc/idontPG-backup/web.json` with restrictive permissions.

> **Security:** the panel is protected by an admin password. If port 5000 is exposed to the public Internet, keep the password strong and preferably restrict port 5000 with your firewall or access it through a reverse proxy/VPN.

# 🛠 نصب و اجرا

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/durwinam/idontPG-backup/main/install.sh)"
```

لینک جایگزین (در صورت عدم دسترسی):

```bash
sudo bash -c "$(curl -sL https://raw.githack.com/durwinam/idontPG-backup/main/install.sh)"
```

پس از نصب:

```bash
idontPG-backup
```

---

# 🖥️ منوی اصلی

```text
1 ─ 🚀 Auto Backup & Transfer to New Server
2 ─ 🤖 Auto Backup to Telegram Bot (Scheduled)
3 ─ 💾 Manual Backup (Save Locally)
4 ─ 🔄 Manual Restore (From Local ZIP)
5 ─ 🧭 Manage Backup Schedulers
6 ─ ⬆️ Update to Latest Version
7 ─ 🚪 Exit
```

---

# 🚀 انتقال به سرور جدید

```text
Detect Backend Auto
        │
        ▼
Create Multi-DB Backup (tmp dir خصوصی 0700)
        │
        ▼
✅ Verify Archive Locally — manifest.tsv معتبر است؟ (v5.2.0)
   ✗ نامعتبر → توقف همین‌جا، هیچ سروری دست نمی‌خورد
        │
        ▼
Send To Telegram (اختیاری)
        │
        ▼
Connect To New Server (رمز با getpass)  →  Upload  →  Zip-Slip Validate
        │
        ▼
Detect Target Backend Auto
        │
        ▼
اگر MySQL/MariaDB: تشخیص و پاک‌سازی صحیح data-dir
(bind mount یا named volume) پیش از init مجدد کانتینر (v4.2.3)
        │
        ▼
Restore Data (Per-Backend, با retry روی خواندن manifest ریموت)
        │
        ▼
Start Services
```

### اطلاعات موردنیاز

```text
Server IP
Root Password  (فقط getpass، هرگز echo/ذخیره نمی‌شود)
Telegram Bot Token / Chat ID (اختیاری)
```

> ⚠️ اتصال SSH از `AutoAddPolicy` استفاده می‌کند (پذیرش خودکار کلید میزبان در اولین اتصال، با هشدار صریح). فقط از شبکه‌های قابل‌اعتماد اجرا کنید.

---

# 🤖 بکاپ خودکار تلگرام

بازه‌های پشتیبانی‌شده: `30m` `1h` `6h` `12h` `24h`

* فایل‌های >50MB به‌صورت خودکار به قطعات `.001` `.002` ... تقسیم می‌شوند و در Manual Restore به‌صورت خودکار شناسایی، verify و بازچسبانده می‌شوند.
* ماندگاری شِدیولر بعد از بستن SSH: `None` / `screen` / `tmux` / `systemd`، هرکدام با یک نام نمونه (Instance Name) اعتبارسنجی‌شده.
* توکن بات و Chat ID هرگز روی CLI یا unit فایل نیستند — در فایل `0600` جدا در `/etc/pasarguard-backup/<instance>.json` ذخیره می‌شوند.

از منوی «مدیریت شِدیولرها» (گزینه ۵) می‌توان هر شِدیولر را **Restart** (بدون حذف/بازسازی)، **Stop**، **Remove** یا توکن/Chat ID آن را **Update** کرد.

---

# 📁 ساختار فایل بکاپ

```text
backup_full_YYYYMMDDHHMMSS.zip
│
├── docker-compose.yml
├── .env
│
├── db_dump
│   ├── globals.sql        (فقط PostgreSQL / TimescaleDB)
│   ├── db-001.sql         (یا db.sqlite3 برای SQLite)
│   ├── db-002.sql         (Multi-DB)
│   └── manifest.tsv
│       # pg_backup_manifest  v4.2  format=tsv  db_type=timescaledb
│       pasarguard  pasarguard  1  db-001.sql  2.17.2
│       analytics   pasarguard  0  db-002.sql
│
├── pasarguard_data
├── pg_node_opt
└── pg_node_data
```

---

# 📂 مسیرهای پیش‌فرض

| سرویس | کانفیگ | داده |
| --- | --- | --- |
| PasarGuard | `/opt/pasarguard` | `/var/lib/pasarguard` |
| PG-Node | `/opt/pg-node` | `/var/lib/pg-node` |

---

# ⬆️ آپدیت به آخرین نسخه

از منوی اصلی (گزینه ۶):

```text
Download install.sh + install.sh.sha256   →   tmp dir خصوصی (0700)
        │
        ▼
مقایسه SHA256  →  عدم تطابق: توقف کامل
        │
        ▼
اجرای فایل ذخیره‌شده با sudo bash
```

اگر هش در دسترس نباشد، تأیید دستی کاربر برای ادامه‌ی بدون verification لازم است.

> 💡 بعد از آپدیت، شِدیولرهای در حال اجرا را از «مدیریت شِدیولرها» Restart کنید تا کد جدید فعال شود.

---

# 📜 تغییرات نسخه

آخرین تغییرات مهم:

* **v5.2.0** — رفع ریشه‌ای «manifest.tsv not found» در مهاجرت: آرشیو بکاپ پیش از هر عملیات مخرب به‌صورت محلی اعتبارسنجی می‌شود.
* **v4.2.3** — رفع `1045 Access denied` در ریستور MySQL/MariaDB با تشخیص و پاک‌سازی صحیح data-dir مقصد.
* **v4.2.2** — رفع خواندن manifest از سرور ریموت + انتظار آماده‌شدن دیتابیس بر اساس نوع بک‌اند.

تاریخچه‌ی کامل هر نسخه (از v4.0 تا امروز) در **[CHANGELOG.md](CHANGELOG.md)**.

---

# 🔐 امنیت (Security)

| حوزه | مشکل قبلی | راه‌حل |
| --- | --- | --- |
| Manual Restore | نام فایل ZIP بدون escape (Command Injection) | اعتبارسنجی سخت‌گیرانه‌ی نام فایل |
| استخراج آرشیو | `unzip -o` به Zip-Slip اجازه می‌داد | استخراج با `zipfile` + بررسی مسیر هر entry |
| ریستور SQLite | مسیر مقصد از بکاپ خوانده می‌شد (overwrite دلخواه) | `realpath` محدود به `/var/lib/pasarguard/` |
| manifest.tsv | نام فایل SQL بدون بررسی traversal | رد هر مقدار شامل `..`, `/`, `\` |
| توکن/چت‌آیدی تلگرام | plaintext در CLI/unit فایل (world-readable) | فایل `0600` جدا؛ خواندن فقط با `--instance` |
| رمز MySQL/MariaDB | `MYSQL_PWD` به کانتینر forward نمی‌شد | `docker compose exec -e MYSQL_PWD=...` |
| دایرکتوری موقت بکاپ | `/tmp` جهانی‌خواندنی | `tempfile.mkdtemp()` با `0700` |
| آرشیو نهایی | chmod بعد از ساخت (race) | `umask 0077` + chmod 600 بلافاصله |
| آپدیت خودکار | `curl \| sudo bash` خام | دانلود → SHA256 → اجرا |
| نام Instance/سرویس | بدون validation در `shell=True` | regex سخت‌گیرانه + `shlex.quote()` |
| MySQL data-dir مقصد | بین دو مهاجرت رمز عوض نمی‌شد → `1045` | تشخیص و پاک‌سازی bind mount/volume پیش از init |
| مهاجرت با manifest ناقص | کشف خطا فقط بعد از پاک‌شدن سرور مقصد | بررسی محلی آرشیو پیش از هر عملیات مخرب |

> ⚠️ **نکته‌ی باز**: اتصال SSH از `AutoAddPolicy` استفاده می‌کند (پذیرش خودکار کلید میزبان، با هشدار صریح) — ریسک تئوریک MITM روی شبکه‌های نامطمئن. توصیه: فقط از شبکه‌های قابل‌اعتماد اجرا کنید یا کلید میزبان را از قبل دستی verify کنید.

---

# ⚠️ نکات مهم

* اجرا نیازمند دسترسی Root است؛ هنگام ریستور سرویس‌ها موقتاً متوقف می‌شوند.
* اطلاعات SSH ذخیره نمی‌شوند؛ رمز با `getpass` گرفته می‌شود.
* فایل‌های بکاپ حاوی اطلاعات حساس‌اند و با `0600` ذخیره می‌شوند — در محل امن نگه دارید.
* رمزهای عبور در خروجی ابزار به‌صورت `****XXXX` نمایش داده می‌شوند.
* توکن بات و Chat ID همیشه در فایل `0600` جدا هستند، نه در CLI/unit فایل.

---

<p align="center">
<img src="https://raw.githubusercontent.com/durwinam/durwinam/main/sharingan.jpg" width="500" alt="Sharingan" />
</p>

---

# 📞 ارتباط با توسعه‌دهنده

* 👨‍💻 Telegram: https://t.me/DuRnaziiAy
* 🐙 GitHub: https://github.com/durwinam

---

### ❤️ حمایت از پروژه

اگر این پروژه برای شما مفید بوده است، با ثبت ⭐ در GitHub از توسعه آن حمایت کنید.

---

<sub><sub>Developed by durwinam</sub></sub>

</div>

## Direct CLI configuration

After installation, Telegram backup settings can be opened directly from the terminal:

```bash
idont-backup --set
```

The same configuration screen is also available with:

```bash
idontPG-backup --set
```
