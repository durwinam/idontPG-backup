## v5.8.0 — Restore deferred
- Restore UI and Web Panel Restore endpoints are intentionally removed from this release.
- Backup, Telegram, Scheduler, monitoring, account and other existing features remain unchanged.
- Restore will return as a dedicated feature in the next update after the workflow is finalized.

## v5.8.0 — Password Policy Fix
- Fixed the setup password pattern to match the displayed policy: 8+ chars, 2 letters, 1 digit, and #/@/*.
- Removed the unintended uppercase-letter requirement.

## v5.8.0

### Web Panel
- Restore Center for Web Panel and PasarGuard.
- Backup `.env` and manifest validation before restore.
- Source/destination database selection with TimescaleDB recommended.
- Live restore logs and circular progress.
- Node traffic history preservation and keep-nodes-disabled options.
- Run Diagnostics, Security Center, Notification Center, Session Management, Backup Timeline and Command Center.

### Telegram
- Automatic Telegram backup-message cleanup settings.
- Five newest Telegram backups remain protected.
- Web Panel backup files are never removed by Telegram cleanup.

## v5.8.0

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
- CLI version updated to v5.8.0.
- Web Panel version updated to v5.8.0.

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
