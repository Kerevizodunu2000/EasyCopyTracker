"""
Easy Copy Tracker — clipboard inbox / link triage tool.

Architecture:
  - The active list is held in RAM (volatile; gone once the app exits).
  - The archive is written to disk (archive.json) and auto-purged per retention.
  - Settings + collections persist in settings.json.
  - A crash shadow (session_backup.json) is kept; removed on a clean shutdown.

Usage:
    python easycopytracker.py   run it in the console
    start.bat                   start in the background + open the list
    stop.bat / tray -> Quit     stop it

Shortcuts: Ctrl+Alt+K toggle capture - Ctrl+Alt+L open the list
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.request as urllib_request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Personal data lives under %LOCALAPPDATA%\\EasyCopyTracker.

    An application folder (e.g. C:\\) can inherit an ACL that grants other local
    accounts read/write access, while a directory under the user profile is by
    default open only to that user. It also removes any risk of accidentally
    committing personal data to the repository.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "EasyCopyTracker")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            pass
    return BASE_DIR


DATA_DIR = _data_dir()
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
BACKUP_FILE = os.path.join(DATA_DIR, "session_backup.json")
RECOVERY_FILE = os.path.join(DATA_DIR, "recovery_pending.json")
LEGACY_FILE = os.path.join(BASE_DIR, "data.json")
LOG_FILE = os.path.join(DATA_DIR, "easycopytracker.log")
PID_FILE = os.path.join(DATA_DIR, "easycopytracker.pid")
ICON_FILE = os.path.join(BASE_DIR, "docs", "easycopytracker.ico")
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://localhost:{PORT}"
APP_NAME = "Easy Copy Tracker"
DEDUP_WINDOW = 1.5   # s — collapses repeated clipboard events for the same content
MAX_TEXT = 10000     # maximum number of characters stored per item
MAX_ITEMS = 2000     # max items in the active list (oldest unpinned one is dropped)
BACKUP_INTERVAL = 2.0  # s — how often the crash shadow may be written at most
MAX_COLLECTIONS = 100      # ceiling for collections, incl. ones read from disk
MAX_COLLECTION_NAME = 40   # characters
FILTER_MODES = ("all", "links", "instagram", "custom")
RETENTIONS = ("1h", "1d", "eod", "1m", "forever")
RETENTION_SECS = {"1h": 3600, "1d": 86400, "1m": 30 * 86400}

# Under pythonw (no console) stdout/stderr are None -> route them to the log file
_STDOUT_IS_LOG = sys.stdout is None
if sys.stdout is None:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from flask import Flask, Response, jsonify, request, send_file
except ImportError:
    print("Flask is not installed. Run:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import qrcode
    import qrcode.image.svg
    HAS_QR = True
except ImportError:
    HAS_QR = False


# -------------------------------------------------------------------- helpers

_rate_lock = threading.Lock()
_rate_state = {}


def rate_limited(key, limit, window):
    """True once `key` has already fired `limit` times within `window` seconds.

    A web page the user has focused may call navigator.clipboard.writeText() in
    a loop — Chromium grants that without a prompt. Anything that draws an
    always-on-top window or appends to the log therefore has to be bounded, or
    a single visited page can blanket the desktop and grow the log for ever.
    """
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_state.get(key, ()) if now - t < window]
        if len(hits) >= limit:
            _rate_state[key] = hits
            return True
        hits.append(now)
        _rate_state[key] = hits
        return False


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if not _STDOUT_IS_LOG:  # under pythonw stdout IS the log file — don't write twice
        try:
            print(line)
        except Exception:
            pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def short(text, n=100):
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------- state

_lock = threading.RLock()
_started = now_iso()          # session start (the active list belongs to this session)
_items = []                   # ACTIVE LIST — RAM ONLY
_next_id = 1
_recovery = None              # items waiting to be recovered after a crash

_settings = {
    "filter_mode": "all",
    "custom_domains": [],
    "capture_enabled": True,
    "notifications_enabled": True,
    "retention": "1m",
    "collections": [{"id": 1, "name": "General", "created_at": _started}],
    "active_collection": 1,
    "next_collection_id": 2,
}

_archive = {"next_aid": 1, "items": []}


class StorageError(Exception):
    """Writing to disk failed — the caller must roll back its RAM change."""


def save_settings():
    with _lock:
        try:
            _write_json(SETTINGS_FILE, _settings)
        except OSError as e:
            log(f"ERROR: could not write settings.json: {e}")
            raise StorageError(str(e)) from e


def save_archive():
    with _lock:
        try:
            _write_json(ARCHIVE_FILE, _archive)
        except OSError as e:
            log(f"ERROR: could not write archive.json: {e}")
            raise StorageError(str(e)) from e


_backup_dirty = threading.Event()


def save_backup():
    """Marks the shadow copy dirty; the actual write is batched in backup_thread.

    Writing the whole list on every copy cost O(n^2); instead it is written at
    most once every BACKUP_INTERVAL seconds (a crash loses at most that much).
    """
    _backup_dirty.set()


def _flush_backup():
    with _lock:
        snapshot = {"items": list(_items), "next_id": _next_id, "saved_at": now_iso()}
    try:
        _write_json(BACKUP_FILE, snapshot)
    except OSError as e:
        log(f"Could not write the crash shadow: {e}")


def backup_thread():
    while True:
        _backup_dirty.wait()
        _backup_dirty.clear()
        _flush_backup()
        time.sleep(BACKUP_INTERVAL)  # rate-limit the writes


def delete_backup():
    try:
        os.remove(BACKUP_FILE)
    except OSError:
        pass


def load_settings():
    global _settings
    d = _read_json(SETTINGS_FILE)
    if not isinstance(d, dict):
        return False
    cols = [c for c in d.get("collections", []) if isinstance(c, dict)
            and isinstance(c.get("id"), int) and isinstance(c.get("name"), str)]
    cols = cols[:MAX_COLLECTIONS]          # the API caps these; a hand-edited file might not
    for c in cols:
        c["name"] = c["name"][:MAX_COLLECTION_NAME]
        if c["id"] == 1 and c["name"] == "Genel":
            c["name"] = "General"          # earlier versions shipped a Turkish default name
    if not any(c["id"] == 1 for c in cols):
        cols.insert(0, {"id": 1, "name": "General", "created_at": now_iso()})
    max_cid = max(c["id"] for c in cols)
    next_cid = d.get("next_collection_id")
    if not isinstance(next_cid, int) or next_cid <= max_cid:
        next_cid = max_cid + 1
    active = d.get("active_collection")
    if active not in {c["id"] for c in cols}:
        active = 1
    _settings = {
        "filter_mode": d.get("filter_mode") if d.get("filter_mode") in FILTER_MODES else "all",
        "custom_domains": [s for s in d.get("custom_domains", []) if isinstance(s, str)][:50],
        "capture_enabled": bool(d.get("capture_enabled", True)),
        "notifications_enabled": bool(d.get("notifications_enabled", True)),
        "retention": d.get("retention") if d.get("retention") in RETENTIONS else "1m",
        "collections": cols,
        "active_collection": active,
        "next_collection_id": next_cid,
    }
    return True


def _sanitize_entry(e, is_archive):
    """Normalises an entry read from disk; returns None when it is malformed.

    Files on disk may have been tampered with (by another local process), and
    `url` is used directly as an <a href> in the UI, so every stored URL is put
    through the as_web_link check again.
    """
    if not isinstance(e, dict) or not isinstance(e.get("text"), str):
        return None
    out = dict(e)
    url = out.get("url")
    out["url"] = as_web_link(url) if isinstance(url, str) else None
    out["is_link"] = out["url"] is not None
    title = out.get("title")
    out["title"] = title[:200] if isinstance(title, str) else None
    for key in ("copied_at", "archived_at", "checked_at"):
        if key in out and not isinstance(out[key], str):
            out[key] = None
    out["checked"] = bool(out.get("checked"))
    if is_archive:
        aid = out.get("aid")
        out["aid"] = aid if isinstance(aid, int) and aid > 0 else 0
        out["archived_at"] = out.get("archived_at") or now_iso()
        name = out.get("collection_name")
        if not isinstance(name, str) or name == "Genel":
            name = "General"  # archived by an earlier build that shipped Turkish names
        out["collection_name"] = name
    else:
        iid = out.get("id")
        out["id"] = iid if isinstance(iid, int) else 0
        out["pinned"] = bool(out.get("pinned"))
        copies = out.get("copies")
        out["copies"] = copies if isinstance(copies, int) and copies > 0 else 1
        cid = out.get("collection")
        out["collection"] = cid if isinstance(cid, int) else 1
        out["copied_at"] = out.get("copied_at") or now_iso()
    return out


def load_archive():
    global _archive
    d = _read_json(ARCHIVE_FILE)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return
    items = [x for x in (_sanitize_entry(e, True) for e in d["items"]) if x]
    max_aid = max((e["aid"] for e in items), default=0)
    for e in items:
        if not e["aid"]:
            max_aid += 1
            e["aid"] = max_aid
    next_aid = d.get("next_aid")
    if not isinstance(next_aid, int) or next_aid <= max_aid:
        next_aid = max_aid + 1
    _archive = {"next_aid": next_aid, "items": items}


def migrate_from_old_name():
    """Moves data over when the app was previously installed as "CopyTracker"."""
    base = os.environ.get("LOCALAPPDATA")
    if not base or DATA_DIR == BASE_DIR:
        return
    old = os.path.join(base, "CopyTracker")
    if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(DATA_DIR):
        return
    moved = 0
    for name in os.listdir(old):
        src = os.path.join(old, name)
        # log/pid files carry on under the new name
        dst = os.path.join(DATA_DIR, "easy" + name if name.startswith("copytracker.") else name)
        if os.path.exists(dst):
            continue
        try:
            os.replace(src, dst)
            moved += 1
        except OSError as e:
            log(f"Could not move {name}: {e}")
    if moved:
        log(f"Moved {moved} file(s) out of the old CopyTracker folder -> {DATA_DIR}")
    try:
        os.rmdir(old)
    except OSError:
        pass


def migrate_data_dir():
    """Moves data left in the app folder by older versions into %LOCALAPPDATA%."""
    if DATA_DIR == BASE_DIR:
        return
    for name in ("settings.json", "archive.json", "session_backup.json",
                 "recovery_pending.json", "easycopytracker.log"):
        old = os.path.join(BASE_DIR, name)
        new = os.path.join(DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                os.replace(old, new)
                log(f"Moved {name} -> {DATA_DIR}")
            except OSError as e:
                log(f"Could not move {name}: {e}")


def migrate_legacy():
    """On first run, moves the old data.json content into the archive intact."""
    if os.path.exists(SETTINGS_FILE) or not os.path.exists(LEGACY_FILE):
        return
    d = _read_json(LEGACY_FILE)
    if not isinstance(d, dict):
        return
    cols = {c.get("id"): c.get("name", "General") for c in d.get("collections", [])
            if isinstance(c, dict)}
    old_cols = [c for c in d.get("collections", []) if isinstance(c, dict)
                and isinstance(c.get("id"), int) and isinstance(c.get("name"), str)]
    if old_cols:
        _settings["collections"] = old_cols
        if not any(c["id"] == 1 for c in old_cols):
            _settings["collections"].insert(
                0, {"id": 1, "name": "General", "created_at": now_iso()})
        _settings["next_collection_id"] = max(c["id"] for c in _settings["collections"]) + 1
    if d.get("filter_mode") in FILTER_MODES:
        _settings["filter_mode"] = d["filter_mode"]
    moved = 0
    for it in d.get("items", []):
        if not isinstance(it, dict) or "text" not in it:
            continue
        _archive["items"].append({
            "aid": _archive["next_aid"],
            "text": it.get("text", ""),
            "url": it.get("url"),
            "is_link": bool(it.get("is_link")),
            "title": None,
            "copied_at": it.get("copied_at"),
            "checked": bool(it.get("checked")),
            "archived_at": now_iso(),
            "collection_name": cols.get(it.get("collection"), "General"),
        })
        _archive["next_aid"] += 1
        moved += 1
    try:
        os.replace(LEGACY_FILE, LEGACY_FILE + ".v2.bak")
    except OSError:
        pass
    if moved:
        save_archive()  # persist right away — anything left in RAM would be lost
        log(f"Moved {moved} item(s) from the old data.json into the archive "
            "(backed up as data.json.v2.bak).")


def check_recovery():
    """Holds the shadow copy as a recovery candidate when the last session crashed.

    If a second crash happens before the user decides about a pending recovery,
    the two files are MERGED (never overwritten) so nothing is lost.
    """
    global _recovery
    pending = _read_json(RECOVERY_FILE) if os.path.exists(RECOVERY_FILE) else None
    fresh = _read_json(BACKUP_FILE) if os.path.exists(BACKUP_FILE) else None

    merged = []
    for src in (pending, fresh):
        if isinstance(src, dict) and isinstance(src.get("items"), list):
            merged.extend(x for x in (_sanitize_entry(e, False) for e in src["items"]) if x)
    seen, unique = set(), []
    for it in merged:  # if the same text is in both files, recover it once
        key = (it["text"], it.get("collection"))
        if key not in seen:
            seen.add(key)
            unique.append(it)

    if unique:
        _recovery = unique
        try:
            _write_json(RECOVERY_FILE, {"items": unique, "saved_at": now_iso()})
        except OSError as e:
            log(f"Could not write the recovery file: {e}")
        log(f"Found {len(unique)} recoverable item(s) from the previous session.")
    else:
        try:
            os.remove(RECOVERY_FILE)
        except OSError:
            pass
    try:
        os.remove(BACKUP_FILE)
    except OSError:
        pass


# -------------------------------------------------------------- links/filters

def _host_of(url):
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return ""
    # Browsers treat '\' and userinfo as authority terminators; do the same here
    netloc = netloc.split("\\")[0].rsplit("@", 1)[-1]
    return netloc.split(":")[0].lower()


def as_web_link(text):
    """Returns a normalised URL when the text is a real, complete website link."""
    t = text.strip()
    if not t or any(ch.isspace() for ch in t):
        return None
    candidate = t
    if t.lower().startswith("www.") and "." in t[4:]:
        candidate = "https://" + t
    try:
        p = urlparse(candidate)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    # Backslashes and userinfo make Python's and the browser's parsers disagree
    # about the host (evil.com\.instagram.com could fool the filter) — reject them.
    if "\\" in candidate or "@" in p.netloc:
        return None
    host = p.netloc.split(":")[0]
    if "." not in host or len(host) < 4:
        return None
    return candidate


def _norm_domain(s):
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def _host_matches(host, domain):
    return host == domain or host == "www." + domain or host.endswith("." + domain)


def passes_filter(url):
    """Should this copy be stored, given the active capture filter?"""
    mode = _settings["filter_mode"]
    if mode == "links":
        return url is not None
    if mode == "instagram":
        return url is not None and _host_matches(_host_of(url), "instagram.com")
    if mode == "custom":
        if url is None:
            return False
        host = _host_of(url)
        domains = [_norm_domain(d) for d in _settings["custom_domains"] if d.strip()]
        return any(_host_matches(host, d) for d in domains) if domains else False
    return True  # "all"


def collection_name(cid):
    with _lock:
        for c in _settings["collections"]:
            if c["id"] == cid:
                return c["name"]
    return "General"


# ------------------------------------------------------------------ title fetch

TITLE_MAX_BYTES = 131072
TITLE_SOCKET_TIMEOUT = 6      # s per socket operation
TITLE_TOTAL_TIMEOUT = 15      # s wall clock for the whole fetch, redirects included
TITLE_MAX_REDIRECTS = 3
TITLE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EasyCopyTracker/1.0"


def _public_address(host):
    """Resolves `host` and returns ONE address — but only if every address it
    resolves to is on the public internet. Returns None otherwise.

    The title fetch sends a request to whatever URL the user copied, so a hostile
    page could put `http://10.0.0.1/...` on the clipboard and have us scan the
    internal network (SSRF). Returning the address (rather than a bool) lets the
    caller connect to exactly what was validated — see _pinned_opener().
    """
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return None
    if not infos:
        return None
    chosen = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast:
            return None
        if chosen is None:
            chosen = info[4][0]
    return chosen


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    """Refuses to follow redirects; _fetch_bounded() follows them by hand.

    Letting urllib follow them would reuse this hop's pinned address for the
    next host, and would skip the public-address check on the new target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pinned_opener(host, addr):
    """An opener that connects ONLY to `addr`, whatever DNS says afterwards.

    _public_address() resolves the name to validate it, and without pinning the
    connection would resolve it a *second* time. An attacker who controls the
    domain can answer the first lookup with a public address and the second with
    an internal one (DNS rebinding), stepping straight past the check.
    """
    import http.client

    class PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((addr, self.port), self.timeout)

    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((addr, self.port), self.timeout)
            # server_hostname stays the real name, so the certificate is still
            # validated against the host the user actually copied.
            self.sock = self._context.wrap_socket(sock, server_hostname=host)

    class PinnedHTTPHandler(urllib_request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(PinnedHTTP, req)

    class PinnedHTTPSHandler(urllib_request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(PinnedHTTPS, req, context=self._context)

    return urllib_request.build_opener(PinnedHTTPHandler, PinnedHTTPSHandler, _NoRedirect)


def _read_until(resp, deadline):
    """Reads at most TITLE_MAX_BYTES, giving up at `deadline`.

    urllib's timeout is per socket operation, so a server that trickles a few
    bytes every couple of seconds can hold a worker for ever. The pool only has
    three workers, so three such URLs would kill the feature for the session.
    """
    buf = b""
    while len(buf) < TITLE_MAX_BYTES and time.time() < deadline:
        chunk = resp.read(min(16384, TITLE_MAX_BYTES - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf


def _fetch_bounded(url):
    """GETs `url` with every hop re-validated and pinned. Returns bytes or None."""
    from urllib.error import HTTPError, URLError
    from urllib.parse import urljoin
    deadline = time.time() + TITLE_TOTAL_TIMEOUT
    for _ in range(TITLE_MAX_REDIRECTS + 1):
        if as_web_link(url) is None:
            return None
        addr = _public_address(_host_of(url))
        if addr is None:
            return None  # internal / loopback address — send nothing
        req = urllib_request.Request(url, headers={"User-Agent": TITLE_UA})
        try:
            with _pinned_opener(_host_of(url), addr).open(
                    req, timeout=TITLE_SOCKET_TIMEOUT) as r:
                return _read_until(r, deadline)
        except HTTPError as e:
            location = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
            e.close()
            if not location or time.time() > deadline:
                return None
            url = urljoin(url, location)
        except (URLError, OSError, ValueError):
            return None
    return None


_title_pool = None  # created in main() — keeps the thread count bounded


def _fetch_title(item_id, url):
    """Fetches the page title for a link item in the background (best-effort)."""
    try:
        raw = _fetch_bounded(url)
        if not raw:
            return
        m = re.search(rb"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        if not m:
            return
        import html as html_mod
        title = None
        for enc in ("utf-8", "iso-8859-9", "latin-1"):
            try:
                title = m.group(1).decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not title:
            return
        title = html_mod.unescape(" ".join(title.split()))[:200]
        if not title:
            return
        with _lock:
            for it in _items:
                if it["id"] == item_id:
                    it["title"] = title
                    break
        save_backup()
    except Exception:
        pass  # the title is cosmetic; failures are swallowed


# -------------------------------------------------------------- item operations

_last_text = None
_last_time = 0.0
_ignore_text = None
_ignore_until = 0.0


def suppress_next(text, secs=3.0):
    """Ignore this text on the clipboard for a few seconds (it is our own copy)."""
    global _ignore_text, _ignore_until
    _ignore_text = text
    _ignore_until = time.time() + secs


def add_item(text):
    """Adds a copy to the in-memory list.

    Returns: ("new"|"dup", item) | ("skip"|"filtered", None)
    "dup": the same text is already in the active collection -> no new row is
    created, the copy counter is bumped; a completed row is reopened (it means
    the item is to be processed again).
    """
    global _last_text, _last_time, _next_id
    now = time.time()
    if text == _last_text and (now - _last_time) < DEDUP_WINDOW:
        _last_time = now
        return "skip", None
    _last_text = text
    _last_time = now

    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n… (truncated)"
    url = as_web_link(text)
    with _lock:
        if not passes_filter(url):
            return "filtered", None
        col = _settings["active_collection"]
        for it in _items:
            if it["collection"] == col and it["text"] == text:
                it["copies"] = it.get("copies", 1) + 1
                it["copied_at"] = now_iso()
                if it["checked"]:
                    it["checked"] = False
                    it["checked_at"] = None
                save_backup()
                return "dup", dict(it)
        item = {
            "id": _next_id,
            "text": text,
            "url": url,
            "is_link": url is not None,
            "title": None,
            "copied_at": now_iso(),
            "checked": False,
            "checked_at": None,
            "pinned": False,
            "copies": 1,
            "collection": col,
        }
        _next_id += 1
        _items.append(item)
        if len(_items) > MAX_ITEMS:  # keep the list from growing without bound
            for i, old in enumerate(_items):
                if not old.get("pinned"):
                    dropped = _items.pop(i)
                    # No clipboard content here: the log may be shared or backed
                    # up, and a flood of copies would dump the user's own items.
                    log(f"List limit ({MAX_ITEMS}) exceeded; dropped the oldest item "
                        f"#{dropped['id']} ({len(dropped['text'])} characters).")
                    break
    save_backup()
    if url and _title_pool is not None:
        _title_pool.submit(_fetch_title, item["id"], url)
    return "new", dict(item)


def toggle_item(item_id, checked=None):
    with _lock:
        for it in _items:
            if it["id"] == item_id:
                new_val = (not it["checked"]) if checked is None else bool(checked)
                it["checked"] = new_val
                it["checked_at"] = now_iso() if new_val else None
                save_backup()
                return dict(it)
    return None


def pin_item(item_id, pinned):
    with _lock:
        for it in _items:
            if it["id"] == item_id:
                it["pinned"] = bool(pinned)
                save_backup()
                return dict(it)
    return None


def delete_items(ids):
    with _lock:
        before = len(_items)
        idset = set(ids)
        _items[:] = [it for it in _items if it["id"] not in idset]
        removed = before - len(_items)
    save_backup()
    return removed


def archive_items(ids):
    """Moves items out of RAM and onto disk (the archive).

    If the disk write fails the WHOLE change is rolled back — the items stay in
    the active list instead of vanishing halfway through.
    """
    idset = set(ids)
    moved = 0
    with _lock:
        prev_items = list(_items)
        prev_archive = list(_archive["items"])
        prev_next_aid = _archive["next_aid"]
        keep = []
        for it in _items:
            if it["id"] in idset:
                _archive["items"].append({
                    "aid": _archive["next_aid"],
                    "text": it["text"],
                    "url": it["url"],
                    "is_link": it["is_link"],
                    "title": it.get("title"),
                    "copied_at": it["copied_at"],
                    "checked": it["checked"],
                    "archived_at": now_iso(),
                    "collection_name": collection_name(it["collection"]),
                })
                _archive["next_aid"] += 1
                moved += 1
            else:
                keep.append(it)
        _items[:] = keep
        if moved:
            try:
                save_archive()
            except StorageError:
                _items[:] = prev_items  # roll back
                _archive["items"] = prev_archive
                _archive["next_aid"] = prev_next_aid
                raise
    save_backup()
    return moved


def clear_items(cid):
    """Clears a collection; pinned (📌) items are left untouched."""
    with _lock:
        _items[:] = [it for it in _items
                     if it["collection"] != cid or it.get("pinned")]
    save_backup()


def archive_delete(aids):
    with _lock:
        aidset = set(aids)
        before = len(_archive["items"])
        _archive["items"] = [e for e in _archive["items"] if e["aid"] not in aidset]
        removed = before - len(_archive["items"])
        if removed:
            save_archive()
    return removed


def archive_restore(aids):
    """Brings archived items back into the active collection."""
    global _next_id
    aidset = set(aids)
    restored = 0
    with _lock:
        keep = []
        for e in _archive["items"]:
            if e["aid"] in aidset:
                _items.append({
                    "id": _next_id,
                    "text": e["text"],
                    "url": e.get("url"),
                    "is_link": bool(e.get("is_link")),
                    "title": e.get("title"),
                    "copied_at": e.get("copied_at") or now_iso(),
                    "checked": False,
                    "checked_at": None,
                    "pinned": False,
                    "copies": 1,
                    "collection": _settings["active_collection"],
                })
                _next_id += 1
                restored += 1
            else:
                keep.append(e)
        _archive["items"] = keep
        if restored:
            save_archive()
    save_backup()
    return restored


def purge_archive():
    """Deletes archive entries automatically according to the retention rule."""
    r = _settings["retention"]
    if r == "forever":
        return 0
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    def expired(e):
        ts = e.get("archived_at") or ""
        if r == "eod":
            return bool(ts[:10]) and ts[:10] < today
        try:
            age = now - datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return False
        return age > RETENTION_SECS[r]

    with _lock:
        before = len(_archive["items"])
        _archive["items"] = [e for e in _archive["items"] if not expired(e)]
        removed = before - len(_archive["items"])
        if removed:
            save_archive()
    if removed:
        log(f"Deleted {removed} expired item(s) from the archive (rule: {r}).")
    return removed


# ------------------------------------------------------------ start with Windows

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EasyCopyTracker"


def _startup_command():
    """The command run at logon — the console-less pythonw is preferred."""
    script = os.path.join(BASE_DIR, "easycopytracker.py")
    exe = sys.executable or "python.exe"
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{script}"'


def get_startup():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, RUN_NAME)
        return bool(value)
    except OSError:
        return False


def _drop_legacy_startup():
    """Removes the startup entry left under the old "CopyTracker" name."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, "CopyTracker")
                log("Removed the old startup entry (CopyTracker).")
            except FileNotFoundError:
                pass
    except OSError:
        pass


def set_startup(enabled):
    """Updates the registry Run key. Returns an error message, or None."""
    _drop_legacy_startup()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _startup_command())
                log("Start with Windows enabled.")
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
                log("Start with Windows disabled.")
        return None
    except OSError as e:
        log(f"Could not change the start-with-Windows setting: {e}")
        return str(e)


def announce_capture(enabled):
    log("Capture " + ("started." if enabled else "stopped."))
    notify("📋 Capture " + ("on" if enabled else "off"),
           "Copies are being saved." if enabled else "Copies are no longer saved.")


def set_capture(enabled):
    """For the tray menu / hotkey: sets capture, persists it and notifies."""
    with _lock:
        previous = _settings["capture_enabled"]
        _settings["capture_enabled"] = bool(enabled)
    try:
        save_settings()
    except StorageError:
        with _lock:
            _settings["capture_enabled"] = previous
        notify("⚠️ Setting not saved", "The capture state could not be changed.")
        return
    announce_capture(enabled)


# -------------------------------------------------------------- notifications
# Windows' own toast notifications can be switched off in the user's settings,
# so notifications are drawn in the app's own small window instead.

_toasts = queue.Queue()
TOAST_W, TOAST_H = 344, 96
TOAST_SHOW_MS = 3200
TOAST_MAX_AGE = 8.0
TOAST_MAX_ON_SCREEN = 5
TOAST_BURST, TOAST_BURST_WINDOW = 4, 3.0   # beyond this the toasts are summarised
_toasts_hidden = 0


def notify(title, msg, force=False):
    """Shows a notification. force=True ones (setting changes) always appear."""
    global _toasts_hidden
    if not force and not _settings.get("notifications_enabled", True):
        return
    if not force and rate_limited("toast", TOAST_BURST, TOAST_BURST_WINDOW):
        # Copies are arriving faster than a person can make them. Summarise
        # instead of stacking windows — see rate_limited() for why.
        _toasts_hidden += 1
        if rate_limited("toast_summary", 1, 5.0):
            return
        hidden, _toasts_hidden = _toasts_hidden, 0
        title = f"📋 {hidden} more copies saved"
        msg = "Copies are arriving very fast, so notifications are summarised."
    _toasts.put((title, msg, time.time()))


def _work_area():
    rect = wt.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _round_corners(hwnd):
    try:
        dwm = ctypes.WinDLL("dwmapi")
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


_active_toasts = []


def _show_toast(tk, root, title, msg):
    """Shows a single notification (a Toplevel) under the persistent root window.

    Opening a separate Tk() root per notification could hard-kill the process
    when done over and over inside a thread; one root plus Toplevels is the
    path Tk actually supports.
    """
    if len(_active_toasts) >= TOAST_MAX_ON_SCREEN:
        return  # hard ceiling, independent of the rate limit in notify()
    win = tk.Toplevel(root)
    win.withdraw()
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)

    slot = len(_active_toasts)            # keep them from stacking on top of each other
    _active_toasts.append(win)
    _, _, right, bottom = _work_area()
    y = bottom - TOAST_H - 16 - slot * (TOAST_H + 10)
    win.geometry(f"{TOAST_W}x{TOAST_H}+{right - TOAST_W - 16}+{y}")

    win.configure(bg="#1a0f15")
    tk.Frame(win, bg="#ff2d6f", width=4).pack(side="left", fill="y")  # brand colour
    box = tk.Frame(win, bg="#1a0f15")
    box.pack(side="left", fill="both", expand=True, padx=14, pady=10)
    tk.Label(box, text=title, bg="#1a0f15", fg="#ffffff",
             font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
             wraplength=TOAST_W - 50).pack(fill="x")
    tk.Label(box, text=msg, bg="#1a0f15", fg="#c6cbd4",
             font=("Segoe UI", 9), anchor="w", justify="left",
             wraplength=TOAST_W - 50).pack(fill="x", pady=(3, 0))

    closed = []

    def close():
        if closed:
            return
        closed.append(True)
        if win in _active_toasts:
            _active_toasts.remove(win)
        try:
            win.destroy()
        except Exception:
            pass

    def open_list(_e=None):
        try:
            webbrowser.open(URL)
        finally:
            close()

    win.bind("<Button-1>", open_list)
    for child in (box,) + tuple(box.winfo_children()):
        child.bind("<Button-1>", open_list)
    win.deiconify()
    win.update_idletasks()
    _round_corners(user32.GetParent(win.winfo_id()) or win.winfo_id())

    def fade(step, delta, on_end):
        if closed:
            return
        try:
            win.attributes("-alpha", max(0.0, min(1.0, step / 10)))
        except Exception:
            return close()
        nxt = step + delta
        if (delta > 0 and nxt > 10) or (delta < 0 and nxt < 0):
            on_end()
            return
        win.after(18, lambda: fade(nxt, delta, on_end))

    fade(0, 1, lambda: win.after(TOAST_SHOW_MS, lambda: fade(10, -1, close)))


def toast_thread():
    """Draws notifications through a single Tk root and a single message loop."""
    try:
        import tkinter as tk
    except Exception as e:
        log(f"tkinter is unavailable, notifications disabled: {e}")
        return
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception as e:
        log(f"Could not create the notification window, notifications disabled: {e}")
        return

    def pump():
        try:
            while True:
                title, msg, ts = _toasts.get_nowait()
                if time.time() - ts <= TOAST_MAX_AGE:
                    _show_toast(tk, root, title, msg)
        except queue.Empty:
            pass
        except Exception as e:
            log(f"Could not show a notification: {e}")
        root.after(150, pump)

    root.after(150, pump)
    try:
        root.mainloop()
    except Exception as e:
        log(f"The notification loop stopped: {e}")


# ------------------------------------------------- Win32 (ctypes) declarations

CF_UNICODETEXT = 13
WM_CLIPBOARDUPDATE = 0x031D
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_COMMAND = 0x0111
WM_TRAY = 0x8001          # WM_APP + 1
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 1, 2, 0x4000
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
SM_CXSMICON, SM_CXICON = 49, 11
WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
ID_TRAY_OPEN, ID_TRAY_TOGGLE, ID_TRAY_EXIT = 1001, 1002, 1003
HK_TOGGLE, HK_OPEN = 1, 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HANDLE),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.WORD
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.AddClipboardFormatListener.argtypes = [wt.HWND]
user32.AddClipboardFormatListener.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
user32.IsClipboardFormatAvailable.restype = wt.BOOL
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wt.UINT
user32.CountClipboardFormats.argtypes = []
user32.CountClipboardFormats.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostThreadMessageW.restype = wt.BOOL
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, wt.LPVOID, wt.UINT]
user32.SystemParametersInfoW.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPVOID]
user32.LoadIconW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID]
user32.TrackPopupMenu.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
kernel32.GlobalLock.argtypes = [wt.HANDLE]
kernel32.GlobalLock.restype = wt.LPVOID
kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalSize.argtypes = [wt.HANDLE]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.SetConsoleCtrlHandler.argtypes = [PHANDLER_ROUTINE, wt.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL

CF_EXCLUDE = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")

_main_tid = None


def _console_ctrl(event):
    if event in (0, 1, 2, 5, 6):  # CTRL_C, CTRL_BREAK, CTRL_CLOSE, LOGOFF, SHUTDOWN
        if _main_tid:
            user32.PostThreadMessageW(_main_tid, WM_QUIT, 0, 0)
        return True
    return False


_console_ctrl_ref = PHANDLER_ROUTINE(_console_ctrl)


# ------------------------------------------------------------ clipboard reading

def read_clipboard():
    """(status, text): 'ok'|'empty'|'excluded'|'no_text'|'error'."""
    for _ in range(30):  # ~1.5 s total budget
        if not user32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            if user32.CountClipboardFormats() == 0:
                return "empty", None
            if CF_EXCLUDE and user32.IsClipboardFormatAvailable(CF_EXCLUDE):
                return "excluded", None
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return "no_text", None
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    try:
                        size = kernel32.GlobalSize(h)
                        text = ctypes.wstring_at(ptr, size // 2) if size else ctypes.wstring_at(ptr)
                        return "ok", text.split("\x00", 1)[0]
                    finally:
                        kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
        time.sleep(0.05)
    log("The clipboard stayed locked for ~1.5 s — this copy could not be saved.")
    return "error", None


_events = queue.Queue()
_last_nontext = 0.0
_last_blind_warn = 0.0


FILTER_LABEL = {"links": "Links only", "instagram": "Instagram only",
                "custom": "Custom domains"}


def warn_filtered():
    """Tells the user why a copy was dropped by the capture filter.

    With a filter on, the app looked like it "just did not work": you copy,
    nothing happens, and nothing on screen explains why. Show a reminder at most
    once every 30 s so it does not nag while you copy repeatedly.
    """
    global _last_blind_warn
    now = time.time()
    if now - _last_blind_warn < 30:
        return
    _last_blind_warn = now
    with _lock:
        mode = _settings["filter_mode"]
        domains = [d for d in _settings["custom_domains"] if d.strip()]
    if mode == "custom" and not domains:
        notify("⚠️ Nothing is being saved",
               "The filter is 'Custom domains' but the list is empty. "
               "Add a domain or pick 'Everything'.")
        return
    notify(f"🚫 Dropped by the capture filter — {FILTER_LABEL.get(mode, mode)}",
           "Set the filter on the left to 'Everything' to save everything.")


def clipboard_worker():
    global _last_nontext
    while True:
        _events.get()
        try:
            if not _settings["capture_enabled"]:
                continue  # capture is off — swallow the event quietly
            status, text = read_clipboard()
            if status == "ok":
                if not text.strip():
                    continue
                if (_ignore_text is not None and text == _ignore_text
                        and time.time() < _ignore_until):
                    continue  # our own copy, made by the UI's "Copy" button
                kind, item = add_item(text)
                if kind in ("skip", "filtered"):
                    if kind == "filtered":
                        if not rate_limited("filter_log", 30, 10.0):
                            log(f"Dropped by the capture filter ({len(text)} characters).")
                        warn_filtered()
                    continue
                # Clipboard CONTENT never reaches the log (the log file may be
                # shared or backed up); only numeric metadata is kept. The content
                # appears solely in the on-screen notification. The log line is
                # also rate limited so a scripted flood cannot grow the file.
                col = collection_name(item["collection"])
                where = f" → {col}" if col != "General" else ""
                quiet = rate_limited("capture_log", 30, 10.0)
                if kind == "dup":
                    if not quiet:
                        log(f"#{item['id']} copied again (×{item['copies']}).")
                    notify("♻️ Already on the list",
                           f"Copied again (×{item['copies']}): {short(text, 80)}")
                elif item["is_link"]:
                    if not quiet:
                        log(f"#{item['id']} link saved ({col}, {_host_of(item['url'])})")
                    notify("🔗 Website link copied",
                           f"Saved{where}: {short(item['url'])}")
                else:
                    if not quiet:
                        log(f"#{item['id']} text saved ({col}, {len(text)} characters)")
                    notify(f"📋 Copied and saved{where}", short(text))
            elif status == "no_text":
                now = time.time()
                if now - _last_nontext > DEDUP_WINDOW:
                    _last_nontext = now
                    notify("📎 Copied (not text)", "Image and file contents are not saved.")
        except Exception as e:
            log(f"Clipboard processing error: {e}")


# --------------------------------------------- hidden window + tray + hotkeys

_hwnd = None
_tray_data = None


def _app_icon(size=0):
    """Loads docs/easycopytracker.ico, falling back to the Windows default.

    size=0 -> let Windows pick the small size suited to the notification area.
    """
    if os.path.exists(ICON_FILE):
        h = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON, size, size,
                              LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if h:
            return h
        log("Could not load the app icon; using the default one.")
    return user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION


def _tray_add(hwnd):
    global _tray_data
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _app_icon(user32.GetSystemMetrics(SM_CXSMICON))
    nid.szTip = "Easy Copy Tracker — clipboard inbox"
    if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        _tray_data = nid
    else:
        log("Could not add the tray icon (not critical).")


def _tray_remove():
    if _tray_data is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_tray_data))


def _tray_menu(hwnd):
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    try:
        toggle_text = ("⏸ Pause Capture" if _settings["capture_enabled"]
                       else "▶ Start Capture")
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_OPEN, "📋 Open the List")
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_TOGGLE, toggle_text)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_EXIT, "✕ Quit")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, hwnd, None)
        if cmd == ID_TRAY_OPEN:
            webbrowser.open(URL)
        elif cmd == ID_TRAY_TOGGLE:
            set_capture(not _settings["capture_enabled"])
        elif cmd == ID_TRAY_EXIT:
            user32.PostQuitMessage(0)
    finally:
        user32.DestroyMenu(menu)


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_CLIPBOARDUPDATE:
        _events.put(time.time())
        return 0
    if msg in (WM_CLOSE, WM_DESTROY):
        user32.PostQuitMessage(0)  # end the message loop -> clean shutdown
        return 0
    if msg == WM_TRAY:
        ev = lparam & 0xFFFF
        if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
            _tray_menu(hwnd)
        elif ev == WM_LBUTTONDBLCLK:
            webbrowser.open(URL)
        return 0
    if msg == WM_HOTKEY:
        if wparam == HK_TOGGLE:
            set_capture(not _settings["capture_enabled"])
        elif wparam == HK_OPEN:
            webbrowser.open(URL)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_wnd_proc_ref = WNDPROC(_wnd_proc)  # keep it from being garbage-collected


def run_listener():
    """Hidden window: clipboard listener + tray icon + global hotkeys (main thread)."""
    global _hwnd
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wnd_proc_ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "EasyCopyTrackerListener"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Tray callbacks never reach message-only windows, so use a normal hidden one
    _hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP_NAME, 0,
                                   0, 0, 0, 0, None, None, wc.hInstance, None)
    if not _hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.AddClipboardFormatListener(_hwnd):
        raise ctypes.WinError(ctypes.get_last_error())
    # Window icon: show the app icon in Alt+Tab and in Task Manager
    small = _app_icon(user32.GetSystemMetrics(SM_CXSMICON))
    big = _app_icon(user32.GetSystemMetrics(SM_CXICON))
    user32.SendMessageW(_hwnd, WM_SETICON, ICON_SMALL, small)
    user32.SendMessageW(_hwnd, WM_SETICON, ICON_BIG, big)
    _tray_add(_hwnd)
    if not user32.RegisterHotKey(_hwnd, HK_TOGGLE, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4B):
        log("Could not register the Ctrl+Alt+K hotkey (another app may hold it).")
    if not user32.RegisterHotKey(_hwnd, HK_OPEN, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4C):
        log("Could not register the Ctrl+Alt+L hotkey (another app may hold it).")
    log("Clipboard listener ready — tray icon and hotkeys active (Ctrl+Alt+K / Ctrl+Alt+L).")
    msg = wt.MSG()
    try:
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _tray_remove()


# -------------------------------------------------------------- web interface

app = Flask(__name__)

ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
# Port-less spellings are deliberately absent: Werkzeug normalises "127.0.0.1:80"
# down to "127.0.0.1", so allowing the bare form would let that Host through.
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}"}
# Our own page always sends "same-origin"; typing the address sends "none".
# Anything else is a request another site made on our behalf.
ALLOWED_FETCH_SITES = {"same-origin", "none"}


@app.before_request
def request_guard():
    """DNS rebinding + CSRF protection.

    The Host check applies to EVERY request: even if a hostile site re-resolves
    its domain to 127.0.0.1 (DNS rebinding) and thereby becomes same-origin with
    us, the Host header still carries its own domain and the request is refused.

    Same-origin policy already stops another page from *reading* a GET response,
    but not from making the request: status code, timing and the intrinsic size
    of an image reply (/api/qr) still leak. So cross-site subresource loads are
    refused as well. Following a plain link to the UI from somewhere else is
    still allowed — that opens a tab the other site cannot read, and framing is
    already blocked by X-Frame-Options.
    """
    if (request.host or "").lower() not in ALLOWED_HOSTS:
        return jsonify({"ok": False, "error": "invalid host"}), 403
    site = request.headers.get("Sec-Fetch-Site")
    navigating = (request.headers.get("Sec-Fetch-Mode") == "navigate"
                  and request.headers.get("Sec-Fetch-Dest") == "document")
    if site and site not in ALLOWED_FETCH_SITES and not navigating:
        return jsonify({"ok": False, "error": "invalid origin"}), 403
    if request.method == "POST":
        if request.headers.get("X-EasyCopyTracker") != "1":
            return jsonify({"ok": False, "error": "invalid request"}), 403
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({"ok": False, "error": "invalid origin"}), 403


@app.after_request
def security_headers(resp):
    """Baseline headers against clickjacking and content-type sniffing."""
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https://icons.duckduckgo.com; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    return resp


@app.get("/")
def index():
    return send_file(os.path.join(BASE_DIR, "web", "index.html"), max_age=0)


@app.get("/favicon.ico")
def favicon():
    if os.path.exists(ICON_FILE):
        return send_file(ICON_FILE, max_age=86400)
    return ("", 404)


@app.get("/api/items")
def api_items():
    # Snapshot under the lock, serialise outside it: turning a full list into
    # JSON takes long enough that holding the lock would stall clipboard capture
    # for as long as anything keeps requesting this endpoint.
    with _lock:
        payload = {
            "started": _started,
            "capture_enabled": _settings["capture_enabled"],
            "filter_mode": _settings["filter_mode"],
            "custom_domains": list(_settings["custom_domains"]),
            "retention": _settings["retention"],
            "active_collection": _settings["active_collection"],
            "collections": [dict(c) for c in _settings["collections"]],
            "items": [dict(it) for it in _items],
            "archive_count": len(_archive["items"]),
            "recovery_count": len(_recovery) if _recovery else 0,
            "qr_available": HAS_QR,
            "data_dir": DATA_DIR,
            "notifications_enabled": _settings["notifications_enabled"],
        }
    payload["startup_enabled"] = get_startup()  # reads the registry — never under the lock
    return jsonify(payload)


@app.get("/api/archive")
def api_archive():
    with _lock:
        payload = {"items": [dict(e) for e in _archive["items"]],
                   "retention": _settings["retention"]}
    return jsonify(payload)


@app.post("/api/settings")
def api_settings():
    """Updates settings. Every field is validated FIRST and applied afterwards,
    so an invalid field never leaves the others half-applied."""
    body = request.get_json(silent=True) or {}
    updates, changed = {}, []

    if "filter_mode" in body:
        if body["filter_mode"] not in FILTER_MODES:
            return jsonify({"ok": False, "error": "invalid mode"}), 400
        updates["filter_mode"] = body["filter_mode"]
        changed.append("filter=" + body["filter_mode"])
    if "custom_domains" in body:
        doms = body["custom_domains"]
        if not isinstance(doms, list):
            return jsonify({"ok": False, "error": "invalid domain list"}), 400
        updates["custom_domains"] = [
            _norm_domain(str(s))[:100] for s in doms if str(s).strip()][:50]
        changed.append("custom domains")
    if "retention" in body:
        if body["retention"] not in RETENTIONS:
            return jsonify({"ok": False, "error": "invalid retention"}), 400
        updates["retention"] = body["retention"]
        changed.append("retention=" + body["retention"])

    if "notifications_enabled" in body:
        updates["notifications_enabled"] = bool(body["notifications_enabled"])
        changed.append("notifications=" + ("on" if body["notifications_enabled"] else "off"))

    if "startup_enabled" in body:  # registry-backed; not stored in the settings file
        err = set_startup(bool(body["startup_enabled"]))
        if err:
            return jsonify({"ok": False, "error": f"could not set start with Windows: {err}"}), 500
        changed.append("start with Windows=" + ("on" if body["startup_enabled"] else "off"))

    with _lock:
        previous = {k: _settings[k] for k in updates}
        _settings.update(updates)
    if "notifications_enabled" in body:
        notify("🔔 Notifications " + ("on" if body["notifications_enabled"] else "off"),
               "Copy notifications will be shown." if body["notifications_enabled"]
               else "Copy notifications will no longer be shown.", force=True)
    if "capture_enabled" in body:
        with _lock:
            previous["capture_enabled"] = _settings["capture_enabled"]
            _settings["capture_enabled"] = bool(body["capture_enabled"])
        changed.append("capture=" + ("on" if body["capture_enabled"] else "off"))
    try:
        save_settings()
    except StorageError as e:
        with _lock:
            _settings.update(previous)  # the disk write failed — roll RAM back too
        return jsonify({"ok": False, "error": f"could not save settings: {e}"}), 500
    if "capture_enabled" in body:
        announce_capture(_settings["capture_enabled"])
    if changed:
        log("Setting changed: " + ", ".join(changed))
    return jsonify({"ok": True})


@app.post("/api/collections")
def api_collections_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:MAX_COLLECTION_NAME]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    with _lock:
        if len(_settings["collections"]) >= MAX_COLLECTIONS:
            return jsonify({"ok": False, "error": "too many collections"}), 400
        if any(c["name"].lower() == name.lower() for c in _settings["collections"]):
            return jsonify({"ok": False,
                            "error": "A collection with this name already exists"}), 409
        c = {"id": _settings["next_collection_id"], "name": name, "created_at": now_iso()}
        _settings["next_collection_id"] += 1
        _settings["collections"].append(c)
        _settings["active_collection"] = c["id"]
        resp = dict(c)
    save_settings()
    log(f"Collection created and made active: {name}")
    return jsonify({"ok": True, "collection": resp})


@app.post("/api/collections/activate")
def api_collections_activate():
    body = request.get_json(silent=True) or {}
    try:
        cid = int(body.get("id", -1))
    except (TypeError, ValueError):
        cid = -1
    with _lock:
        if not any(c["id"] == cid for c in _settings["collections"]):
            return jsonify({"ok": False, "error": "not found"}), 404
        _settings["active_collection"] = cid
    save_settings()
    return jsonify({"ok": True})


@app.post("/api/collections/delete")
def api_collections_delete():
    body = request.get_json(silent=True) or {}
    try:
        cid = int(body.get("id", -1))
    except (TypeError, ValueError):
        cid = -1
    if cid == 1:
        return jsonify({"ok": False, "error": "The General collection cannot be deleted"}), 400
    with _lock:
        if not any(c["id"] == cid for c in _settings["collections"]):
            return jsonify({"ok": False, "error": "not found"}), 404
        _settings["collections"] = [c for c in _settings["collections"] if c["id"] != cid]
        for it in _items:
            if it["collection"] == cid:
                it["collection"] = 1  # items move to General
        if _settings["active_collection"] == cid:
            _settings["active_collection"] = 1
    save_settings()
    save_backup()
    return jsonify({"ok": True})


def _ids_from(body, key="ids"):
    raw = body.get(key, [])
    if not isinstance(raw, list):
        return []
    out = []
    for v in raw[:MAX_ITEMS]:  # upper bound: more than the whole list is pointless
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


@app.post("/api/toggle")
def api_toggle():
    body = request.get_json(silent=True) or {}
    try:
        item_id = int(body.get("id", -1))
    except (TypeError, ValueError):
        item_id = -1
    checked = body.get("checked")
    if checked is not None:
        checked = bool(checked)
    it = toggle_item(item_id, checked)
    if it is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "item": it})


@app.post("/api/pin")
def api_pin():
    body = request.get_json(silent=True) or {}
    try:
        item_id = int(body.get("id", -1))
    except (TypeError, ValueError):
        item_id = -1
    it = pin_item(item_id, bool(body.get("pinned")))
    if it is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "item": it})


@app.post("/api/delete")
def api_delete():
    body = request.get_json(silent=True) or {}
    ids = _ids_from(body)
    if not ids:
        return jsonify({"ok": False, "error": "id required"}), 400
    removed = delete_items(ids)
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/archive")
def api_archive_move():
    body = request.get_json(silent=True) or {}
    ids = _ids_from(body)
    if not ids:
        return jsonify({"ok": False, "error": "id required"}), 400
    try:
        moved = archive_items(ids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"could not write the archive: {e}"}), 500
    return jsonify({"ok": True, "moved": moved})


@app.post("/api/archive/delete")
def api_archive_delete():
    body = request.get_json(silent=True) or {}
    aids = _ids_from(body, "aids")
    if not aids:
        return jsonify({"ok": False, "error": "aid required"}), 400
    try:
        removed = archive_delete(aids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"could not update the archive: {e}"}), 500
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/archive/restore")
def api_archive_restore():
    body = request.get_json(silent=True) or {}
    aids = _ids_from(body, "aids")
    if not aids:
        return jsonify({"ok": False, "error": "aid required"}), 400
    try:
        restored = archive_restore(aids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"could not update the archive: {e}"}), 500
    return jsonify({"ok": True, "restored": restored})


@app.post("/api/quit")
def api_quit():
    """Clean shutdown — stop.bat uses this so the crash shadow is removed and
    the next start does not show a bogus 'crash' banner."""
    if _hwnd:
        user32.PostMessageW(_hwnd, WM_CLOSE, 0, 0)
    else:
        user32.PostThreadMessageW(_main_tid or 0, WM_QUIT, 0, 0)
    log("Shutdown requested (stop.bat / the UI).")
    return jsonify({"ok": True})


@app.post("/api/clear")
def api_clear():
    body = request.get_json(silent=True) or {}
    cid = body.get("collection")
    if not isinstance(cid, int):
        return jsonify({"ok": False, "error": "collection id required"}), 400
    clear_items(cid)
    return jsonify({"ok": True})


@app.post("/api/copied")
def api_copied():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if isinstance(text, str) and text:
        suppress_next(text)
    return jsonify({"ok": True})


@app.post("/api/recovery")
def api_recovery():
    global _recovery, _next_id
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action == "restore":
        with _lock:  # the check is inside the lock: a double click must not restore twice
            pending, _recovery = _recovery, None
            if pending is None:
                return jsonify({"ok": False, "error": "nothing to recover"}), 404
            valid_cols = {c["id"] for c in _settings["collections"]}
            for src in pending:
                it = _sanitize_entry(src, False)
                if it is None:
                    continue
                it["id"] = _next_id
                it["checked_at"] = it.get("checked_at") if it["checked"] else None
                if it["collection"] not in valid_cols:
                    it["collection"] = 1
                _next_id += 1
                _items.append(it)
            count = len(pending)
        save_backup()
        try:
            os.remove(RECOVERY_FILE)
        except OSError:
            pass
        log(f"Restored {count} item(s) from the previous session.")
        return jsonify({"ok": True, "restored": count})
    if action == "discard":
        _recovery = None
        try:
            os.remove(RECOVERY_FILE)
        except OSError:
            pass
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "invalid action"}), 400


@app.get("/api/qr")
def api_qr():
    if not HAS_QR:
        return jsonify({"ok": False, "error": "the qrcode package is not installed"}), 501
    try:
        item_id = int(request.args.get("id", -1))
    except (TypeError, ValueError):
        item_id = -1
    data = None
    with _lock:
        for it in _items:
            if it["id"] == item_id and it.get("url"):
                data = it["url"]
                break
    if not data:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    except (ValueError, TypeError):  # a URL past ~2900 chars has no QR version
        return jsonify({"ok": False, "error": "this link is too long for a QR code"}), 400
    return Response(img.to_string(), mimetype="image/svg+xml")


def run_flask():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


# ------------------------------------------------------------- maintenance task

def maintenance_thread():
    """Once a minute: enforce the archive retention rule."""
    while True:
        time.sleep(60)
        try:
            purge_archive()
        except Exception as e:
            log(f"Archive cleanup error: {e}")


# ---------------------------------------------------------------------- main

def _easycopytracker_on_port():
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/items", timeout=2) as r:
            return b'"items"' in r.read(4096)
    except Exception:
        return False


def main():
    global _main_tid
    probe = socket.socket()
    try:
        probe.bind((HOST, PORT))
    except OSError:
        probe.close()
        if _easycopytracker_on_port():
            log("Easy Copy Tracker is already running — opening the list in the browser.")
            webbrowser.open(URL)
        else:
            log(f"ERROR: port {PORT} is held by another application, so Easy Copy "
                f"Tracker could not start. Close that application and try again.")
        return
    probe.close()

    global _title_pool
    migrate_from_old_name()
    migrate_data_dir()
    _drop_legacy_startup()
    try:  # a corrupt/tampered file must not make the app permanently unstartable
        migrate_legacy()
        if not load_settings():
            save_settings()  # first run — write the defaults
        load_archive()
        check_recovery()
        purge_archive()
    except Exception as e:
        log(f"ERROR: stored data could not be read ({e}). Quarantining the corrupt files.")
        for path in (SETTINGS_FILE, ARCHIVE_FILE, RECOVERY_FILE, BACKUP_FILE):
            if os.path.exists(path):
                try:
                    os.replace(path, path + ".corrupt")
                except OSError:
                    pass
        log("Starting with default settings (the .corrupt backups were kept).")

    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    _title_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="title")
    threading.Thread(target=clipboard_worker, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=toast_thread, daemon=True).start()
    threading.Thread(target=maintenance_thread, daemon=True).start()
    threading.Thread(target=backup_thread, daemon=True).start()
    log(f"Easy Copy Tracker v1.0 started. Web UI: {URL}  (active list in RAM, archive on disk)")
    log(f"Data folder: {DATA_DIR}")
    if not HAS_QR:
        log("Note: the 'qrcode' package is missing — the QR feature is off (pip install qrcode).")
    notify("📋 Easy Copy Tracker started", f"Everything you copy lands on the list: {URL}")
    if "--open" in sys.argv:
        webbrowser.open(URL)

    _main_tid = kernel32.GetCurrentThreadId()
    try:
        kernel32.SetConsoleCtrlHandler(_console_ctrl_ref, True)
    except Exception:
        pass

    try:
        run_listener()
    except KeyboardInterrupt:
        pass
    finally:
        delete_backup()  # clean shutdown — the RAM list is volatile by design
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        log("Easy Copy Tracker stopped.")


if __name__ == "__main__":
    main()
