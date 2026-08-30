🛡️ idontPG-backup

<div align="center">Advanced Backup & Migration Suite for PasarGuard

برگرفته از pg_backup
Backup · Restore · Migration · Telegram · Multi-Database · Docker

یک ابزار کامل برای مدیریت بکاپ، بازیابی و انتقال زیرساخت‌های
PasarGuard و PG-Node بین سرورها.

<div align="center">
  <img src="idontPG-img.png" alt="idontPG-backup Logo" width="300">
</div><br><div align="center">
  <img src="github-preview.png" alt="idontPG-backup Preview" width="900">
</div><br>""Version" (https://img.shields.io/badge/version-v5.6.4-7c3aed?style=for-the-badge)" (https://github.com/durwinam/idontPG-backup)
""Python" (https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white)" (https://www.python.org/)
""Docker" (https://img.shields.io/badge/Docker-Supported-2496ED?style=for-the-badge&logo=docker&logoColor=white)" (https://www.docker.com/)
""License" (https://img.shields.io/badge/license-MIT-green?style=for-the-badge)" (https://github.com/durwinam/idontPG-backup)

</div>---

🌐 Web Panel

🖥️ Professional Backup Management & Monitoring

idontPG-backup دارای یک پنل وب سبک و حرفه‌ای برای مدیریت و مانیتورینگ Backupها است.

این پنل با تمرکز روی سرعت، سادگی و نمایش اطلاعات مهم سیستم طراحی شده و بدون نیاز به Frameworkهای سنگین قابل اجرا است. ✨

🚀 پنل کاملاً Dependency-Free طراحی شده و برای اجرا نیازی به Flask، Node.js یا سایر Frameworkهای اضافی ندارد.

⚙️ پنل به‌صورت یک سرویس systemd روی سیستم نصب و مدیریت می‌شود و به‌طور پیش‌فرض روی پورت "5000" در دسترس خواهد بود. 🔐

امکانات پنل

- 🌐 مدیریت Backup از طریق مرورگر
- 📊 نمایش میزان مصرف CPU
- 🧠 نمایش میزان مصرف RAM
- 💾 نمایش میزان مصرف Disk
- 🕒 نمایش ۳ فعالیت اخیر
- 📤 ارسال Backup به‌صورت دستی
- 🤖 ارسال پیام تست به ربات Telegram
- 📦 مدیریت Backupهای ذخیره‌شده
- 🔎 نمایش وضعیت عملیات Backup

Web panel_URL:http://IP-SERVER:5000

---

🛠 نصب و اجرا

sudo bash -c "$(curl -sL https://raw.githubusercontent.com/durwinam/idontPG-backup/main/install.sh)"

پس از نصب:

idontPG-backup

برای آپدیت:

idont-backup update

---

✦ درباره پروژه

idontPG-backup یک Backup Suite مستقل و سبک برای محیط‌های
PasarGuard و PG-Node است که با هدف ساده‌کردن فرآیندهای Backup، Restore
و Migration طراحی شده است.

این پروژه تلاش می‌کند بدون وابستگی به پنل‌های جانبی یا فریم‌ورک‌های
سنگین، تمام مراحل موردنیاز برای انتقال اطلاعات را مدیریت کند.

از تشخیص نوع دیتابیس گرفته تا ساخت آرشیو، بررسی سلامت بکاپ،
ارسال به Telegram و بازیابی روی سرور مقصد.

مناسب برای

- 🖥️ سرورهای Production
- 🗄️ نصب‌های PasarGuard
- 🌐 سرورهای دارای PG-Node
- 🔄 انتقال سرویس به VPS جدید
- ☁️ نگهداری Backup خارج از سرور
- 🤖 Backup خودکار Telegram
- 🐳 محیط‌های Docker

---

⚡ قابلیت‌های اصلی

قابلیت| توضیح
🐳 Docker Environment| پشتیبانی و مدیریت در محیط Docker
🤖 Automatic Telegram Backup| بکاپ خودکار و ارسال به Telegram
🗄️ Backup Storage| نگهداری و مدیریت Backupها
🖥️ Panel & Node Backup| بکاپ از PasarGuard Panel و PG-Node
📦 Full Backup| تهیه نسخه کامل از اطلاعات
🧩 Large File Splitting| تقسیم خودکار فایل‌های حجیم
⏱️ Backup Scheduler| اجرای Backup در زمان‌ها و بازه‌های مختلف
🔎 Auto Backend Detection| تشخیص خودکار Backend
🔐 Sensitive Data Protection| محافظت از اطلاعات حساس
🌐 Web Management| مدیریت Backup از طریق مرورگر
💻 CLI Management| مدیریت Backup از طریق Terminal
📊 System Resource Monitor| نمایش میزان مصرف CPU، RAM و Disk
🕒 Recent Activities| نمایش ۳ فعالیت اخیر
📤 Manual Backup| ارسال Backup به‌صورت دستی
🤖 Telegram Bot Test| ارسال پیام تست به ربات Telegram
♻️ Restore| به‌زودی
🚚 Migration| به‌زودی
🔄 Transfer| به‌زودی

---

🗄️ Database Engine Support

سیستم قبل از شروع عملیات، Backend نصب‌شده روی PasarGuard را
به‌صورت خودکار شناسایی می‌کند.

پشتیبانی فعلی:

SQLite

- Backup مستقیم فایل دیتابیس
- Restore با مسیر مقصد اعتبارسنجی‌شده
- محافظت در برابر overwrite مسیرهای غیرمجاز

PostgreSQL

- "pg_dump"
- "pg_dumpall"
- Backup تمام Databaseها
- Backup اطلاعات Global
- Restore جداگانه هر Database

TimescaleDB

تمام قابلیت‌های PostgreSQL به‌همراه:

- تشخیص TimescaleDB
- ثبت نسخه Extension
- Restore مستقل Databaseها

MySQL / MariaDB

- "mysqldump"
- Backup مستقل Databaseها
- ایجاد خودکار Database در Restore
- تشخیص Credential مناسب
- Fallback خودکار در شرایط خطای Authentication

---

🧠 تشخیص خودکار Backend

نیازی نیست نوع دیتابیس را دستی وارد کنید.

فرآیند تشخیص به شکل زیر انجام می‌شود:

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

---

🚧 Coming Soon

قابلیت‌های پیشرفته زیر در نسخه‌های آینده اضافه خواهند شد:

- ♻️ Restore
- 🚚 Migration
- 🔄 Transfer

---

📡 Telegram

برای ارتباط و پشتیبانی:

Telegram: http://t.me/DuRnaziiAy

GitHub: https://github.com/durwinam

---

<div align="center">🛡️ idontPG-backup

Lightweight · Fast · Secure · Reliable

</div>
