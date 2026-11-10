#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin.py — لوحة تحكم بأسلوب Viloud لمحطة koyeb-tv — بايثون قياسي فقط (لا مكتبات خارجية).

المسؤوليات:
- واجهة برمجية JSON تحت /api/ (nginx يحوّل طلبات /api/ إلى هنا)
- مكتبة فيديو: إضافة عبر رابط مباشر → تنزيل إلى MEDIA_DIR → فحص المدة/الترميز
  (mp4_info بدون ffmpeg) → جاهزة للبث
- قنوات: "vod" (قائمة تشغيل تتكرر 24/7 عبر nginx-vod) أو "restream" (رابط خارجي)
- مجدول دوري يعيد توليد خرائط البث كل SCHEDULE_INTERVAL ثانية
- عند أي تعديل: توليد مواقع nginx (gen_locations) ثم nginx -s reload

التشغيل:
    python3 admin.py --bootstrap   # تهيئة أولية ثم خروج (يستدعيها entrypoint)
    python3 admin.py               # الخدمة الكاملة: API + مجدول + تنزيلات

المتغيرات:
    DATA_DIR          مجلد البيانات (افتراضي /data)
    MEDIA_DIR         مجلد الفيديوهات (افتراضي /media)
    MAPS_DIR          مجلد خرائط البث (افتراضي /var/vod/maps)
    ADMIN_PORT        منفذ الواجهة الداخلي (افتراضي 9000 — خلف nginx فقط)
    ADMIN_PASSWORD    كلمة مرور اللوحة (افتراضي admin123 — فارغة = بدون حماية)
    APP_NAME          اسم المحطة (افتراضي قناتي)
    MAX_VIDEO_MB      الحد الأقصى لحجم الفيديو الواحد (افتراضي 1024)
    MAX_LIBRARY_MB    الحد الأقصى لحجم المكتبة كاملة (افتراضي 2048)
    SEED_JSON         بذرة بيانات JSON لاستعادة القنوات والمكتبة عند الإقلاع
"""

import base64
import hashlib
import hmac
import http.cookies
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_schedule  # noqa: E402
import gen_locations   # noqa: E402
from mp4_info import probe, probe_url  # noqa: E402

# ------------------------------------------------------------------
# الإعدادات
# ------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
MAPS_DIR = os.environ.get("MAPS_DIR", "/var/vod/maps")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
APP_NAME = os.environ.get("APP_NAME", "قناتي")
NGINX_BIN = os.environ.get("NGINX_BIN", "/opt/nginx/sbin/nginx")
WWW_DIR = os.environ.get("WWW_DIR", "/var/www")
MAX_VIDEO_MB = int(os.environ.get("MAX_VIDEO_MB", "1024"))
# افتراضي 1500 بدل 2048: خطة Koyeb المجانية قرصها 2GB كاملة (الصورة + النظام تأكل منها)
MAX_LIBRARY_MB = int(os.environ.get("MAX_LIBRARY_MB", "1500"))
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", "300"))
SCHEDULE_INTERVAL = int(os.environ.get("SCHEDULE_INTERVAL", "15"))
SEED_JSON = os.environ.get("SEED_JSON", "")

VIDEOS_FILE = os.path.join(DATA_DIR, "videos.json")
CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# ---------- الاستوديو (الشعار + الشريط النصي) ----------
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LOGO_NAME_RE = re.compile(r"^logo\.(png|jpg|webp)$")
DEFAULT_SETTINGS = {
    "logo": {"enabled": False, "file": "", "position": "top-left",
             "width_pct": 14, "opacity": 0.9},
    "ticker": {"enabled": False, "text": "", "color": "#ffffff",
               "bg": "#c0392b", "speed": 70, "now_playing": True},
}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
RESERVED_IDS = {"api", "admin", "watch", "embed", "map", "live", "restream",
                "config", "healthz", "static", "www"}
ALLOWED_EXT = (".mp4", ".mkv", ".mov", ".m4v")
# الفحص عن بُعد يعتمد على قراءة عشوائية — MP4 فقط (لا MKV عن بُعد عمداً)
REMOTE_EXT = (".mp4", ".mov", ".m4v")
# روابط «عن بُعد» يجب أن تكون نصاً لاتينياً مطبوعاً نظيفاً (تُمرّر داخل URI فرعي لـ nginx)
REMOTE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")
CHUNK = 256 * 1024
PROGRESS_STEP = 8 * 1024 * 1024   # تحديث التقدم كل 8MB

_LOCK = threading.RLock()
_DOWNLOADQ = queue.Queue()


def log(msg):
    print("[admin] %s" % msg, flush=True)


# ------------------------------------------------------------------
# تخزين البيانات (كتابة ذرية + قفل واحد)
# ------------------------------------------------------------------
def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_videos():
    try:
        with open(VIDEOS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_videos(videos):
    _atomic_write(VIDEOS_FILE, videos)


def load_channels():
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_channels(channels):
    _atomic_write(CHANNELS_FILE, channels)


# ------------------------------------------------------------------
# الاستوديو: إعدادات الشعار والشريط النصي (تُعرض في مشغل الموقع)
# ------------------------------------------------------------------
def _clamp(x, lo, hi, dflt):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return dflt
    return max(lo, min(hi, x))


def sanitize_settings(raw, prev=None):
    """ينظف إعدادات الاستوديو القادمة من اللوحة ويعيد قيماً آمنة فقط.
    القيمة غير الصالحة لا تُمس القيمة المحفوظة سابقاً (prev) — والافتراضي آخر الحلول."""
    if not isinstance(raw, dict):
        raw = {}
    prev = prev if isinstance(prev, dict) else {}
    out = {
        "logo": dict(DEFAULT_SETTINGS["logo"], **(prev.get("logo") or {})),
        "ticker": dict(DEFAULT_SETTINGS["ticker"], **(prev.get("ticker") or {})),
    }
    logo = raw.get("logo") or {}
    ticker = raw.get("ticker") or {}

    out["logo"]["enabled"] = bool(logo.get("enabled", out["logo"]["enabled"]))
    if logo.get("position") in LOGO_POSITIONS:
        out["logo"]["position"] = logo["position"]
    out["logo"]["width_pct"] = int(_clamp(logo.get("width_pct"), 5, 40,
                                           out["logo"]["width_pct"]))
    out["logo"]["opacity"] = round(_clamp(logo.get("opacity"), 0.05, 1,
                                           out["logo"]["opacity"]), 2)
    if "file" in logo:
        fname = str(logo.get("file") or "")
        if LOGO_NAME_RE.match(fname):
            out["logo"]["file"] = fname

    out["ticker"]["enabled"] = bool(ticker.get("enabled", out["ticker"]["enabled"]))
    if "text" in ticker:
        out["ticker"]["text"] = str(ticker.get("text") or "")[:300]
    for key in ("color", "bg"):
        val = str(ticker.get(key) or "")
        if HEX_COLOR_RE.match(val):
            out["ticker"][key] = val
    out["ticker"]["speed"] = int(_clamp(ticker.get("speed"), 20, 250,
                                         out["ticker"]["speed"]))
    out["ticker"]["now_playing"] = bool(ticker.get("now_playing",
                                                    out["ticker"]["now_playing"]))
    return out


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            return sanitize_settings(json.load(fh))
    except Exception:  # noqa: BLE001
        return sanitize_settings({})


def save_settings(s):
    _atomic_write(SETTINGS_FILE, s)


def logo_url_of(s):
    f = (s.get("logo") or {}).get("file") or ""
    if not LOGO_NAME_RE.match(f):
        return None
    try:
        v = int(os.path.getmtime(os.path.join(WWW_DIR, f)))
    except OSError:
        v = int(time.time())
    return "/%s?v=%d" % (f, v)


def _logo_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


def save_logo_image(data_b64):
    """يفك شعاراً مرفوعاً (base64) ويتحقق منه ويكتبه في مجلد الموقع. يعيد (اسم الملف، خطأ)."""
    try:
        raw = base64.b64decode(data_b64 or "")
    except Exception:  # noqa: BLE001
        return None, "بيانات الصورة غير صالحة (base64)"
    if len(raw) < 64:
        return None, "الصورة فارغة أو تالفة"
    if len(raw) > LOGO_MAX_BYTES:
        return None, "حجم الصورة يتجاوز الحد 2MB — صغّرها وأعد المحاولة"
    ext = _logo_type(raw)
    if not ext:
        return None, "صيغة غير مدعومة — استخدم PNG (شفاف مستحسن) أو JPG أو WEBP"
    try:
        for old in os.listdir(WWW_DIR):
            if old.startswith("logo."):
                try:
                    os.remove(os.path.join(WWW_DIR, old))
                except OSError:
                    pass
        name = "logo" + ext
        with open(os.path.join(WWW_DIR, name), "wb") as fh:
            fh.write(raw)
    except OSError as exc:
        return None, "تعذر حفظ الملف: %s" % exc
    s = load_settings()
    s["logo"]["file"] = name
    s["logo"]["enabled"] = True
    save_settings(s)
    log("شعار جديد: %s (%.1fKB)" % (name, len(raw) / 1024.0))
    return name, None


def delete_logo():
    for old in os.listdir(WWW_DIR) if os.path.isdir(WWW_DIR) else []:
        if old.startswith("logo."):
            try:
                os.remove(os.path.join(WWW_DIR, old))
            except OSError:
                pass
    s = load_settings()
    s["logo"]["file"] = ""
    s["logo"]["enabled"] = False
    save_settings(s)
    log("حُذف الشعار")


# ------------------------------------------------------------------
# المصادقة (كوكي موقّعة HMAC مشتقة من كلمة المرور)
# ------------------------------------------------------------------
def auth_enabled():
    return bool(ADMIN_PASSWORD.strip())


def make_token():
    return hmac.new(ADMIN_PASSWORD.encode("utf-8"),
                    b"koyeb-tv-admin-v1", hashlib.sha256).hexdigest()


def check_password(password):
    return hmac.compare_digest((password or "").encode("utf-8"),
                               ADMIN_PASSWORD.encode("utf-8"))


# ------------------------------------------------------------------
# مكتبة الفيديو
# ------------------------------------------------------------------
def ext_from_url(url):
    path = (url or "").split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in ALLOWED_EXT else ".mp4"


def library_usage_bytes(videos):
    # فيديوهات «عن بُعد» لا تشغل قرص المحطة — لا تدخل في الحساب
    return sum(int(v.get("size_bytes") or 0) for v in videos.values()
               if v.get("storage") != "remote")


def free_disk_mb():
    """المساحة الحرة على قرص مجلد الفيديو بالميغابايت، أو None إن تعذر القياس."""
    try:
        st = os.statvfs(MEDIA_DIR)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        return None


def new_video_id(videos):
    while True:
        vid = "v_" + os.urandom(4).hex()
        if vid not in videos:
            return vid


def add_video(name, url, storage="download"):
    """يضيف فيديو للمكتبة. وضعان:
    - download: تنزيل إلى قرص المحطة (الافتراضي) — طابور تنزيل + فحص بعده.
    - remote: بث مباشر من الرابط بطلبات Range (بلا حد للقرص) — فحص فوري بالطلبات.
    يعيد (entry, error)."""
    url = (url or "").strip()
    name = (name or "").strip() or None
    storage = storage if storage in ("download", "remote") else "download"
    if not re.match(r"^https?://", url):
        return None, "الرابط غير صالح — يجب أن يبدأ بـ http:// أو https://"
    if len(url) > 2048:
        return None, "الرابط طويل جداً"

    # روابط البث (m3u8/ts) ليست ملفات تُنزَّل إلى المكتبة — تُدار كقنوات خارجية
    low = url.split("?", 1)[0].split("#", 1)[0].lower()
    if low.endswith((".m3u8", ".m3u", ".ts", ".m2ts", ".mts")):
        return None, ("رابط m3u8/ts يُضاف من قسم «القنوات» كقناة خارجية — "
                      "وليس في مكتبة الفيديو")
    if storage == "remote" and low.endswith((".mkv", ".avi", ".webm", ".flv", ".wmv")):
        return None, ("وضع «عن بُعد» يدعم MP4/MOV/M4V فقط — للـ MKV استخدم التنزيل "
                      "إلى المحطة أو أعد تغليفه إلى MP4")
    if storage == "download" and low.endswith((".mkv", ".avi", ".webm", ".flv", ".wmv")):
        return None, ("هذه الصيغة غير مدعومة في المكتبة بدون ffmpeg — "
                      "استخدم رابط MP4 مباشر بترميز H.264/AAC")

    with _LOCK:
        videos = load_videos()
        if storage == "download":
            usage = library_usage_bytes(videos)
            if usage >= MAX_LIBRARY_MB * 1024 * 1024:
                return None, ("المكتبة ممتلئة (الحد %dMB) — احذف فيديوهات لم تعد بحاجة إليها، "
                              "أو ارفع MAX_LIBRARY_MB، أو استخدم وضع «عن بُعد» بلا حد"
                              % MAX_LIBRARY_MB)
            free = free_disk_mb()
            if free is not None and free < MIN_FREE_DISK_MB:
                return None, ("مساحة القرص الحرة %dMB فقط (الحد الأدنى %dMB) — "
                              "احذف فيديوهات أو استخدم وضع «عن بُعد»"
                              % (free, MIN_FREE_DISK_MB))
        vid = new_video_id(videos)
        ext = ext_from_url(url)

    if storage == "remote":
        # تحقق صارم من شكل الرابط: سيُمرّر داخل URI طلب فرعي في nginx
        if not REMOTE_URL_RE.match(url):
            return None, ("رابط «عن بُعد» يحتوي محارف غير مدعومة — "
                          "استخدم رابطاً مباشراً بسيطاً ينتهي بـ .mp4")
        if not low.endswith(REMOTE_EXT):
            return None, "وضع «عن بُعد» يحتاج رابطاً ينتهي بـ .mp4 أو .mov أو .m4v"
        # فحص فوري عبر الشبكة (بضع طلبات Range صغيرة — ثوانٍ)
        try:
            info = probe_url(url)
        except Exception as exc:  # noqa: BLE001
            return None, "فشل الفحص عن بُعد: %s" % exc
        problem = build_schedule.codec_problem({
            "video": info["video"], "audio": info["audio"],
            "duration_ms": info["duration_ms"],
        })
        if problem:
            return None, problem

    with _LOCK:
        videos = load_videos()
        if vid in videos:  # نادر: سباق مع إضافة أخرى
            vid = new_video_id(videos)
        if not name:
            base = os.path.splitext(os.path.basename(url.split("?", 1)[0]))[0]
            name = base or ("فيديو %s" % vid[-4:])
        entry = {
            "id": vid, "name": name[:120], "url": url, "ext": ext,
            "file": os.path.join(MEDIA_DIR, vid + ext) if storage == "download" else "",
            "status": "queued" if storage == "download" else "ready",
            "error": None,
            "size_bytes": 0, "done_bytes": 0, "duration_ms": 0,
            "video": None, "audio": None,
            "storage": storage,
            "source": "url", "added_at": int(time.time()),
        }
        if storage == "remote":
            entry.update(size_bytes=0, duration_ms=info["duration_ms"],
                         video=info["video"], audio=info["audio"])
        videos[vid] = entry
        save_videos(videos)
    if storage == "download":
        _DOWNLOADQ.put(vid)
        log("فيديو جديد في الطابور: %s (%s)" % (name, url[:80]))
    else:
        log("فيديو عن بُعد جاهز: %s (%.0f ث — بلا تنزيل)" % (name, info["duration_ms"] / 1000.0))
    return entry, None


def _reprobe_remote(vid):
    """إعادة فحص فيديو «عن بُعد» من رابطه (بعد الاستيراد أو الإقلاع ببيانات ناقصة)."""
    with _LOCK:
        videos = load_videos()
        entry = videos.get(vid)
        if not entry:
            return
        url = entry.get("url") or ""
        entry["status"] = "downloading"  # «جارٍ الفحص» في الواجهة
        entry["error"] = None
        save_videos(videos)
    try:
        info = probe_url(url)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            vids = load_videos()
            if vid in vids:
                vids[vid]["status"] = "error"
                vids[vid]["error"] = "فشل الفحص عن بُعد: %s" % exc
                save_videos(vids)
        log("فشل فحص عن بُعد %s: %s" % (vid, exc))
        return
    with _LOCK:
        vids = load_videos()
        if vid not in vids:
            return
        e = vids[vid]
        e["duration_ms"] = info["duration_ms"]
        e["video"] = info["video"]
        e["audio"] = info["audio"]
        problem = build_schedule.codec_problem(e)
        if problem:
            e["status"] = "error"
            e["error"] = problem
        else:
            e["status"] = "ready"
            e["error"] = None
        save_videos(vids)
    log("فيديو عن بُعد جاهز: %s (%.0f ث)" % (e["name"], info["duration_ms"] / 1000.0))
    try:
        build_schedule.run_once()
    except Exception as exc:  # noqa: BLE001
        log("تحذير: تعذر تحديث الخرائط: %s" % exc)


def process_download(vid):
    """معالجة عنصر واحد من الطابور: فحص عن بُعد (وضع remote) أو تنزيل + فحص."""
    with _LOCK:
        videos = load_videos()
        entry = videos.get(vid)
        if not entry:
            return
        if entry.get("storage") == "remote":
            _reprobe_remote(vid)
            return
        url = entry.get("url")
        if not url:
            entry["status"] = "error"
            entry["error"] = "لا يوجد رابط لهذا العنصر (ملف مدمج في الصورة)"
            save_videos(videos)
            return
        entry["status"] = "downloading"
        entry["error"] = None
        entry["done_bytes"] = 0
        save_videos(videos)
        dest = entry["file"]

    cap = MAX_VIDEO_MB * 1024 * 1024
    try:
        # حارس القرص: لا نبدأ التنزيل إذا كانت المساحة الحررة خطرة
        free = free_disk_mb()
        if free is not None and free < MIN_FREE_DISK_MB:
            raise ValueError("مساحة القرص الحرة %dMB فقط (الحد الأدنى %dMB) — "
                             "احذف فيديوهات أو استخدم وضع «عن بُعد»"
                             % (free, MIN_FREE_DISK_MB))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) koyeb-tv/1.0",
            "Accept": "*/*",
        })
        done = 0
        last_save = 0
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else 0
            if total and total > cap:
                raise ValueError("حجم الملف %.0fMB يتجاوز الحد MAX_VIDEO_MB=%dMB"
                                 % (total / 1e6, MAX_VIDEO_MB))
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if done > cap:
                        fh.close()
                        os.remove(tmp)
                        raise ValueError("تجاوز الحد MAX_VIDEO_MB=%dMB أثناء التنزيل"
                                         % MAX_VIDEO_MB)
                    if done - last_save >= PROGRESS_STEP:
                        last_save = done
                        with _LOCK:
                            vids = load_videos()
                            if vid in vids:
                                vids[vid]["done_bytes"] = done
                                vids[vid]["size_bytes"] = total or 0
                                save_videos(vids)
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            vids = load_videos()
            if vid in vids:
                vids[vid]["status"] = "error"
                vids[vid]["error"] = "فشل التنزيل: %s" % exc
                save_videos(vids)
        log("فشل تنزيل %s: %s" % (vid, exc))
        return

    # فحص الملف: المدة والترميز (بدون ffmpeg)
    try:
        info = probe(dest)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            vids = load_videos()
            if vid in vids:
                vids[vid]["status"] = "error"
                vids[vid]["error"] = "ملف غير قابل للقراءة: %s" % exc
                vids[vid]["size_bytes"] = done
                save_videos(vids)
        log("ملف غير صالح %s: %s" % (vid, exc))
        return

    with _LOCK:
        vids = load_videos()
        if vid not in vids:
            return
        e = vids[vid]
        e["size_bytes"] = done
        e["duration_ms"] = info["duration_ms"]
        e["video"] = info["video"]
        e["audio"] = info["audio"]
        problem = build_schedule.codec_problem(e)
        if problem:
            e["status"] = "error"
            e["error"] = problem
        else:
            e["status"] = "ready"
            e["error"] = None
        save_videos(vids)
    log("فيديو جاهز: %s (%.1fMB، %.0f ث)" % (e["name"], done / 1e6, info["duration_ms"] / 1000.0))
    try:
        build_schedule.run_once()  # تحديث خرائط البث فوراً
    except Exception as exc:  # noqa: BLE001
        log("تحذير: تعذر تحديث الخرائط: %s" % exc)


def download_worker():
    while True:
        vid = _DOWNLOADQ.get()
        try:
            process_download(vid)
        except Exception as exc:  # noqa: BLE001
            log("خطأ غير متوقع في التنزيل: %s" % exc)
        finally:
            _DOWNLOADQ.task_done()


def requeue_pending():
    """عند الإقلاع: أي فيديو برابط وملفه مفقود يُعاد تنزيله (تخزين Koyeb مؤقت).
    فيديوهات «عن بُعد» لا تحتاج تنزيلاً — تُترك جاهزة كما هي."""
    n = 0
    with _LOCK:
        videos = load_videos()
        changed = False
        for vid, e in videos.items():
            if e.get("storage") == "remote":
                # ناقص البيانات؟ (استيراد قديم) — نعيد فحصه في الخلفية، وإلا فهو جاهز دائماً
                if e.get("status") != "ready" or not e.get("duration_ms"):
                    e["status"] = "queued"
                    e["error"] = None
                    _DOWNLOADQ.put(vid)
                    n += 1
                    changed = True
                continue
            if not e.get("url"):
                continue
            if e.get("status") in ("queued", "downloading") or not os.path.isfile(e.get("file") or ""):
                e["status"] = "queued"
                e["error"] = None
                changed = True
                _DOWNLOADQ.put(vid)
                n += 1
        if changed:
            save_videos(videos)
    if n:
        log("%d فيديو في طابور إعادة التنزيل" % n)
    return n


def delete_video(vid):
    with _LOCK:
        videos = load_videos()
        entry = videos.get(vid)
        if not entry:
            return False, "العنصر غير موجود"
        f = entry.get("file") or ""
        if f.startswith(os.path.abspath(MEDIA_DIR) + os.sep) and os.path.isfile(f):
            try:
                os.remove(f)
            except OSError:
                pass
        del videos[vid]
        save_videos(videos)
        channels = load_channels()
        for ch in channels.values():
            if ch.get("type") == "vod" and vid in (ch.get("playlist") or []):
                ch["playlist"] = [x for x in ch["playlist"] if x != vid]
        save_channels(channels)
    apply_changes()
    return True, None


# ------------------------------------------------------------------
# القنوات
# ------------------------------------------------------------------
def new_channel_id(channels, wanted=None):
    if wanted:
        cid = wanted.strip().lower()
        if not ID_RE.match(cid):
            return None, "المعرف غير صالح: أحرف لاتينية صغيرة وأرقام و - و _ فقط (حتى 24 حرفاً)"
        if cid in RESERVED_IDS:
            return None, "هذا المعرف محجوز — اختر غيره"
        if cid in channels:
            return None, "المعرف مستخدم بالفعل"
        return cid, None
    while True:
        cid = "ch" + os.urandom(3).hex()
        if cid not in channels:
            return cid, None


def validate_channel_payload(payload, channels, existing=None):
    """يتحقق من بيانات قناة ويعيد (dict جزئي للتحديث, خطأ)."""
    out = {}
    ctype = payload.get("type") or (existing or {}).get("type")
    if ctype not in ("vod", "restream"):
        return None, "نوع القناة يجب أن يكون vod أو restream"
    out["type"] = ctype

    name = (payload.get("name") if "name" in payload else (existing or {}).get("name")) or ""
    name = name.strip()
    if not name or len(name) > 80:
        return None, "اسم القناة مطلوب (حتى 80 حرفاً)"
    out["name"] = name

    if ctype == "vod":
        playlist = payload.get("playlist")
        if playlist is None:
            playlist = (existing or {}).get("playlist") or []
        seen = []
        videos = load_videos()
        for vid in playlist:
            if vid not in videos:
                return None, "القائمة تحتوي فيديو غير موجود: %s" % vid
            if vid not in seen:
                seen.append(vid)
        if not seen:
            return None, "اختر فيديو واحداً على الأقل لقائمة التشغيل"
        out["playlist"] = seen
    else:
        src = payload.get("source_url")
        if src is None:
            src = (existing or {}).get("source_url") or ""
        src = src.strip()
        if not re.match(r"^https?://", src):
            return None, "رابط البث الخارجي غير صالح — يجب أن يبدأ بـ http(s)://"
        out["source_url"] = src[:2048]
        stype = (payload.get("source_type") or (existing or {}).get("source_type") or "auto").strip().lower()
        out["source_type"] = stype if stype in ("auto", "hls", "ts") else "auto"
    return out, None


def create_channel(payload):
    with _LOCK:
        channels = load_channels()
        cid, err = new_channel_id(channels, payload.get("id"))
        if err:
            return None, err
        data, err = validate_channel_payload(payload, channels)
        if err:
            return None, err
        ch = {"id": cid, "created_at": int(time.time())}
        ch.update(data)
        channels[cid] = ch
        save_channels(channels)
    apply_changes()
    log("قناة جديدة: «%s» (id=%s، type=%s)" % (ch["name"], cid, ch["type"]))
    return ch, None


def update_channel(cid, payload):
    with _LOCK:
        channels = load_channels()
        ch = channels.get(cid)
        if not ch:
            return None, "القناة غير موجودة"
        data, err = validate_channel_payload(payload, channels, existing=ch)
        if err:
            return None, err
        ch.update(data)
        channels[cid] = ch
        save_channels(channels)
    apply_changes()
    log("تحديث قناة «%s» (id=%s)" % (ch["name"], cid))
    return ch, None


def delete_channel(cid):
    with _LOCK:
        channels = load_channels()
        if cid not in channels:
            return False, "القناة غير موجودة"
        del channels[cid]
        save_channels(channels)
    apply_changes()
    log("حذف قناة id=%s" % cid)
    return True, None


def channels_public():
    """قائمة القنوات للعرض العام (تُستخدم في صفحات المشغل)."""
    with _LOCK:
        data = build_schedule.load_data()
    videos = data["videos"]
    channels = data["channels"]
    out = []
    for ch in sorted(channels.values(), key=lambda c: c.get("created_at") or 0):
        item = {"id": ch["id"], "name": ch.get("name") or ch["id"], "type": ch.get("type")}
        if ch.get("type") == "vod":
            item["url"] = "/live/%s/master.m3u8" % ch["id"]
            item["media_type"] = "hls"
            item["now"] = build_schedule.now_playing(ch, data)
            item["playable"] = bool(item["now"])
        else:
            item["url"] = gen_locations.channel_url(ch)
            item["media_type"] = gen_locations.parse_url(
                ch.get("source_url") or "", ch.get("source_type") or "auto")["type"]
            item["playable"] = bool(ch.get("source_url"))
        out.append(item)
    return out


# ------------------------------------------------------------------
# تطبيق التغييرات: مواقع nginx + خرائط البث + إعادة تحميل
# ------------------------------------------------------------------
def reload_nginx():
    try:
        subprocess.run([NGINX_BIN, "-s", "reload"], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("أُعيد تحميل nginx")
    except Exception:  # noqa: BLE001
        pass  # (مثلاً أثناء الاختبار المحلي بدون nginx)


def apply_changes():
    gen_locations.generate()
    try:
        build_schedule.run_once()
    except Exception as exc:  # noqa: BLE001
        log("تحذير: تعذر توليد الخرائط: %s" % exc)
    reload_nginx()


# ------------------------------------------------------------------
# التصدير/الاستيراد + البذرة (SEED_JSON) + فحص الملفات المدمجة
# ------------------------------------------------------------------
def export_data():
    with _LOCK:
        videos = load_videos()
        channels = load_channels()
    # الاستوديو يُصدَّر أيضاً (الإعدادات + الشعار base64) ليعود بعد كل إقلاع جديد
    s = load_settings()
    logo_b64 = None
    f = (s.get("logo") or {}).get("file") or ""
    if LOGO_NAME_RE.match(f):
        p = os.path.join(WWW_DIR, f)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as fh:
                    logo_b64 = base64.b64encode(fh.read()).decode("ascii")
            except OSError:
                logo_b64 = None
    return {
        "version": 2,
        "exported_at": int(time.time()),
        "videos": [
            {"id": v["id"], "name": v.get("name"), "url": v.get("url"),
             "ext": v.get("ext"), "size_bytes": v.get("size_bytes") or 0,
             "duration_ms": v.get("duration_ms") or 0,
             "video": v.get("video"), "audio": v.get("audio"),
             "storage": v.get("storage") or "download"}
            for v in videos.values()
        ],
        "channels": [
            {"id": c["id"], "name": c.get("name"), "type": c.get("type"),
             "playlist": c.get("playlist") or [],
             "source_url": c.get("source_url"), "source_type": c.get("source_type")}
            for c in channels.values()
        ],
        "settings": s,
        "logo_b64": logo_b64,
    }


def import_data(obj, source="import"):
    """يستبدل البيانات بمحتوى JSON مُصدَّر. يعيد (إحصاءات، أخطاء)."""
    if isinstance(obj, str):
        obj = json.loads(obj)
    if not isinstance(obj, dict):
        raise ValueError("البيانات يجب أن تكون كائن JSON")

    videos_in = obj.get("videos") or []
    channels_in = obj.get("channels") or []
    if not isinstance(videos_in, list) or not isinstance(channels_in, list):
        raise ValueError("بنية البيانات غير صحيحة")

    new_videos = {}
    for item in videos_in[:400]:
        vid = item.get("id")
        if not isinstance(vid, str) or not ID_RE.match(vid):
            vid = "v_" + hashlib.md5(json.dumps(item, sort_keys=True).encode()).hexdigest()[:8]
        url = item.get("url")
        if url and not re.match(r"^https?://", url):
            url = None
        ext = item.get("ext")
        ext = ext if ext in ALLOWED_EXT else (ext_from_url(url) if url else ".mp4")
        fpath = os.path.join(MEDIA_DIR, vid + ext)
        exists = os.path.isfile(fpath)
        entry = {
            "id": vid, "name": (item.get("name") or "فيديو")[:120],
            "url": url, "ext": ext, "file": fpath,
            "size_bytes": int(item.get("size_bytes") or 0),
            "duration_ms": int(item.get("duration_ms") or 0),
            "video": item.get("video"), "audio": item.get("audio"),
            "storage": "download",
            "source": "url" if url else "embedded",
            "added_at": int(time.time()),
        }
        if exists and entry["duration_ms"] > 0:
            entry["status"] = "ready" if not build_schedule.codec_problem(entry) else "error"
            entry["error"] = build_schedule.codec_problem(entry)
        elif url:
            entry["status"] = "queued"
            entry["error"] = None
        else:
            entry["status"] = "error"
            entry["error"] = "الملف غير موجود على القرص ولا يوجد رابط له"
        # فيديو «عن بُعد»: لا ملف محلي إطلاقاً — جاهز فوراً إن كانت بياناته مكتملة
        if url and item.get("storage") == "remote":
            entry["storage"] = "remote"
            entry["file"] = ""
            entry["size_bytes"] = 0
            low = url.split("?", 1)[0].split("#", 1)[0].lower()
            if (low.endswith(REMOTE_EXT) and entry["duration_ms"] > 0
                    and not build_schedule.codec_problem(entry)):
                entry["status"] = "ready"
                entry["error"] = None
            else:
                # بيانات ناقصة (تصدير قديم) — نعيد فحصه عن بُعد في الخلفية
                entry["status"] = "queued"
                entry["error"] = None
        new_videos[vid] = entry

    new_channels = {}
    for item in channels_in[:50]:
        cid = item.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid) or cid in RESERVED_IDS or cid in new_channels:
            continue
        ctype = item.get("type")
        ch = {"id": cid, "name": (item.get("name") or cid)[:80],
              "type": ctype, "created_at": int(time.time())}
        if ctype == "vod":
            pl = [v for v in (item.get("playlist") or []) if v in new_videos]
            if not pl:
                continue
            ch["playlist"] = list(dict.fromkeys(pl))
        elif ctype == "restream":
            src = item.get("source_url") or ""
            if not re.match(r"^https?://", src):
                continue
            ch["source_url"] = src[:2048]
            st = (item.get("source_type") or "auto").lower()
            ch["source_type"] = st if st in ("auto", "hls", "ts") else "auto"
        else:
            continue
        new_channels[cid] = ch

    with _LOCK:
        save_videos(new_videos)
        save_channels(new_channels)
    # استعادة إعدادات الاستوديو والشعار إن وُجدا في البيانات (v2)
    st = obj.get("settings")
    if isinstance(st, dict) and (st.get("logo") or st.get("ticker")):
        try:
            save_settings(sanitize_settings(st))
        except Exception as exc:  # noqa: BLE001
            log("تحذير: تعذرت استعادة إعدادات الاستوديو: %s" % exc)
    lb = obj.get("logo_b64")
    if isinstance(lb, str) and len(lb) > 64:
        name, err = save_logo_image(lb)
        if err:
            log("تحذير: تعذرت استعادة الشعار: %s" % err)
    apply_changes()
    return {"videos": len(new_videos), "channels": len(new_channels)}, None


def migrate_legacy_restream():
    """توافق قديم: قنوات RESTREAM_URL..RESTREAM_URL_8 تُحوَّل إلى القنوات."""
    channels = load_channels()
    changed = False
    for i in range(1, 9):
        key = "RESTREAM_URL" if i == 1 else "RESTREAM_URL_%d" % i
        url = (os.environ.get(key) or "").strip()
        if not url:
            continue
        cid = str(i)
        if cid in channels:
            continue
        name_key = "RESTREAM_NAME" if i == 1 else "RESTREAM_NAME_%d" % i
        type_key = "RESTREAM_TYPE" if i == 1 else "RESTREAM_TYPE_%d" % i
        channels[cid] = {
            "id": cid,
            "name": (os.environ.get(name_key) or "").strip() or "قناة خارجية %d" % i,
            "type": "restream",
            "source_url": url,
            "source_type": (os.environ.get(type_key) or "auto").strip().lower(),
            "created_at": int(time.time()) - (9 - i),
        }
        changed = True
        log("ترحيل قناة خارجية من المتغير %s -> id=%s" % (key, cid))
    if changed:
        save_channels(channels)


def scan_media():
    """يستورد ملفات الفيديو الموجودة في MEDIA_DIR (المدمجة في الصورة) إلى المكتبة."""
    try:
        names = sorted(os.listdir(MEDIA_DIR))
    except OSError:
        return 0
    with _LOCK:
        videos = load_videos()
        known = {os.path.basename(v.get("file") or "") for v in videos.values()}
        changed = False
        added = 0
        for name in names:
            if name.startswith(".") or name.endswith(".part"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_EXT or name in known:
                continue
            vid = "m_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
            if vid in videos:
                continue
            fpath = os.path.join(MEDIA_DIR, name)
            entry = {
                "id": vid, "name": os.path.splitext(name)[0][:120],
                "url": None, "ext": ext, "file": fpath,
                "size_bytes": 0, "done_bytes": 0, "duration_ms": 0,
                "video": None, "audio": None,
                "source": "embedded", "added_at": int(time.time()) + added,
                "status": "error", "error": None,
            }
            try:
                info = probe(fpath)
                entry["size_bytes"] = os.path.getsize(fpath)
                entry["duration_ms"] = info["duration_ms"]
                entry["video"] = info["video"]
                entry["audio"] = info["audio"]
                problem = build_schedule.codec_problem(entry)
                if problem:
                    entry["error"] = problem
                else:
                    entry["status"] = "ready"
            except Exception as exc:  # noqa: BLE001
                entry["error"] = "ملف غير قابل للقراءة: %s" % exc
            videos[vid] = entry
            changed = True
            added += 1
            log("استيراد ملف مدمج: %s (%s)" % (name, entry["status"]))
        if changed:
            save_videos(videos)
    return added


def bootstrap():
    """تهيئة الإقلاع: بذرة + ترحيل متغيرات قديمة + فحص ملفات الصورة + توليد الملفات."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(os.path.join(MAPS_DIR, "live"), exist_ok=True)

    if SEED_JSON.strip() and not os.path.exists(VIDEOS_FILE) and not os.path.exists(CHANNELS_FILE):
        try:
            stats, _ = import_data(json.loads(SEED_JSON), source="seed")
            log("استُعيدت البذرة SEED_JSON: %d فيديو، %d قناة" % (stats["videos"], stats["channels"]))
        except Exception as exc:  # noqa: BLE001
            log("تحذير: فشل استيراد SEED_JSON: %s" % exc)

    migrate_legacy_restream()
    scan_media()
    n_loc = gen_locations.generate()
    n_map = build_schedule.run_once()
    log("التهيئة اكتملت: %d قناة في ملف المواقع، %d خريطة بث" % (n_loc, n_map))


# ------------------------------------------------------------------
# خادم HTTP (خلف nginx فقط — يستمع على 127.0.0.1)
# ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "koyeb-tv-admin/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- أدوات ----------
    def log_message(self, fmt, *args):  # سجل هادئ
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), cookie=cookie)

    def _error(self, code, msg):
        self._json(code, {"ok": False, "error": msg})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 5 * 1024 * 1024:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _authed(self):
        if not auth_enabled():
            return True
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        tok = cookie["tv_token"].value if "tv_token" in cookie else ""
        return hmac.compare_digest(tok, make_token())

    def _require_auth(self):
        if self._authed():
            return True
        self._error(401, "غير مصرح — سجّل الدخول إلى لوحة التحكم")
        return False

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        parts = [s for s in path.split("/") if s]
        try:
            if path == "/api/session":
                self._json(200, {"ok": True, "auth_enabled": auth_enabled(),
                                 "authed": self._authed(), "app_name": APP_NAME})
            elif path == "/api/channels":
                self._json(200, {"ok": True, "app_name": APP_NAME,
                                 "channels": channels_public()})
            elif len(parts) == 3 and parts[0] == "api" and parts[1] == "now":
                self.api_now(parts[2])
            elif path == "/api/library":
                if self._require_auth():
                    self.api_library()
            elif path == "/api/system":
                if self._require_auth():
                    self.api_system()
            elif path == "/api/export":
                if self._require_auth():
                    self._json(200, {"ok": True, "data": export_data()})
            elif path == "/api/branding":
                s = load_settings()
                self._json(200, {"ok": True, "branding": s, "logo_url": logo_url_of(s)})
            else:
                self._error(404, "المسار غير موجود")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(500, "خطأ داخلي: %s" % exc)
            except Exception:  # noqa: BLE001
                pass

    def api_now(self, cid):
        with _LOCK:
            data = build_schedule.load_data()
        ch = (data.get("channels") or {}).get(cid)
        if not ch:
            self._error(404, "القناة غير موجودة")
            return
        now = build_schedule.now_playing(ch, data)
        self._json(200, {"ok": True, "id": cid, "name": ch.get("name"),
                         "type": ch.get("type"), "now": now})

    def api_library(self):
        with _LOCK:
            videos = load_videos()
        items = sorted(videos.values(), key=lambda v: v.get("added_at") or 0, reverse=True)
        self._json(200, {"ok": True, "videos": items,
                         "usage_bytes": library_usage_bytes(videos),
                         "limit_bytes": MAX_LIBRARY_MB * 1024 * 1024})

    def api_system(self):
        with _LOCK:
            videos = load_videos()
            channels = load_channels()
        self._json(200, {
            "ok": True, "app_name": APP_NAME,
            "limits": {"video_mb": MAX_VIDEO_MB, "library_mb": MAX_LIBRARY_MB},
            "usage": {"library_mb": round(library_usage_bytes(videos) / 1e6, 1),
                      "videos": len(videos),
                      "videos_ready": sum(1 for v in videos.values() if v.get("status") == "ready"),
                      "channels": len(channels)},
            "paths": {"data": DATA_DIR, "media": MEDIA_DIR},
            "media_url_env": bool(os.environ.get("MEDIA_URL")),
        })

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        parts = [s for s in path.split("/") if s]
        try:
            if path == "/api/login":
                body = self._body() or {}
                if check_password(body.get("password")):
                    cookie = ("tv_token=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                              % make_token())
                    self._json(200, {"ok": True}, cookie=cookie)
                else:
                    time.sleep(0.6)  # تبطيء المحاولات العشوائية
                    self._error(401, "كلمة المرور غير صحيحة")
            elif path == "/api/logout":
                cookie = "tv_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                self._json(200, {"ok": True}, cookie=cookie)
            elif path == "/api/videos":
                if self._require_auth():
                    body = self._body() or {}
                    entry, err = add_video(body.get("name"), body.get("url"),
                                           body.get("storage"))
                    if err:
                        self._error(400, err)
                    else:
                        self._json(200, {"ok": True, "video": entry})
            elif len(parts) == 4 and parts[0] == "api" and parts[1] == "videos" and parts[3] == "retry":
                if self._require_auth():
                    self.api_retry(parts[2])
            elif path == "/api/channels":
                if self._require_auth():
                    body = self._body() or {}
                    ch, err = create_channel(body)
                    if err:
                        self._error(400, err)
                    else:
                        self._json(200, {"ok": True, "channel": ch})
            elif path == "/api/import":
                if self._require_auth():
                    body = self._body() or {}
                    try:
                        stats, err = import_data(body.get("data"), source="import")
                    except Exception as exc:  # noqa: BLE001
                        stats, err = None, "بيانات غير صالحة: %s" % exc
                    if err:
                        self._error(400, err)
                    else:
                        requeue_pending()
                        self._json(200, {"ok": True, "imported": stats})
            elif path == "/api/branding":
                if self._require_auth():
                    body = self._body() or {}
                    cur = load_settings()
                    s = sanitize_settings(body, prev=cur)
                    save_settings(s)
                    log("تحديث إعدادات الاستوديو")
                    self._json(200, {"ok": True, "branding": s, "logo_url": logo_url_of(s)})
            elif path == "/api/branding/logo":
                if self._require_auth():
                    body = self._body() or {}
                    name, err = save_logo_image(body.get("data"))
                    if err:
                        self._error(400, err)
                    else:
                        s = load_settings()
                        self._json(200, {"ok": True, "branding": s, "logo_url": logo_url_of(s)})
            else:
                self._error(404, "المسار غير موجود")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(500, "خطأ داخلي: %s" % exc)
            except Exception:  # noqa: BLE001
                pass

    def api_retry(self, vid):
        with _LOCK:
            videos = load_videos()
            e = videos.get(vid)
            if not e:
                self._error(404, "العنصر غير موجود")
                return
            if not e.get("url"):
                self._error(400, "لا يمكن إعادة المحاولة — لا يوجد رابط (ملف مدمج)")
                return
            e["status"] = "queued"
            e["error"] = None
            save_videos(videos)
        _DOWNLOADQ.put(vid)
        self._json(200, {"ok": True})

    # ---------- PUT / DELETE ----------
    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        parts = [s for s in path.split("/") if s]
        try:
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "channels":
                if self._require_auth():
                    body = self._body() or {}
                    ch, err = update_channel(parts[2], body)
                    if err:
                        self._error(400, err)
                    else:
                        self._json(200, {"ok": True, "channel": ch})
            else:
                self._error(404, "المسار غير موجود")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(500, "خطأ داخلي: %s" % exc)
            except Exception:  # noqa: BLE001
                pass

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        parts = [s for s in path.split("/") if s]
        try:
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "videos":
                if self._require_auth():
                    ok, err = delete_video(parts[2])
                    if err:
                        self._error(404, err)
                    else:
                        self._json(200, {"ok": True})
            elif len(parts) == 3 and parts[0] == "api" and parts[1] == "channels":
                if self._require_auth():
                    ok, err = delete_channel(parts[2])
                    if err:
                        self._error(404, err)
                    else:
                        self._json(200, {"ok": True})
            elif path == "/api/branding/logo":
                if self._require_auth():
                    delete_logo()
                    self._json(200, {"ok": True, "branding": load_settings(), "logo_url": None})
            else:
                self._error(404, "المسار غير موجود")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._error(500, "خطأ داخلي: %s" % exc)
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------
# المجدول الدوري
# ------------------------------------------------------------------
def scheduler_loop():
    while True:
        time.sleep(max(5, SCHEDULE_INTERVAL))
        try:
            build_schedule.run_once()
        except Exception as exc:  # noqa: BLE001
            log("خطأ في المجدول: %s" % exc)


# ------------------------------------------------------------------
# التشغيل
# ------------------------------------------------------------------
def main():
    if "--bootstrap" in sys.argv:
        bootstrap()
        return 0

    bootstrap()
    requeue_pending()
    threading.Thread(target=download_worker, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", ADMIN_PORT), Handler)
    log("الواجهة البرمجية على 127.0.0.1:%d — الحماية: %s — اسم المحطة: %s"
        % (ADMIN_PORT, "مفعّلة" if auth_enabled() else "معطّلة", APP_NAME))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
