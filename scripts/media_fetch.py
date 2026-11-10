#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
media_fetch.py — تنزيل ملفات الفيديو من روابط مباشرة عند إقلاع الحاوية.

هذا هو البديل العملي عن التورنت: أي ملف MP4/MKV (H.264) يوضعه المستخدم على
GitHub Releases أو أي رابط مباشر، وتنزّله الحاوية إلى MEDIA_DIR عند كل إقلاع
(التخزين مؤقت على Koyeb، لذا التنزيل يتكرر مع كل إعادة تشغيل).

المتغيرات:
    MEDIA_URL      روابط مفصولة بفواصل أو مسافات (إلزامي لعمل هذا السكربت)
    MEDIA_DIR      مجلد الوسائط (افتراضي /media)
    MEDIA_MAX_MB   الحد الأقصى لحجم الملف الواحد (افتراضي 2048)

ملاحظات:
- يُقبل فقط الرابط الذي ينتهي اسم ملفه بامتداد مدعوم (.mp4/.mkv/.mov/.m4v)
  لأن مصنف الحاوية يقرأ الترميزات حسب الامتداد.
- يتخطى التنزيل إذا كان المجلد يحتوي فيديو بالفعل.
"""

import os
import sys
import time
import urllib.request

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
MEDIA_URL = (os.environ.get("MEDIA_URL") or "").strip()
MEDIA_MAX_MB = int(os.environ.get("MEDIA_MAX_MB", "2048"))

EXTENSIONS = (".mp4", ".mkv", ".mov", ".m4v")
CHUNK = 256 * 1024
PROGRESS_EVERY = 25 * 1024 * 1024   # اطبع تقدماً كل 25MB


def has_video():
    try:
        for name in os.listdir(MEDIA_DIR):
            if name.lower().endswith(EXTENSIONS):
                return True
    except OSError:
        pass
    return False


def filename_from_url(url):
    name = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return name


def download(url, dest):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (koyeb-tv media fetcher)",
        "Accept": "*/*",
    })
    started = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        if total and total > MEDIA_MAX_MB * 1024 * 1024:
            raise ValueError("حجم الملف %dMB يتجاوز الحد MEDIA_MAX_MB=%dMB"
                             % (total // (1024 * 1024), MEDIA_MAX_MB))
        tmp = dest + ".part"
        done = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if done // PROGRESS_EVERY != (done - len(chunk)) // PROGRESS_EVERY:
                    pct = (" %.0f%%" % (100.0 * done / total)) if total else ""
                    print("[media]   ... %.0fMB%s" % (done / 1e6, pct), flush=True)
                if done > MEDIA_MAX_MB * 1024 * 1024:
                    fh.close()
                    os.remove(tmp)
                    raise ValueError("تجاوز الحد MEDIA_MAX_MB=%dMB" % MEDIA_MAX_MB)
    os.replace(tmp, dest)
    print("[media] تم تنزيل %s (%.1fMB في %.0f ث)"
          % (dest, done / 1e6, time.time() - started), flush=True)


def main():
    if not MEDIA_URL:
        print("[media] MEDIA_URL غير مضبوط — تخطي")
        return 0
    if has_video():
        print("[media] يوجد فيديو مسبقاً في %s — تخطي التنزيل" % MEDIA_DIR)
        return 0

    urls = [u.strip() for u in MEDIA_URL.replace(",", " ").split() if u.strip()]
    if not urls:
        return 0

    os.makedirs(MEDIA_DIR, exist_ok=True)
    ok = 0
    for url in urls:
        name = filename_from_url(url)
        if not name.lower().endswith(EXTENSIONS):
            print("[media] تخطي %s — اسم الملف يجب أن ينتهي بامتداد %s"
                  % (url, "/".join(EXTENSIONS)), flush=True)
            continue
        dest = os.path.join(MEDIA_DIR, name)
        print("[media] تنزيل %s -> %s" % (url, dest), flush=True)
        try:
            download(url, dest)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("[media] فشل تنزيل %s: %s" % (url, exc), flush=True)

    if not ok:
        print("[media] تحذير: لم يُنزَّل أي ملف — القناة الرئيسية ستبقى غير متاحة",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
