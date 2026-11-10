#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_schedule.py — توليد خرائط البث الحي لكل قنوات المكتبة (بدون ffmpeg).

الفكرة (نفس خوارزمية test/playlist.php المرجعية لوحدة nginx-vod-module):
- كل قناة من نوع "vod" لها قائمة تشغيل (playlist) من فيديوهات المكتبة تُعرض
  بالترتيب، واحدة تلو الأخرى، في حلقة زمنية لا نهائية تبدأ من SCHEDULE_EPOCH_MS.
- عند كل تشغيل، يُكتب لكل قناة ملف JSON يغطي النافذة الزمنية
  [الآن - هامش، الآن + هامش]، وتقرأه وحدة vod لكل طلب بث في وضع mapped.
- الحساب حتمي (deterministic) اعتماداً على ساعة الحاوية فقط، لذا الأرقام
  التسلسلية (initialClipIndex / initialSegmentIndex) تبقى متسقة عبر التجديدات.

يُستخدم كمكتبة من admin.py:
    run_once(data)         يولّد خرائط كل قنوات VOD ويعيد عددها
    now_playing(ch, data)  ما يُبث الآن في قناة معينة (للعرض في المشغل)
    codec_problem(v)      سبب استبعاد فيديو من البث أو None إن كان صالحاً

أو كسكربت من سطر الأوامر:  python3 build_schedule.py   (يقرأ DATA_DIR)

المتغيرات:
    DATA_DIR            مجلد البيانات (videos.json / channels.json)
    MEDIA_DIR           مجلد الفيديوهات            (افتراضي /media)
    MAPS_DIR            مجلد خرائط البث            (افتراضي /var/vod/maps)
    SEGMENT_DURATION    مدة المقطع بالمللي ثانية   (افتراضي 10000)
    LIVE_WINDOW         نافذة البث بالمللي ثانية   (افتراضي 60000)
    SCHEDULE_EPOCH_MS   بداية الخط الزمني للقنوات  (افتراضي 2026-01-01T00:00:00Z)
"""

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
MAPS_DIR = os.environ.get("MAPS_DIR", "/var/vod/maps")
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10000"))
LIVE_WINDOW = int(os.environ.get("LIVE_WINDOW", "60000"))
SCHEDULE_EPOCH_MS = int(
    os.environ.get("SCHEDULE_EPOCH_MS", "1767225600000")  # 2026-01-01T00:00:00Z
)

MAX_CLIPS = 126          # حد أقصى آمن (توثيق الوحدة: حتى 128 عنصراً في durations)
# ترميزات الصوت التي تدعمها وحدة vod في حزم MPEG-TS
AUDIO_OK = (None, "", "aac", "mp3", "ac3")
MARGIN_BEFORE_MS = LIVE_WINDOW + 3 * SEGMENT_DURATION + 20000
MARGIN_AFTER_MS = 2 * SEGMENT_DURATION + 10000


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def load_data():
    """يقرأ مكتبة الفيديو والقنوات من DATA_DIR."""
    videos = _read_json(os.path.join(DATA_DIR, "videos.json"), {})
    channels = _read_json(os.path.join(DATA_DIR, "channels.json"), {})
    return {"videos": videos, "channels": channels}


def codec_problem(v):
    """يعيد سبب استبعاد الفيديو من البث، أو None إن كان صالحاً للبث عبر TS."""
    if v.get("video") != "avc1":
        return (
            "ترميز الفيديو %s غير مدعوم (المطلوب H.264 — ملفات H.265/HEVC "
            "لا يمكن معالجتها بدون ffmpeg)" % (v.get("video") or "غير معروف")
        )
    if v.get("audio") not in AUDIO_OK:
        return "ترميز الصوت %s غير مدعوم في TS (المطلوب AAC أو MP3 أو AC3)" % v.get("audio")
    if (v.get("duration_ms") or 0) < 500:
        return "مدة الملف قصيرة جداً"
    return None


def channel_items(ch, videos):
    """[(path_or_url, duration_ms, name)] للعناصر الجاهزة والصالحة فقط وبترتيب القائمة.
    العناصر "عن بُعد" (storage=remote) يُعاد رابطها المصدرية بدل مسار الملف المحلي."""
    out = []
    for vid in ch.get("playlist") or []:
        v = videos.get(vid)
        if not v or v.get("status") != "ready":
            continue
        if codec_problem(v):
            continue
        if v.get("storage") == "remote":
            url = v.get("url") or ""
            if not url.startswith(("http://", "https://")):
                continue
            out.append((url, int(v["duration_ms"]), v.get("name") or vid))
            continue
        if not v.get("file") or not os.path.isfile(v["file"]):
            continue
        out.append((v["file"], int(v["duration_ms"]), v.get("name") or vid))
    return out


def build_mapping(items):
    """يبني قاموس خريطة البث الحي لقائمة عناصر [(file, dur_ms, name)]."""
    paths = [p for p, _, _ in items]
    durs = [d for _, d, _ in items]
    n = len(paths)
    cycle_duration = sum(durs)

    now_ms = int(time.time() * 1000)
    win_start = now_ms - MARGIN_BEFORE_MS
    win_end = now_ms + MARGIN_AFTER_MS

    # موقع بداية الدورة التي تلتقي مع بداية النافذة
    cycle_index = (win_start - SCHEDULE_EPOCH_MS) // cycle_duration
    if cycle_index < 0:
        cycle_index = 0

    clips = []
    out_durs = []
    first_clip_time = None
    first_global_index = None

    t = SCHEDULE_EPOCH_MS + cycle_index * cycle_duration
    gi = cycle_index * n  # الرقم العام (0-based) للمقطع الحالي منذ بداية القناة

    while t < win_end and len(clips) < MAX_CLIPS:
        idx = gi % n
        d = durs[idx]
        if t + d > win_start:  # المقطع يتقاطع مع النافذة
            if not clips:
                first_clip_time = t
                first_global_index = gi
            clips.append(paths[idx])
            out_durs.append(d)
        t += d
        gi += 1

    if not clips:
        return None

    # عدد المقاطع الكاملة قبل أول مقطع في النافذة (بتقسيم ذكي على الدورات الكاملة)
    full_cycles = first_global_index // n
    remainder = first_global_index % n
    segments_per_cycle = sum(int(math.ceil(d / float(SEGMENT_DURATION))) for d in durs)
    segments_before = full_cycles * segments_per_cycle
    segments_before += sum(
        int(math.ceil(durs[i] / float(SEGMENT_DURATION))) for i in range(remainder)
    )

    return {
        "playlistType": "live",
        "discontinuity": True,
        "firstClipTime": first_clip_time,
        "initialClipIndex": first_global_index + 1,      # 1-based كما في المثال المرجعي
        "initialSegmentIndex": segments_before + 1,      # 1-based
        "durations": out_durs,
        "sequences": [
            {
                "clips": [
                    # sourceType http = قراءة عن بُعد بطلبات Range (لا يستهلك القرص)
                    # sourceType file = ملف محلي على قرص المحطة
                    {"type": "source", "path": p,
                     "sourceType": "http" if p.startswith(("http://", "https://")) else "file"}
                    for p in clips
                ],
            }
        ],
    }


def _mapping_path(channel_id):
    # الوحدة تطلب الخريطة عبر /map/ + <مسار الطلب بدون اسم الملف>،
    # لذا يجب أن يكون الملف تحت MAPS_DIR/live/<id> ليطابق /live/<id>/master.m3u8
    return os.path.join(MAPS_DIR, "live", channel_id)


def _write_mapping(channel_id, mapping):
    path = _mapping_path(channel_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _remove_mapping(channel_id):
    try:
        os.remove(_mapping_path(channel_id))
    except OSError:
        pass


def run_once(data=None):
    """يولّد خرائط البث لكل قنوات VOD. يعيد عدد القنوات المولَّدة."""
    if data is None:
        data = load_data()
    videos = data.get("videos") or {}
    channels = data.get("channels") or {}

    generated = 0
    for ch in channels.values():
        if ch.get("type") != "vod":
            continue
        ch_id = ch.get("id")
        if not ch_id or not ID_OK(ch_id):
            continue
        items = channel_items(ch, videos)
        if not items:
            # لا عناصر صالحة — نحذف أي خريطة قديمة حتى يستجيب الرابط بـ 404
            _remove_mapping(ch_id)
            print("[schedule] قناة «%s» بلا عناصر صالحة — الخريطة محذوفة" % ch.get("name", ch_id),
                  flush=True)
            continue

        mapping = build_mapping(items)
        if mapping is None:
            print("[schedule] قناة «%s»: نافذة فارغة؟" % ch.get("name", ch_id), flush=True)
            continue

        _write_mapping(ch_id, mapping)
        generated += 1
        total_window = sum(mapping["durations"])
        print(
            "[schedule] قناة «%s»: %d مقطعاً، تغطية %.1f ث، أول مقطع عند %d، "
            "clipIndex=%d segmentIndex=%d"
            % (
                ch.get("name", ch_id),
                len(mapping["durations"]),
                total_window / 1000.0,
                mapping["firstClipTime"],
                mapping["initialClipIndex"],
                mapping["initialSegmentIndex"],
            ),
            flush=True,
        )
    return generated


def ID_OK(channel_id):
    """تحقق من سلامة معرف القناة (يُستخدم في مسارات nginx)."""
    import re
    return bool(re.match(r"^[a-z0-9][a-z0-9_-]{0,23}$", channel_id or ""))


def now_playing(ch, data=None, now_ms=None):
    """ما يُبث الآن في قناة VOD: {title, position_ms, duration_ms, index, next}."""
    if ch.get("type") != "vod":
        return None
    if data is None:
        data = load_data()
    items = channel_items(ch, data.get("videos") or {})
    if not items:
        return None

    durs = [d for _, d, _ in items]
    total = sum(durs)
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    offset = (now_ms - SCHEDULE_EPOCH_MS) % total
    if offset < 0:
        offset += total
    idx = 0
    for i, d in enumerate(durs):
        if offset < d:
            idx = i
            break
        offset -= d

    return {
        "title": items[idx][2],
        "position_ms": int(offset),
        "duration_ms": durs[idx],
        "index": idx,
        "next": items[(idx + 1) % len(items)][2],
        "total_ms": total,
    }


def main():
    data = load_data()
    count = run_once(data)
    print("[schedule] تم توليد خرائط %d قناة -> %s/live/" % (count, MAPS_DIR), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
