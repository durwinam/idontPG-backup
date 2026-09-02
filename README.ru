[🇮🇷 README اصلی (فارسی)](README.md) | [🇬🇧 README English](README.en) | [🇷🇺 README Русский](README.ru)

# 🛡️ idontPG-backup

<div align="center">

### Продвинутый комплекс резервного копирования и миграции для PasarGuard

Основан на pg_backup  
**Backup · Restore · Migration · Telegram · Multi-Database · Docker**

Полноценный инструмент для управления резервным копированием, восстановлением и переносом инфраструктуры  
**PasarGuard** и **PG-Node** между серверами.

<div align="center">
  <img src="idontPG-img.png" alt="idontPG-backup Logo" width="300">
</div>

<br>

<div align="center">
  <img src="github-preview.png" alt="idontPG-backup Preview" width="900">
</div>

</div>

---

<div align="center">

[![Version](https://img.shields.io/badge/version-v5.6.4-7c3aed?style=for-the-badge)](https://github.com/durwinam/idontPG-backup)
[![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Supported-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](https://github.com/durwinam/idontPG-backup)

</div>

---

🌐 Веб-панель

🖥️ Профессиональное управление и мониторинг резервных копий

idontPG-backup включает лёгкую и профессиональную веб-панель для управления и мониторинга резервных копий.

Панель разработана с акцентом на скорость, простоту и отображение важной информации о системе и не требует тяжёлых Framework. ✨

🚀 Панель полностью **Dependency-Free** и для работы не требует Flask, Node.js или других дополнительных Framework.

⚙️ Панель устанавливается и управляется как сервис **systemd** и по умолчанию доступна на порту "5000". 🔐

Возможности панели

- 🌐 Управление Backup через браузер
- 📊 Отображение загрузки CPU
- 🧠 Отображение использования RAM
- 💾 Отображение использования Disk
- 🕒 Отображение 3 последних действий
- 📤 Ручная отправка Backup
- 🤖 Отправка тестового сообщения Telegram-боту
- 📦 Управление сохранёнными Backup
- 🔎 Отображение состояния операции Backup
  
```bash
Web panel_URL:http://IP-SERVER:5000
```

🛠 Установка и запуск

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/durwinam/idontPG-backup/main/install.sh)"
```

После установки:

```bash
idontPG-backup
```

Для обновления:

```bash
idont-backup update
```

---

✦ О проекте

idontPG-backup — это независимый и лёгкий Backup Suite для окружений
PasarGuard и PG-Node, созданный для упрощения процессов Backup, Restore
и Migration.

Проект стремится управлять всеми необходимыми этапами переноса данных без зависимости от сторонних панелей или тяжёлых Framework.

От определения типа базы данных до создания архива, проверки целостности Backup,
отправки в Telegram и восстановления на целевом сервере.

Подходит для

- 🖥️ Production-серверов
- 🗄️ Установок PasarGuard
- 🌐 Серверов с PG-Node
- 🔄 Переноса сервиса на новый VPS
- ☁️ Хранения Backup вне сервера
- 🤖 Автоматического Backup в Telegram
- 🐳 Docker-окружений

# ⚡ Основные возможности

| Возможность | Описание |
|---|---|
| 🐳 Docker Environment | Поддержка и управление в Docker-окружении |
| 🤖 Automatic Telegram Backup | Автоматическое создание и отправка Backup в Telegram |
| 🗄️ Backup Storage | Хранение и управление Backup |
| 🖥️ Panel & Node Backup | Backup панели PasarGuard и PG-Node |
| 📦 Full Backup | Создание полного Backup |
| 🧩 Large File Splitting | Автоматическое разделение больших файлов |
| ⏱️ Backup Scheduler | Запуск Backup в различное время и по заданным интервалам |
| 🔎 Auto Backend Detection | Автоматическое определение Backend |
| 🔐 Sensitive Data Protection | Защита конфиденциальной информации |
| 🌐 Web Management | Управление Backup через браузер |
| 💻 CLI Management | Полное управление Backup через Terminal |
| 📊 System Resource Monitor | Отображение использования CPU, RAM и Disk |
| 🕒 Recent Activities | Отображение 3 последних действий |
| 📤 Manual Backup | Ручная отправка Backup |
| 🤖 Telegram Bot Test | Отправка тестового сообщения Telegram-боту |
| ♻️ Restore | Web Panel & PasarGuard Restore |
| 🚚 Migration | Доступно в v5.8.0 |
| 🔄 Transfer | Доступно в v5.8.0 |

# 🗄️ Поддержка движков баз данных

Перед началом операции система автоматически определяет установленный Backend на PasarGuard.

| Database Engine | Возможности |
|---|---|
| 🟢 **SQLite** | • Прямой Backup файла базы данных<br>• Restore с проверенным целевым путём<br>• Защита от несанкционированной перезаписи путей |
| 🔵 **PostgreSQL** | • `pg_dump`<br>• `pg_dumpall`<br>• Backup всех Database<br>• Backup Global-данных<br>• Отдельный Restore каждой Database |
| 🟣 **TimescaleDB** | • Все возможности PostgreSQL<br>• Определение TimescaleDB<br>• Определение версии Extension<br>• Независимый Restore Database |
| 🟠 **MySQL / MariaDB** | • `mysqldump`<br>• Независимый Backup Database<br>• Автоматическое создание Database при Restore<br>• Определение подходящих Credential<br>• Автоматический Fallback при ошибке Authentication |


```bash
🧠 Автоматическое определение Backend

Вам не нужно вручную указывать тип базы данных.

Процесс определения выполняется следующим образом:

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

Доступно в v5.8.0

Следующие расширенные возможности будут добавлены в будущих версиях:

- ♻️ Restore Center
- 🩺 Run Diagnostics
- 🔐 Session Management
- 🔔 Notification Center
- 🕒 Backup Timeline

```

📡 Telegram

Для связи и поддержки:

Telegram: http://t.me/DuRnaziiAy

GitHub: https://github.com/durwinam

---

<div align="center">

🛡️ idontPG-backup

Лёгкий · Быстрый · Безопасный · Надёжный

</div>
