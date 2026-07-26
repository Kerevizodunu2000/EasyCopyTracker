# -*- coding: utf-8 -*-
"""
CopyTracker v3 — Pano gelen kutusu / link triage aracı.

Mimari:
  - Aktif liste RAM'de tutulur (geçici; uygulama kapanınca gider).
  - Arşiv diske yazılır (archive.json) ve ayarlanan süre sonunda otomatik silinir.
  - Ayarlar + koleksiyonlar settings.json'da kalıcıdır.
  - Çökmeye karşı oturum gölgesi (session_backup.json) tutulur; temiz kapanışta silinir.

Kullanım:
    python copytracker.py     konsolda çalıştırır
    start.bat                 arka planda başlatır + tarayıcıda listeyi açar
    stop.bat / tepsi → Çıkış  durdurur

Kısayollar: Ctrl+Alt+K yakalamayı aç/kapat · Ctrl+Alt+L listeyi aç
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
    """Kişisel veriler %LOCALAPPDATA%\\CopyTracker altında tutulur.

    Uygulama klasörü (ör. C:\\) diğer yerel hesaplara okuma/yazma izni veren bir
    ACL devralabiliyor; kullanıcı profili altındaki dizin varsayılan olarak
    sadece o kullanıcıya açıktır. Ayrıca depoya yanlışlıkla commit'leme riskini
    tamamen ortadan kaldırır.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "CopyTracker")
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
LOG_FILE = os.path.join(DATA_DIR, "copytracker.log")
PID_FILE = os.path.join(DATA_DIR, "copytracker.pid")
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://localhost:{PORT}"
APP_NAME = "CopyTracker"
DEDUP_WINDOW = 1.5   # sn — aynı içeriğin peş peşe yinelenen pano olaylarını eler
MAX_TEXT = 10000     # kaydedilecek azami karakter sayısı
MAX_ITEMS = 2000     # aktif listedeki azami öğe (aşılınca en eski sabitlenmemiş atılır)
BACKUP_INTERVAL = 2.0  # sn — gölge kopyanın azami yazma sıklığı
FILTER_MODES = ("all", "links", "instagram", "custom")
RETENTIONS = ("1h", "1d", "eod", "1m", "forever")
RETENTION_SECS = {"1h": 3600, "1d": 86400, "1m": 30 * 86400}

# pythonw ile (konsolsuz) çalışırken stdout/stderr None olur → log dosyasına yönlendir
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
    print("Flask kurulu değil. Şunu çalıştırın:  pip install -r requirements.txt")
    sys.exit(1)

try:
    import qrcode
    import qrcode.image.svg
    HAS_QR = True
except ImportError:
    HAS_QR = False


# ---------------------------------------------------------------- yardımcılar

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- durum

_lock = threading.RLock()
_started = now_iso()          # oturum başlangıcı (aktif liste bu oturuma ait)
_items = []                   # AKTİF LİSTE — SADECE RAM
_next_id = 1
_recovery = None              # çökmeden kurtarılmayı bekleyen öğeler

_settings = {
    "filter_mode": "all",
    "custom_domains": [],
    "capture_enabled": True,
    "notifications_enabled": True,
    "retention": "1m",
    "collections": [{"id": 1, "name": "Genel", "created_at": _started}],
    "active_collection": 1,
    "next_collection_id": 2,
}

_archive = {"next_aid": 1, "items": []}


class StorageError(Exception):
    """Diske yazma başarısız — çağıran RAM değişikliğini geri almalı."""


def save_settings():
    with _lock:
        try:
            _write_json(SETTINGS_FILE, _settings)
        except OSError as e:
            log(f"HATA: settings.json yazılamadı: {e}")
            raise StorageError(str(e)) from e


def save_archive():
    with _lock:
        try:
            _write_json(ARCHIVE_FILE, _archive)
        except OSError as e:
            log(f"HATA: archive.json yazılamadı: {e}")
            raise StorageError(str(e)) from e


_backup_dirty = threading.Event()


def save_backup():
    """Gölge kopyayı işaretler; asıl yazma backup_thread'de toplu yapılır.

    Her kopyada tüm listeyi diske yazmak O(n²) maliyet çıkarıyordu; bunun yerine
    en fazla BACKUP_INTERVAL sn'de bir yazılır (çökmede en fazla o kadarı kaybolur).
    """
    _backup_dirty.set()


def _flush_backup():
    with _lock:
        snapshot = {"items": list(_items), "next_id": _next_id, "saved_at": now_iso()}
    try:
        _write_json(BACKUP_FILE, snapshot)
    except OSError as e:
        log(f"Gölge kopya yazılamadı: {e}")


def backup_thread():
    while True:
        _backup_dirty.wait()
        _backup_dirty.clear()
        _flush_backup()
        time.sleep(BACKUP_INTERVAL)  # yazma sıklığını sınırla


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
    if not any(c["id"] == 1 for c in cols):
        cols.insert(0, {"id": 1, "name": "Genel", "created_at": now_iso()})
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
    """Diskten okunan bir kaydı şemaya normalize eder; bozuksa None döner.

    Diskteki dosyalar (başka bir yerel süreç tarafından) kurcalanmış olabilir;
    özellikle `url` doğrudan arayüzde <a href> olarak kullanıldığından, kaydedilmiş
    her URL yeniden as_web_link denetiminden geçirilir.
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
        out["collection_name"] = name if isinstance(name, str) else "Genel"
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


def migrate_data_dir():
    """v3.0'da uygulama klasöründe kalan verileri %LOCALAPPDATA%'ya taşır."""
    if DATA_DIR == BASE_DIR:
        return
    for name in ("settings.json", "archive.json", "session_backup.json",
                 "recovery_pending.json", "copytracker.log"):
        old = os.path.join(BASE_DIR, name)
        new = os.path.join(DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                os.replace(old, new)
                log(f"{name} → {DATA_DIR} taşındı.")
            except OSError as e:
                log(f"{name} taşınamadı: {e}")


def migrate_legacy():
    """İlk v3 açılışında eski data.json içeriğini kayıpsız arşive taşır."""
    if os.path.exists(SETTINGS_FILE) or not os.path.exists(LEGACY_FILE):
        return
    d = _read_json(LEGACY_FILE)
    if not isinstance(d, dict):
        return
    cols = {c.get("id"): c.get("name", "Genel") for c in d.get("collections", [])
            if isinstance(c, dict)}
    old_cols = [c for c in d.get("collections", []) if isinstance(c, dict)
                and isinstance(c.get("id"), int) and isinstance(c.get("name"), str)]
    if old_cols:
        _settings["collections"] = old_cols
        if not any(c["id"] == 1 for c in old_cols):
            _settings["collections"].insert(0, {"id": 1, "name": "Genel", "created_at": now_iso()})
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
            "collection_name": cols.get(it.get("collection"), "Genel"),
        })
        _archive["next_aid"] += 1
        moved += 1
    try:
        os.replace(LEGACY_FILE, LEGACY_FILE + ".v2.bak")
    except OSError:
        pass
    if moved:
        save_archive()  # taşınanlar hemen diske yazılsın — RAM'de kalırsa kaybolur
        log(f"Eski data.json'dan {moved} öğe arşive taşındı (data.json.v2.bak yedeklendi).")


def check_recovery():
    """Önceki oturum çökmüşse gölge kopyayı kurtarma adayı olarak bekletir.

    Kullanıcı bekleyen kurtarmaya karar vermeden ikinci bir çökme yaşanırsa eski
    kayıtlar kaybolmasın diye iki dosya BİRLEŞTİRİLİR (üzerine yazılmaz).
    """
    global _recovery
    pending = _read_json(RECOVERY_FILE) if os.path.exists(RECOVERY_FILE) else None
    fresh = _read_json(BACKUP_FILE) if os.path.exists(BACKUP_FILE) else None

    merged = []
    for src in (pending, fresh):
        if isinstance(src, dict) and isinstance(src.get("items"), list):
            merged.extend(x for x in (_sanitize_entry(e, False) for e in src["items"]) if x)
    seen, unique = set(), []
    for it in merged:  # aynı metin iki dosyada varsa bir kez kurtarılsın
        key = (it["text"], it.get("collection"))
        if key not in seen:
            seen.add(key)
            unique.append(it)

    if unique:
        _recovery = unique
        try:
            _write_json(RECOVERY_FILE, {"items": unique, "saved_at": now_iso()})
        except OSError as e:
            log(f"Kurtarma dosyası yazılamadı: {e}")
        log(f"Önceki oturumdan {len(unique)} kurtarılabilir öğe bulundu.")
    else:
        try:
            os.remove(RECOVERY_FILE)
        except OSError:
            pass
    try:
        os.remove(BACKUP_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- link/filtre

def _host_of(url):
    try:
        netloc = urlparse(url).netloc
    except ValueError:
        return ""
    # Tarayıcı '\' ve userinfo'yu otorite sonlandırıcı sayar; aynısını biz de yapalım
    netloc = netloc.split("\\")[0].rsplit("@", 1)[-1]
    return netloc.split(":")[0].lower()


def as_web_link(text):
    """Metin gerçek/tam bir web sitesi linkiyse normalize URL döndürür, değilse None."""
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
    # Ters bölü ve userinfo, Python ile tarayıcı ayrıştırıcılarının farklı host
    # bulmasına yol açar (evil.com\.instagram.com filtreyi kandırabilir) — reddet.
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
    """Aktif yakalama filtresine göre bu kopya kaydedilmeli mi?"""
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
    return "Genel"


# ---------------------------------------------------------------- başlık çekme

def _is_public_host(host):
    """Host genel internete mi çözümleniyor? Loopback/özel/link-local ise False.

    Başlık çekimi kullanıcının kopyaladığı URL'ye istek attığı için, kötü niyetli
    bir sayfa panoya `http://10.0.0.1/...` yazdırıp iç ağ taraması yaptırabilir
    (SSRF). Tüm çözümlenen adresleri denetleyip özel aralıkları reddediyoruz.
    """
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


class _SafeRedirect(urllib_request.HTTPRedirectHandler):
    """Yönlendirmelerin de iç ağa sapmasını engeller."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if as_web_link(newurl) is None or not _is_public_host(_host_of(newurl)):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_title_pool = None  # main() içinde kurulur — iş parçacığı sayısı sınırlı kalsın


def _fetch_title(item_id, url):
    """Link öğesi için sayfa başlığını arka planda çeker (best-effort)."""
    try:
        if not _is_public_host(_host_of(url)):
            return  # iç ağ / loopback adresi — istek atma
        opener = urllib_request.build_opener(_SafeRedirect)
        req = urllib_request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CopyTracker/3.0"})
        with opener.open(req, timeout=6) as r:
            raw = r.read(131072)
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
        pass  # başlık süs; hata sessizce yutulur


# ---------------------------------------------------------------- öğe işlemleri

_last_text = None
_last_time = 0.0
_ignore_text = None
_ignore_until = 0.0


def suppress_next(text, secs=3.0):
    """Bu metnin önümüzdeki birkaç saniyedeki pano olayını yok say (kendi kopyamız)."""
    global _ignore_text, _ignore_until
    _ignore_text = text
    _ignore_until = time.time() + secs


def add_item(text):
    """Kopyayı RAM listesine ekler.

    Dönüş: ("new"|"dup", item) | ("skip"|"filtered", None)
    "dup": aynı metin aktif koleksiyonda zaten var → yeni kayıt açılmaz,
    kopya sayacı artar; tamamlanmışsa geri açılır (yeniden işlenecek demektir).
    """
    global _last_text, _last_time, _next_id
    now = time.time()
    if text == _last_text and (now - _last_time) < DEDUP_WINDOW:
        _last_time = now
        return "skip", None
    _last_text = text
    _last_time = now

    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n… (kırpıldı)"
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
        if len(_items) > MAX_ITEMS:  # sınırsız büyümeyi engelle
            for i, old in enumerate(_items):
                if not old.get("pinned"):
                    dropped = _items.pop(i)
                    log(f"Liste sınırı ({MAX_ITEMS}) aşıldı; en eski öğe atıldı: "
                        f"#{dropped['id']} {short(dropped['text'], 40)}")
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
    """Öğeleri RAM'den diske (arşive) taşır.

    Diske yazma başarısız olursa TÜM değişiklik geri alınır — öğeler aktif
    listede kalır; yarı yolda kaybolmaz.
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
                _items[:] = prev_items  # geri al
                _archive["items"] = prev_archive
                _archive["next_aid"] = prev_next_aid
                raise
    save_backup()
    return moved


def clear_items(cid):
    """Koleksiyonu temizler; sabitlenmiş (📌) öğelere dokunmaz."""
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
    """Arşivdeki öğeleri aktif koleksiyona geri getirir."""
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
    """Saklama süresine göre arşivden otomatik siler."""
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
        log(f"Arşivden {removed} öğe süresi dolduğu için silindi (kural: {r}).")
    return removed


# ------------------------------------------------- Windows'la birlikte başlatma

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CopyTracker"


def _startup_command():
    """Açılışta çalıştırılacak komut — konsolsuz pythonw tercih edilir."""
    script = os.path.join(BASE_DIR, "copytracker.py")
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


def set_startup(enabled):
    """Kayıt defterindeki Run anahtarını günceller. Hata mesajını döndürür (yoksa None)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _startup_command())
                log("Windows açılışında başlatma açıldı.")
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
                log("Windows açılışında başlatma kapatıldı.")
        return None
    except OSError as e:
        log(f"Açılışta başlatma ayarlanamadı: {e}")
        return str(e)


def announce_capture(enabled):
    log("Yakalama " + ("açıldı." if enabled else "durduruldu."))
    notify("📋 Yakalama " + ("açık" if enabled else "kapalı"),
           "Kopyalananlar kaydediliyor." if enabled else "Kopyalananlar artık kaydedilmiyor.")


def set_capture(enabled):
    """Tepsi menüsü / kısayol için: yakalamayı ayarlar, kaydeder ve bildirir."""
    with _lock:
        previous = _settings["capture_enabled"]
        _settings["capture_enabled"] = bool(enabled)
    try:
        save_settings()
    except StorageError:
        with _lock:
            _settings["capture_enabled"] = previous
        notify("⚠️ Ayar kaydedilemedi", "Yakalama durumu değiştirilemedi.")
        return
    announce_capture(enabled)


# ---------------------------------------------------------------- bildirimler
# Windows'un kendi toast bildirimleri kullanıcı ayarlarıyla kapatılabildiğinden
# bildirimler uygulamanın kendi küçük penceresiyle gösterilir.

_toasts = queue.Queue()
TOAST_W, TOAST_H = 344, 96
TOAST_SHOW_MS = 3200
TOAST_MAX_AGE = 8.0


def notify(title, msg, force=False):
    """Bildirim gösterir. force=True olanlar (ayar bildirimleri) hep gösterilir."""
    if not force and not _settings.get("notifications_enabled", True):
        return
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


def toast_thread():
    try:
        import tkinter as tk
    except Exception as e:
        log(f"tkinter bulunamadı, bildirimler devre dışı: {e}")
        return
    while True:
        title, msg, ts = _toasts.get()
        if time.time() - ts > TOAST_MAX_AGE:
            continue
        try:
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.0)
            left, top, right, bottom = _work_area()
            root.geometry(f"{TOAST_W}x{TOAST_H}+{right - TOAST_W - 16}+{bottom - TOAST_H - 16}")
            root.configure(bg="#202124")
            tk.Frame(root, bg="#6c8cff", width=4).pack(side="left", fill="y")
            box = tk.Frame(root, bg="#202124")
            box.pack(side="left", fill="both", expand=True, padx=14, pady=10)
            tk.Label(box, text=title, bg="#202124", fg="#ffffff",
                     font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
                     wraplength=TOAST_W - 50).pack(fill="x")
            tk.Label(box, text=msg, bg="#202124", fg="#c6cbd4",
                     font=("Segoe UI", 9), anchor="w", justify="left",
                     wraplength=TOAST_W - 50).pack(fill="x", pady=(3, 0))

            def open_list(_e=None):
                try:
                    webbrowser.open(URL)
                finally:
                    root.destroy()

            root.bind_all("<Button-1>", open_list)
            root.deiconify()
            root.update_idletasks()
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            _round_corners(hwnd)

            def fade_in(step=0):
                if step > 10:
                    return
                root.attributes("-alpha", step / 10)
                root.after(15, lambda: fade_in(step + 1))

            def fade_out(step=10):
                if step < 0:
                    root.destroy()
                    return
                root.attributes("-alpha", step / 10)
                root.after(20, lambda: fade_out(step - 1))

            fade_in()
            root.after(TOAST_SHOW_MS, fade_out)
            root.mainloop()
        except Exception as e:
            log(f"Bildirim penceresi hatası: {e}")


# ------------------------------------------------- Win32 (ctypes) tanımları

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


# ---------------------------------------------------------------- pano okuma

def read_clipboard():
    """(durum, metin): 'ok'|'empty'|'excluded'|'no_text'|'error'."""
    for _ in range(30):  # ~1,5 sn toplam bütçe
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
    log("Pano ~1,5 sn boyunca okunamadı — bu kopya kaydedilemedi.")
    return "error", None


_events = queue.Queue()
_last_nontext = 0.0
_last_blind_warn = 0.0


def warn_if_filter_blind():
    """'Özel alan adları' seçili ama liste boşsa hiçbir şey yakalanamaz.

    Bu sessiz bir çıkmazdır: arayüz 'Yakalama açık' der ama tek bir kopya bile
    kaydedilmez. Kullanıcıyı en fazla dakikada bir uyar.
    """
    global _last_blind_warn
    with _lock:
        blind = (_settings["filter_mode"] == "custom"
                 and not [d for d in _settings["custom_domains"] if d.strip()])
    if not blind:
        return
    now = time.time()
    if now - _last_blind_warn < 60:
        return
    _last_blind_warn = now
    log("UYARI: filtre 'özel alan adları' ama liste boş — hiçbir şey kaydedilmiyor.")
    notify("⚠️ Hiçbir şey kaydedilmiyor",
           "Filtre 'Özel alan adları' ama liste boş. Alan adı ekle ya da 'Tümü' seç.")


def clipboard_worker():
    global _last_nontext
    while True:
        _events.get()
        try:
            if not _settings["capture_enabled"]:
                continue  # yakalama kapalı — olayı sessizce yut
            status, text = read_clipboard()
            if status == "ok":
                if not text.strip():
                    continue
                if _ignore_text is not None and text == _ignore_text and time.time() < _ignore_until:
                    continue  # arayüzün "Kopyala" düğmesinden gelen kendi kopyamız
                kind, item = add_item(text)
                if kind in ("skip", "filtered"):
                    if kind == "filtered":
                        log(f"Filtre nedeniyle kaydedilmedi ({len(text)} karakter).")
                        warn_if_filter_blind()
                    continue
                # Log'a pano İÇERİĞİ yazılmaz (log dosyası paylaşılabilir/yedeklenebilir);
                # sadece sayısal üstveri tutulur. İçerik yalnızca ekrandaki bildirimde.
                col = collection_name(item["collection"])
                where = f" → {col}" if col != "Genel" else ""
                if kind == "dup":
                    log(f"#{item['id']} tekrar kopyalandı (×{item['copies']}).")
                    notify("♻️ Zaten listede", f"Tekrar kopyalandı (×{item['copies']}): {short(text, 80)}")
                elif item["is_link"]:
                    log(f"#{item['id']} link kaydedildi ({col}, {_host_of(item['url'])})")
                    notify("🔗 Web sitesi linki kopyalandı",
                           f"Kaydedildi{where}: {short(item['url'])}")
                else:
                    log(f"#{item['id']} metin kaydedildi ({col}, {len(text)} karakter)")
                    notify(f"📋 Kopyalandı ve kaydedildi{where}", short(text))
            elif status == "no_text":
                now = time.time()
                if now - _last_nontext > DEDUP_WINDOW:
                    _last_nontext = now
                    notify("📎 Kopyalandı (metin değil)", "Resim/dosya içerikleri kaydedilmez.")
        except Exception as e:
            log(f"Pano işleme hatası: {e}")


# ------------------------------------------------- gizli pencere + tepsi + kısayol

_hwnd = None
_tray_data = None


def _tray_add(hwnd):
    global _tray_data
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
    nid.szTip = "CopyTracker — pano gelen kutusu"
    if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        _tray_data = nid
    else:
        log("Tepsi simgesi eklenemedi (kritik değil).")


def _tray_remove():
    if _tray_data is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_tray_data))


def _tray_menu(hwnd):
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    try:
        toggle_text = "⏸ Yakalamayı Durdur" if _settings["capture_enabled"] else "▶ Yakalamayı Başlat"
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_OPEN, "📋 Listeyi Aç")
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_TOGGLE, toggle_text)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_EXIT, "✕ Çıkış")
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
        user32.PostQuitMessage(0)  # mesaj döngüsünü bitir → temiz kapanış
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


_wnd_proc_ref = WNDPROC(_wnd_proc)  # GC'ye kapılmasın


def run_listener():
    """Gizli pencere: pano dinleyicisi + tepsi simgesi + global kısayollar (ana iş parçacığı)."""
    global _hwnd
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wnd_proc_ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "CopyTrackerListener"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Tepsi geri çağrıları mesaj-only pencerelere gelmediği için normal gizli pencere
    _hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP_NAME, 0,
                                   0, 0, 0, 0, None, None, wc.hInstance, None)
    if not _hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.AddClipboardFormatListener(_hwnd):
        raise ctypes.WinError(ctypes.get_last_error())
    _tray_add(_hwnd)
    if not user32.RegisterHotKey(_hwnd, HK_TOGGLE, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4B):
        log("Ctrl+Alt+K kısayolu alınamadı (başka uygulama kullanıyor olabilir).")
    if not user32.RegisterHotKey(_hwnd, HK_OPEN, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4C):
        log("Ctrl+Alt+L kısayolu alınamadı (başka uygulama kullanıyor olabilir).")
    log("Pano dinleyicisi hazır — tepsi simgesi ve kısayollar aktif (Ctrl+Alt+K / Ctrl+Alt+L).")
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


# ---------------------------------------------------------------- web arayüzü

app = Flask(__name__)

ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1",
                 f"[::1]:{PORT}", "[::1]"}


@app.before_request
def request_guard():
    """DNS rebinding + CSRF koruması.

    Host denetimi TÜM isteklere uygulanır: kötü niyetli bir site alan adını
    127.0.0.1'e yeniden çözümleyip (DNS rebinding) sayfayı bizimle aynı köken
    haline getirse bile Host başlığı kendi alan adını taşır ve istek reddedilir.
    Böylece GET uçları da (pano geçmişini döndürenler) korunmuş olur.
    """
    if (request.host or "").lower() not in ALLOWED_HOSTS:
        return jsonify({"ok": False, "error": "geçersiz host"}), 403
    if request.method == "POST":
        if request.headers.get("X-CopyTracker") != "1":
            return jsonify({"ok": False, "error": "geçersiz istek"}), 403
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return jsonify({"ok": False, "error": "geçersiz origin"}), 403


@app.after_request
def security_headers(resp):
    """Çerçeveleme (clickjacking) ve içerik türü tahminine karşı temel başlıklar."""
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


@app.get("/api/items")
def api_items():
    with _lock:
        return jsonify({
            "started": _started,
            "capture_enabled": _settings["capture_enabled"],
            "filter_mode": _settings["filter_mode"],
            "custom_domains": _settings["custom_domains"],
            "retention": _settings["retention"],
            "active_collection": _settings["active_collection"],
            "collections": _settings["collections"],
            "items": _items,
            "archive_count": len(_archive["items"]),
            "recovery_count": len(_recovery) if _recovery else 0,
            "qr_available": HAS_QR,
            "data_dir": DATA_DIR,
            "notifications_enabled": _settings["notifications_enabled"],
            "startup_enabled": get_startup(),
        })


@app.get("/api/archive")
def api_archive():
    with _lock:
        return jsonify({"items": _archive["items"], "retention": _settings["retention"]})


@app.post("/api/settings")
def api_settings():
    """Ayarları günceller. Tüm alanlar ÖNCE doğrulanır, sonra uygulanır —
    böylece bir alan geçersizse hiçbiri yarım uygulanmış olmaz."""
    body = request.get_json(silent=True) or {}
    updates, changed = {}, []

    if "filter_mode" in body:
        if body["filter_mode"] not in FILTER_MODES:
            return jsonify({"ok": False, "error": "geçersiz mod"}), 400
        updates["filter_mode"] = body["filter_mode"]
        changed.append("filtre=" + body["filter_mode"])
    if "custom_domains" in body:
        doms = body["custom_domains"]
        if not isinstance(doms, list):
            return jsonify({"ok": False, "error": "geçersiz alan adı listesi"}), 400
        updates["custom_domains"] = [_norm_domain(str(s))[:100] for s in doms if str(s).strip()][:50]
        changed.append("özel alan adları")
    if "retention" in body:
        if body["retention"] not in RETENTIONS:
            return jsonify({"ok": False, "error": "geçersiz saklama süresi"}), 400
        updates["retention"] = body["retention"]
        changed.append("saklama=" + body["retention"])

    if "notifications_enabled" in body:
        updates["notifications_enabled"] = bool(body["notifications_enabled"])
        changed.append("bildirimler=" + ("açık" if body["notifications_enabled"] else "kapalı"))

    if "startup_enabled" in body:  # kayıt defteri; ayar dosyasında tutulmaz
        err = set_startup(bool(body["startup_enabled"]))
        if err:
            return jsonify({"ok": False, "error": f"açılışta başlatma ayarlanamadı: {err}"}), 500
        changed.append("açılışta başlat=" + ("açık" if body["startup_enabled"] else "kapalı"))

    with _lock:
        previous = {k: _settings[k] for k in updates}
        _settings.update(updates)
    if "notifications_enabled" in body:
        notify("🔔 Bildirimler " + ("açık" if body["notifications_enabled"] else "kapalı"),
               "Kopyalama bildirimleri gösterilecek." if body["notifications_enabled"]
               else "Kopyalama bildirimleri artık gösterilmeyecek.", force=True)
    if "capture_enabled" in body:
        with _lock:
            previous["capture_enabled"] = _settings["capture_enabled"]
            _settings["capture_enabled"] = bool(body["capture_enabled"])
        changed.append("yakalama=" + ("açık" if body["capture_enabled"] else "kapalı"))
    try:
        save_settings()
    except StorageError as e:
        with _lock:
            _settings.update(previous)  # diske yazılamadıysa RAM'i de geri al
        return jsonify({"ok": False, "error": f"ayarlar kaydedilemedi: {e}"}), 500
    if "capture_enabled" in body:
        announce_capture(_settings["capture_enabled"])
    if changed:
        log("Ayar değişti: " + ", ".join(changed))
    return jsonify({"ok": True})


@app.post("/api/collections")
def api_collections_create():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:40]
    if not name:
        return jsonify({"ok": False, "error": "isim gerekli"}), 400
    with _lock:
        if any(c["name"].lower() == name.lower() for c in _settings["collections"]):
            return jsonify({"ok": False, "error": "Bu isimde bir koleksiyon zaten var"}), 409
        c = {"id": _settings["next_collection_id"], "name": name, "created_at": now_iso()}
        _settings["next_collection_id"] += 1
        _settings["collections"].append(c)
        _settings["active_collection"] = c["id"]
        resp = dict(c)
    save_settings()
    log(f"Koleksiyon oluşturuldu ve aktif edildi: {name}")
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
            return jsonify({"ok": False, "error": "bulunamadı"}), 404
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
        return jsonify({"ok": False, "error": "Genel koleksiyonu silinemez"}), 400
    with _lock:
        if not any(c["id"] == cid for c in _settings["collections"]):
            return jsonify({"ok": False, "error": "bulunamadı"}), 404
        _settings["collections"] = [c for c in _settings["collections"] if c["id"] != cid]
        for it in _items:
            if it["collection"] == cid:
                it["collection"] = 1  # öğeler Genel'e taşınır
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
    for v in raw[:MAX_ITEMS]:  # üst sınır: tek istekte tüm listeden fazlası anlamsız
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
        return jsonify({"ok": False, "error": "bulunamadı"}), 404
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
        return jsonify({"ok": False, "error": "bulunamadı"}), 404
    return jsonify({"ok": True, "item": it})


@app.post("/api/delete")
def api_delete():
    body = request.get_json(silent=True) or {}
    ids = _ids_from(body)
    if not ids:
        return jsonify({"ok": False, "error": "id gerekli"}), 400
    removed = delete_items(ids)
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/archive")
def api_archive_move():
    body = request.get_json(silent=True) or {}
    ids = _ids_from(body)
    if not ids:
        return jsonify({"ok": False, "error": "id gerekli"}), 400
    try:
        moved = archive_items(ids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"arşive yazılamadı: {e}"}), 500
    return jsonify({"ok": True, "moved": moved})


@app.post("/api/archive/delete")
def api_archive_delete():
    body = request.get_json(silent=True) or {}
    aids = _ids_from(body, "aids")
    if not aids:
        return jsonify({"ok": False, "error": "aid gerekli"}), 400
    try:
        removed = archive_delete(aids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"arşiv güncellenemedi: {e}"}), 500
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/archive/restore")
def api_archive_restore():
    body = request.get_json(silent=True) or {}
    aids = _ids_from(body, "aids")
    if not aids:
        return jsonify({"ok": False, "error": "aid gerekli"}), 400
    try:
        restored = archive_restore(aids)
    except StorageError as e:
        return jsonify({"ok": False, "error": f"arşiv güncellenemedi: {e}"}), 500
    return jsonify({"ok": True, "restored": restored})


@app.post("/api/quit")
def api_quit():
    """Temiz kapanış — stop.bat bunu kullanır ki gölge kopya silinsin
    ve bir sonraki açılışta sahte 'çökme' uyarısı çıkmasın."""
    if _hwnd:
        user32.PostMessageW(_hwnd, WM_CLOSE, 0, 0)
    else:
        user32.PostThreadMessageW(_main_tid or 0, WM_QUIT, 0, 0)
    log("Kapanış isteği alındı (stop.bat / arayüz).")
    return jsonify({"ok": True})


@app.post("/api/clear")
def api_clear():
    body = request.get_json(silent=True) or {}
    cid = body.get("collection")
    if not isinstance(cid, int):
        return jsonify({"ok": False, "error": "koleksiyon id gerekli"}), 400
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
        with _lock:  # kontrol de kilit içinde: çift tıklama iki kez geri yüklemesin
            pending, _recovery = _recovery, None
            if pending is None:
                return jsonify({"ok": False, "error": "kurtarılacak öğe yok"}), 404
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
        log(f"Önceki oturumdan {count} öğe geri yüklendi.")
        return jsonify({"ok": True, "restored": count})
    if action == "discard":
        _recovery = None
        try:
            os.remove(RECOVERY_FILE)
        except OSError:
            pass
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "geçersiz eylem"}), 400


@app.get("/api/qr")
def api_qr():
    if not HAS_QR:
        return jsonify({"ok": False, "error": "qrcode paketi kurulu değil"}), 501
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
        return jsonify({"ok": False, "error": "bulunamadı"}), 404
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    return Response(img.to_string(), mimetype="image/svg+xml")


def run_flask():
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


# ---------------------------------------------------------------- bakım görevi

def maintenance_thread():
    """Dakikada bir: arşiv saklama süresi denetimi."""
    while True:
        time.sleep(60)
        try:
            purge_archive()
        except Exception as e:
            log(f"Arşiv temizliği hatası: {e}")


# ---------------------------------------------------------------------- main

def _copytracker_on_port():
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
        if _copytracker_on_port():
            log("CopyTracker zaten çalışıyor — tarayıcıda liste açılıyor.")
            webbrowser.open(URL)
        else:
            log(f"HATA: {PORT} portu başka bir uygulama tarafından kullanılıyor; "
                f"CopyTracker başlatılamadı. O uygulamayı kapatıp yeniden deneyin.")
        return
    probe.close()

    global _title_pool
    migrate_data_dir()
    try:  # bozuk/kurcalanmış bir dosya uygulamayı kalıcı olarak açılmaz yapmasın
        migrate_legacy()
        if not load_settings():
            save_settings()  # ilk çalıştırma — varsayılanları yaz
        load_archive()
        check_recovery()
        purge_archive()
    except Exception as e:
        log(f"HATA: kayıtlı veriler okunamadı ({e}). Bozuk dosyalar karantinaya alınıyor.")
        for path in (SETTINGS_FILE, ARCHIVE_FILE, RECOVERY_FILE, BACKUP_FILE):
            if os.path.exists(path):
                try:
                    os.replace(path, path + ".corrupt")
                except OSError:
                    pass
        log("Uygulama varsayılan ayarlarla başlıyor (.corrupt yedekleri korundu).")

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
    log(f"CopyTracker v3 başladı. Web arayüzü: {URL}  (aktif liste RAM'de, arşiv diskte)")
    log(f"Veri klasörü: {DATA_DIR}")
    if not HAS_QR:
        log("Not: 'qrcode' paketi yok — QR özelliği kapalı (pip install qrcode).")
    notify("📋 CopyTracker başladı", f"Kopyaladığın her şey listeye düşecek. Liste: {URL}")
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
        delete_backup()  # temiz kapanış — RAM listesi bilerek uçucu
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        log("CopyTracker durdu.")


if __name__ == "__main__":
    main()
