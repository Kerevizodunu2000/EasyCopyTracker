# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(Security tab → *Report a vulnerability*), or contact the maintainer directly via
the email on their GitHub profile.

Please include: what you did, what happened, and what an attacker gains. A proof of
concept helps a lot. Expect an initial response within a few days.

## Scope

CopyTracker runs entirely on the local machine and binds only to `127.0.0.1:8765`.
It has no authentication by design — the trust boundary is the local user account.

**In scope**

- Anything that lets a *remote web page* read, modify, or delete clipboard data
  through the local API (CSRF bypass, DNS rebinding, cross-origin read).
- XSS or script execution in the web UI — remember that captured clipboard content
  and fetched page titles are attacker-influenced input.
- Capturing content that must never be captured (e.g. bypassing the password-manager
  `ExcludeClipboardContentFromMonitorProcessing` exclusion).
- Writing files outside the application directory, or code execution via crafted
  `settings.json` / `archive.json`.

**Out of scope**

- Anything requiring an attacker who already has local code execution or the ability
  to read the user's files — that attacker already owns the clipboard.
- The absence of authentication on a loopback-only interface (by design).
- Clipboard data being readable by other processes on the same machine — that is how
  the Windows clipboard works.
- Denial of service by copying extremely large or numerous items locally.

## Handling your own data

`settings.json`, `archive.json`, `session_backup.json` and `copytracker.log` contain
clipboard contents. They are gitignored — never attach them to a public issue.
Redact before sharing.
