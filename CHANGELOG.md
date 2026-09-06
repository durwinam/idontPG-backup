## v5.9.8
- Fixed Mini App successful-backup count to use a durable counter instead of the capped recent-history list.
- Replaced Mini App emoji glyphs with consistent inline SVG icons.
- Preserved the stable Mini App/Web Panel base and existing 3-recent-backup/resend flow.

## v5.9.7
- Restored Mini App base to the known-good v5.9.0 implementation.
- Fixed successful backup count so it stays based on persistent history.
- Mini App shows the 3 latest backups with resend action.

## v5.9.0

- Added per-Node traffic usage using the same PasarGuard Node counter source already used by the panel, with reset-safe accumulation.
- Mini App now exposes a live per-Node traffic breakdown and total usage.
- Node traffic details are cached briefly to keep Mini App loading fast.

# v5.8.9

- Fixed Mini App administrator authorization when Telegram admin IDs exist in the bot configuration.
- Improved unauthorized Mini App error messaging.

## v5.8.8

### Telegram Bot
- Added the visible `👤 تغییر اطلاعات` button to the main keyboard.
- Added account submenu for username, password, and optional 2FA management.
- Password changes now verify the current password first and invalidate Web Panel sessions.

### Telegram Mini App
- Rebuilt the Mini App UI with a faster, mobile-first glass interface.
- Added dedicated Home, Backup, Server, and Account screens.
- Added staged loading: a lightweight payload is rendered first, then full health/activity data loads in the background.
- Increased Mini App payload cache duration and reduced unnecessary initial filesystem/API work.
- Added account/2FA status to the Mini App.
- Bumped Mini App cache version to `v5.8.8` to prevent stale assets.

## v5.8.7

### Security & Account Management
- Added optional TOTP two-factor authentication for Web Panel and private Admin Panel login.
- Added QR-based 2FA setup, manual Secret/otpauth URI fallback, and one-time Recovery Codes.
- Added 2FA enable/disable controls from the Web Panel and Telegram Bot.
- Added Telegram Bot account menu for changing the Web Panel username and password.
- Password changes invalidate other active Web Panel sessions.

### Telegram Mini App
- Reduced first-load blocking: the splash screen no longer waits for the logo/network extras after API data is ready.
- Reduced API timeout and refresh frequency while adding a short server-side payload cache.
- Removed the external Google Fonts dependency from Mini App bootstrap to reduce startup latency.

## v5.8.2

- Version bump for the Telegram Control Center + HTTPS-fixed build.

## v5.8.1

### Backup & Web Panel
- Restore is intentionally deferred to the next dedicated release; this release is Backup-focused.
- Retained Telegram login notifications, PWA support, Telegram Auto Delete (0.5–48h), protection for the five newest Telegram Backup messages, and protection of Web Panel backup files from Telegram cleanup.
- Retained the three newest backups in Web Panel with Download, Delete and Telegram Resend actions.
- Improved Node usage/resource presentation.
- Added/retained Security Center, Diagnostics, Notification Center, Session Management with suspicious IP/device blocking and forced logout, five-backup Timeline, and Command Palette.
- Synchronized the language/translate control styling with the active theme.
- Hardened updater behavior: an empty GitHub Web Panel response no longer replaces or invalidates an installed Web Panel.


### New Features
- Private per-user login logs and last-login information.
- Telegram login notifications with source IP information.
- Temporary login alerts shown for 10 seconds.
- Progressive Web App support for `idontPG backup`.
- Seven additional browser-local themes.
- Dedicated Backup Health and server resource cards.
- Three most recent backups shown first with direct download/delete actions.
- HTTP-only installation mode without certificate requirements.

### Improvements
- CLI version updated to v5.8.1.
- Web Panel version updated to v5.8.1.

## v5.6.4

- Improved light-theme icon styling for better contrast and visual consistency.
- Adjusted SVG icon glow, borders, and resource icon colors for light glass mode.

## v5.6.4

- Fixed Web Panel HTML/CSS rendering so stylesheet content is not shown as page text.
- Fixed admin customization save flow.
- Kept admin controls isolated behind the private admin path/session.

## v5.6.1

- Unified project version across CLI, Web Panel, updater metadata and documentation.
- Keeps the existing PasarGuard Node traffic/statistics integration and admin-path functionality intact.
- Web Panel release identifier is now `5.6.1`.

## v5.5.4-node-traffic-fix3

- Reworked PasarGuard Node traffic collection against the current Node API contract.
- Uses the canonical `NodeService.GetStats` gRPC method with `UsersStat` and `reset=false`.
- Uses `Authorization: Bearer <node_api_key>` for current Node authentication and keeps `x-api-key` as compatibility fallback.
- Uses the Node service `port` (not `api_port`) for traffic statistics.
- REST `/stats/` protobuf transport remains as a fallback.
- Accepts newer/older node field shapes for address, port, API key, and CA certificate.
- Keeps the UI unchanged and accumulates Node counters across Xray/Panel resets.
- Records the last Node connection error internally instead of silently swallowing every failure.


### Russian Web Panel
- Added a complete Russian interface for the Web Panel, including login, dashboard, Telegram, Backup, account, audit logs, and admin pages.
- Added a working language selector with Persian, English, and Russian; the selected language is remembered per browser.
- Kept all existing Web Panel routes and HTTP-only behavior unchanged.

### Audit Log update
- Full panel audit trail: login, admin login, backups, Telegram, scheduler, account, delete, logout, language changes.
- Up to 200 audit events stored separately from the dashboard 3-item activity feed.

## v5.8.1 — Web Panel Access Modes
- Added Web Panel install mode selection: HTTP via server IP on port 5000, or HTTPS via domain.
- HTTPS setup validates domain DNS before certificate issuance.
- Automatically selects a free HTTPS port separate from 5000 (starting at 5443).
- Automatic Let's Encrypt certificate issuance via Certbot.
- Automatic daily certificate renewal with Web Panel restart after renewal.
- UFW-aware firewall opening for HTTP validation and the selected HTTPS port.
- Web Panel transport configuration is persisted in `/etc/default/idontpg-backup-web`.
- CLI now reports the configured HTTP/HTTPS Web Panel URL.
- Restore remains excluded from the user-facing backup workflow.

## v5.8.1 — Telegram Management Bot

- Added optional admin-only Telegram Management Bot.
- Added Telegram user-ID allowlist enforcement.
- Added colored Telegram button styles (`primary`, `success`, `danger`) with compatibility fallback.
- Added Manual Backup from Telegram.
- Added Latest Backup, Recent Activities, sessions, Auto Delete, Server Status, Notifications and Settings views.
- Added HTTPS Mini App launcher from the Telegram keyboard.
- Added updater support for `idont_bot.py` and safe service restart.
