# 🛡️ idontPG-backup

<div align="center">

### Advanced Backup & Migration Suite for PasarGuard

**Backup · Restore · Migration · Telegram · Multi-Database · Docker**

یک ابزار کامل برای مدیریت بکاپ، بازیابی و انتقال زیرساخت‌های  
**PasarGuard** و **PG-Node** بین سرورها.
<div align="center">
  <img src="idontPG-img.png" alt="idontPG-backup Logo" width="420">
</div>
<br>

[![Version](https://img.shields.io/badge/version-v5.4.2-7c3aed?style=for-the-badge)](https://github.com/durwinam/idontPG-backup)
[![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Supported-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](https://github.com/durwinam/idontPG-backup)

</div>

---


🌐WEB PANEL

🔹 پنل وب مدرن و حرفه‌ای با طراحی Glass

این پروژه علاوه بر قابلیت‌های اصلی، شامل یک پنل وب مدرن، سبک و حرفه‌ای با رابط کاربری Glass نیز می‌باشد. ✨

🚀 پنل کاملاً Dependency-Free طراحی شده و برای اجرا نیازی به Flask، Node.js یا سایر فریم‌ورک‌های اضافی ندارد.

⚙️ پنل به‌صورت یک سرویس systemd روی سیستم نصب و مدیریت می‌شود و به‌طور پیش‌فرض روی پورت 5000 در دسترس خواهد بود. 🔐

```bash
Web panel_URL:http://IP-SERVER:5000
```
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


برای آپدیت: 
```bash
idont-backup update
```


## ✦ درباره پروژه

**idontPG-backup** یک Backup Suite مستقل و سبک برای محیط‌های
PasarGuard و PG-Node است که با هدف ساده‌کردن فرآیندهای Backup، Restore
و Migration طراحی شده است.

این پروژه تلاش می‌کند بدون وابستگی به پنل‌های جانبی یا فریم‌ورک‌های
سنگین، تمام مراحل موردنیاز برای انتقال اطلاعات را مدیریت کند.

از تشخیص نوع دیتابیس گرفته تا ساخت آرشیو، بررسی سلامت بکاپ،
ارسال به Telegram و بازیابی روی سرور مقصد.

### مناسب برای

- 🖥️ سرورهای Production
- 🗄️ نصب‌های PasarGuard
- 🌐 سرورهای دارای PG-Node
- 🔄 انتقال سرویس به VPS جدید
- ☁️ نگهداری Backup خارج از سرور
- 🤖 Backup خودکار Telegram
- 🐳 محیط‌های Docker

---

# ⚡ قابلیت‌های اصلی

| قابلیت | توضیح |
|---|---|
| 📦 Full Backup | تهیه نسخه کامل از اطلاعات |
| ♻️ Restore | بازیابی مستقیم Backup |
| 🚚 Migration | انتقال مستقیم به سرور جدید |
| 🗃️ Multi-Database | پشتیبانی از چند دیتابیس |
| 🤖 Telegram | ارسال خودکار Backup |
| 🧩 Large Files | تقسیم خودکار فایل‌های حجیم |
| ⏱️ Scheduler | اجرای Backup در بازه‌های مختلف |
| 🐳 Docker | مدیریت خودکار Docker Stack |
| 🔎 Auto Detection | تشخیص خودکار Backend |
| 🔐 Secure Storage | محافظت از اطلاعات حساس |
| 🖥️ Web Panel | مدیریت Backup از طریق مرورگر |
| 💻 CLI | مدیریت کامل از طریق Terminal |

---

# 🗄️ Database Engine Support

سیستم قبل از شروع عملیات، Backend نصب‌شده روی PasarGuard را
به‌صورت خودکار شناسایی می‌کند.

پشتیبانی فعلی:

### SQLite

- Backup مستقیم فایل دیتابیس
- Restore با مسیر مقصد اعتبارسنجی‌شده
- محافظت در برابر overwrite مسیرهای غیرمجاز

### PostgreSQL

- `pg_dump`
- `pg_dumpall`
- Backup تمام Databaseها
- Backup اطلاعات Global
- Restore جداگانه هر Database

### TimescaleDB

تمام قابلیت‌های PostgreSQL به‌همراه:

- تشخیص TimescaleDB
- ثبت نسخه Extension
- Restore مستقل Databaseها

### MySQL / MariaDB

- `mysqldump`
- Backup مستقل Databaseها
- ایجاد خودکار Database در Restore
- تشخیص Credential مناسب
- Fallback خودکار در شرایط خطای Authentication

---

# 🧠 تشخیص خودکار Backend

نیازی نیست نوع دیتابیس را دستی وارد کنید.

فرآیند تشخیص به شکل زیر انجام می‌شود:

```text
PasarGuard
    │
    ├── /opt/pasarguard/.env
    │
    └── docker-compose.yml
            │
            ▼
    SQLAlchemy Database URL
            │
            ▼
    Backend Detection
            │
            ├── SQLite
            ├── PostgreSQL
            ├── TimescaleDB
            └── MySQL / MariaDB
            │
            ▼
    Docker Service Validation
            │
            ▼
    Backup Engine

telegram link:http://t.me/DuRnaziiAy
git:https://github.com/durwinam
