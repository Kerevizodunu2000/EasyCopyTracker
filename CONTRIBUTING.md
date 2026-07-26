# Contributing to CopyTracker

Thanks for your interest! A few ground rules:

- **Bug reports / feature requests:** open an issue with steps to reproduce
  (Windows version, Python version, what you copied, what happened).
- **Security issues:** please do **not** open a public issue — contact the
  maintainer privately first.
- **Pull requests:** keep them focused. Run `python -m py_compile copytracker.py`
  before submitting. The project intentionally has minimal dependencies
  (`flask`, `qrcode`) — new runtime dependencies need a strong justification.
- **Never commit personal data:** `settings.json`, `archive.json`, logs and
  session snapshots are gitignored for a reason — they contain clipboard
  contents.
- UI language is Turkish; an i18n/English localization PR is very welcome.
