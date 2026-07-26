# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/).

## [3.0.0] — 2026-07-26

First public release.

### Added
- Application icon ("Gece"): three stacked bars in rose tones on a near-black
  ground, shipped as a multi-size `.ico` (16–256 px) plus an SVG source. Used for
  the tray, the window, the browser tab, the desktop shortcut and the READMEs.
- RAM-first storage: the active list never touches disk; only archived items are persisted.
- Archive (`archive.json`) with automatic retention: 1 hour / 1 day / end of day /
  1 month (default) / forever.
- Crash recovery: a shadow snapshot offers to restore the list after an unclean shutdown.
- Capture on/off — UI switch, tray menu, and the `Ctrl+Alt+K` hotkey.
- System tray icon (open list / toggle capture / quit) and `Ctrl+Alt+L` to open the UI.
- Capture filters: everything, links only, Instagram only, custom domain list.
- View filters (links / Instagram / texts) and search, independent of capture filters.
- Collections: new copies are routed to the active collection; deleting a collection
  moves its items to *Genel* rather than losing them.
- Page-title fetching for links, shown instead of the bare URL.
- Smart duplicates: re-copying bumps a ×N badge instead of adding a row, and revives
  completed items.
- Bulk selection (copy / archive / delete), "copy whole list", and `.txt` export.
- Pinning (survives *Temizle*), QR codes for links, and copy-back without re-capture.
- Date grouping (Bugün / Dün / date) with a completion progress bar.

### Security
- CSRF protection on all state-changing endpoints (custom header + Origin allowlist).
- Copies flagged `ExcludeClipboardContentFromMonitorProcessing` by password managers
  are never recorded.

### Notes
- Windows-only. The active list is intentionally volatile — archive what you want to keep.
