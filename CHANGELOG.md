## v5.5.3 — Account validation rules
- Username: 5–32 chars, English letters/numbers/hyphen only.
- Password: minimum 8 chars, at least 2 letters, 1 number, and one of `#@*`.

## v5.5.3
- Fixed Light theme background so the purple/pink/red animated background is visible instead of the dark background.
- Improved Light theme glass surfaces and mobile browser theme color.

## v5.5.1
- Fixed Telegram Forum Topic delivery from the Web Panel.
- Topic IDs are normalized and validated before sending.
- Telegram Topic links are accepted in addition to numeric message_thread_id values.

# Changelog

## v5.5.0
- Added persistent Dark/Light theme toggle to the Web Panel.
- Added animated Light theme with white, purple, pink, and red glass visuals.
- Synchronized buttons, cards, inputs, backgrounds, and focus states with the active theme.
- Improved responsive Web Panel styling and animated primary controls.
- Updated project version to v5.5.0.

## v5.4.2
- Updated CLI banner to `v5.4.2 - durwinam`.
- Added a green Web Panel access notice with the detected server IP and port 5000.
- Added `idont-backup --set` direct Telegram backup configuration command.
- Kept `idontPG-backup --set` as an alternate command.
- Installer now creates the `idont-backup` command alias.
- Installer version detection now reads the actual `VERSION` constant instead of stale historical header versions.

## 5.3.0
- Redesigned glassmorphism UI controls.
- Improved Persian font rendering with Vazirmatn and Tahoma fallback.
- Improved button sizing, spacing, hover/focus states, and mobile layout.
- Refined icons and action labels.

## v5.5.2
- Fixed mobile Web Panel responsive layout and horizontal overflow.
- Fixed long Panel URL/metadata rows overflowing on small screens.
- Fixed Panel Information card markup/alignment.
- Improved mobile grid sizing and card behavior.
