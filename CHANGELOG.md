# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-07-27

### Changed
- **The whole project is now in English.** Every UI string, notification, log
  line, code comment, docstring, batch script and document was translated; the
  page is `lang="en"` and dates are shown as `YYYY-MM-DD` / `Mon D, YYYY`.
- The default collection is named **General**. Existing installations are
  migrated automatically on the next start.
- The setup script is now named **`install.bat`**.

### Security
- Cross-site subresource requests are now refused outright (`Sec-Fetch-Site`).
  Same-origin policy already hid the response bodies, but `/api/qr` still leaked
  how many links you had copied, when, and roughly how long each one was.
  Following an ordinary link to the UI from another page still works.
- The page-title fetch pins the address it validated. Previously the name was
  resolved once for the check and again for the connection, so whoever controlled
  the domain could answer the second lookup with an internal address (DNS
  rebinding) and have the app probe the local network.
- The title fetch now has a wall-clock deadline. A server trickling a few bytes
  at a time used to hold one of the three workers indefinitely.
- Notifications are rate limited, with a hard ceiling on how many can be on
  screen at once, and capture log lines are rate limited too. A page you have
  focused can write to the clipboard in a loop; that must not be able to blanket
  the desktop with always-on-top windows or grow the log without bound.
- Evicting the oldest item past the list limit no longer writes part of its text
  to the log — it never contains clipboard content now, as documented.
- `/api/items` is serialised outside the global lock, so requesting it in a loop
  can no longer stall clipboard capture.
- The `Host` allowlist no longer accepts the port-less spelling, a QR request for
  an over-long link answers 400 instead of 500, and collections read from disk
  are capped the same way the API caps them.

### Removed
- The Turkish README. English is the single documentation language.

## [1.0.0] — 2026-07-27

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
  moves its items to *General* rather than losing them.
- Page-title fetching for links, shown instead of the bare URL.
- Smart duplicates: re-copying bumps a ×N badge instead of adding a row, and revives
  completed items.
- Bulk selection (copy / archive / delete), "copy whole list", and `.txt` export.
- Pinning (survives *Clear*), QR codes for links, and copy-back without re-capture.
- Date grouping (Today / Yesterday / date) with a completion progress bar.

### Security
- CSRF protection on all state-changing endpoints (custom header + Origin allowlist).
- Copies flagged `ExcludeClipboardContentFromMonitorProcessing` by password managers
  are never recorded.

### Notes
- Windows-only. The active list is intentionally volatile — archive what you want to keep.
