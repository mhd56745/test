#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_locations.py — توليد ملف مواقع nginx لكل القنوات في ملف include واحد.

يقرأ channels.json من DATA_DIR ويكتب (عبر الدالة generate أو سطر الأوامر):
    /var/vod/channel_locations.conf
يضمّن ملف nginx.conf موقعاً لكل قناة:
- قناة VOD    : /live/<id>/      (nginx-vod وضع mapped — تقرأ الخريطة من /map/)
- قناة HLS    : /restream/<id>/  (وكيل عكسي مع إعادة كتابة الروابط المطلقة)
- قناة TS     : /restream/<id>/stream.ts (تمرير مباشر)

ملاحظة تقنية: لا يمكن استخدام معرف القناة في أسماء متغيرات nginx (شرطة غير
مسموحة)، لذا تُرقَّم المتغيرات الداخلية $r1_origin، $r2_origin… بالترتيب.
"""

import json
import os
import sys
from urllib.parse import urlsplit

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DEFAULT_OUT = os.environ.get("CHANNELS_CONF", "/var/vod/channel_locations.conf")
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10000"))
LIVE_WINDOW = int(os.environ.get("LIVE_WINDOW", "60000"))

BLOCK_HEADER = """\
# ============================================================
#  مواقع القنوات — ملف مولَّد آلياً من لوحة التحكم (gen_locations.py)
#  لا تعدّله يدوياً — سيُستبدل عند كل تعديل في القنوات
# ============================================================
"""

VOD_BLOCK = """\
# --- قناة المكتبة: {name} (id={id}) ---
location ^~ /live/{id}/ {{
    vod hls;
    vod_mode mapped;
    vod_upstream_location /map/;
    # فيديوهات «عن بُعد»: مقاطع الخريطة ذات sourceType:http تُجلب من المصدر مباشرة
    vod_remote_upstream_location /vod-remote/;

    # روابط مطلقة بالمخطط الصحيح (https خلف Koyeb، http محلياً)
    vod_base_url $tv_proto://$http_host;

    vod_segment_duration {segment_duration};
    vod_live_window_duration {live_window};
    vod_align_segments_to_key_frames on;

    # مقاطع MPEG-TS = توافق أقصى مع كل تطبيقات IPTV والأجهزة القديمة
    vod_hls_container_format mpegts;

    vod_expires 1h;                     # مقاطع TS (أسماؤها ثابتة)
    vod_expires_live 1h;                # master.m3u8
    vod_expires_live_time_dependent 5s; # index.m3u8 (تتغير باستمرار)

    add_header Access-Control-Allow-Origin "*" always;
    add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS" always;
    add_header Access-Control-Allow-Headers "Origin, Range, Accept, Referer" always;
    add_header Access-Control-Expose-Headers "Server, Range, Content-Length, Content-Range" always;
}}
"""

HLS_BLOCK = """\
# --- قناة خارجية: {name} (id={id}) — HLS من {origin} ---
location ^~ /restream/{id}/ {{
    resolver {resolver} valid=60s ipv6=off;
    set $r{num}_origin "{origin}";
    set $r{num}_host "{host}";
    rewrite ^/restream/{id}/(.*)$ /$1 break;
    proxy_pass $r{num}_origin;
    proxy_set_header Host $r{num}_host;
    proxy_set_header Accept-Encoding "";
    proxy_http_version 1.1;
    proxy_ssl_server_name on;
    proxy_connect_timeout 10s;
    proxy_read_timeout 60s;
    proxy_buffers 16 32k;

    # إعادة كتابة الروابط المطلقة داخل قوائم m3u8 لتعود عبر الوكيل
    sub_filter '{origin}/' '/restream/{id}/';
    sub_filter_types application/vnd.apple.mpegurl application/x-mpegurl;
    sub_filter_once off;

    add_header Access-Control-Allow-Origin "*" always;
    add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS" always;
    add_header Access-Control-Allow-Headers "Origin, Range, Accept, Referer" always;
}}
"""

TS_BLOCK = """\
# --- قناة خارجية: {name} (id={id}) — TS متواصل من {full_url} ---
location = /restream/{id}/stream.ts {{
    resolver {resolver} valid=60s ipv6=off;
    set $r{num}_full "{full_url}";
    set $r{num}_host "{host}";
    proxy_pass $r{num}_full;
    proxy_set_header Host $r{num}_host;
    proxy_set_header Accept-Encoding "";
    proxy_http_version 1.1;
    proxy_ssl_server_name on;
    proxy_buffering off;
    proxy_connect_timeout 10s;
    proxy_read_timeout 300s;
    add_header Access-Control-Allow-Origin "*" always;
}}
"""

TS_EXTENSIONS = ("ts", "mts", "m2ts")


def detect_type(path, force_type="auto"):
    """hls أو ts — بالامتداد أو بالإجبار عبر source_type."""
    rtype = (force_type or "auto").strip().lower()
    if rtype in ("hls", "ts"):
        return rtype
    ext = (path or "").lower().rsplit(".", 1)[-1]
    return "ts" if ext in TS_EXTENSIONS else "hls"


def parse_url(url, force_type="auto"):
    """يحلل رابط قناة خارجية ويعيد (النوع، الأصل، المضيف، المسار، الاستعلام)."""
    url = (url or "").strip()
    sp = urlsplit(url)
    scheme = sp.scheme or "https"
    path = sp.path or "/"
    query = ("?" + sp.query) if sp.query else ""
    return {
        "type": detect_type(path, force_type),
        "origin": "%s://%s" % (scheme, sp.netloc),
        "host": sp.netloc,
        "path": path,
        "query": query,
    }


def channel_url(ch):
    """الرابط العام لقناة خارجية على محطتنا (يُستخدم أيضاً في admin.py)."""
    info = parse_url(ch.get("source_url") or "", ch.get("source_type") or "auto")
    if info["type"] == "ts":
        return "/restream/%s/stream.ts" % ch["id"]
    return "/restream/%s%s%s" % (ch["id"], info["path"], info["query"])


def get_resolver():
    val = (os.environ.get("DNS_RESOLVER") or "").strip()
    if val:
        return val
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError:
        pass
    return "1.1.1.1"


def load_channels():
    try:
        with open(os.path.join(DATA_DIR, "channels.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def build_conf(channels, resolver):
    if not channels:
        return BLOCK_HEADER + "# لا توجد قنوات بعد — أضفها من لوحة التحكم /admin/\n"
    parts = [BLOCK_HEADER]
    num = 0
    for ch in sorted(channels.values(), key=lambda c: c.get("created_at") or 0):
        num += 1
        common = {"id": ch["id"], "name": ch.get("name") or ch["id"],
                  "resolver": resolver, "num": num,
                  "segment_duration": SEGMENT_DURATION, "live_window": LIVE_WINDOW}
        if ch.get("type") == "vod":
            parts.append(VOD_BLOCK.format(**common))
        else:
            info = parse_url(ch.get("source_url") or "", ch.get("source_type") or "auto")
            if not info["host"]:
                parts.append("# !!! قناة «%s» (id=%s): رابط خارجي غير صالح — تم تخطيها\n"
                             % (common["name"], common["id"]))
                continue
            if info["type"] == "hls":
                parts.append(HLS_BLOCK.format(origin=info["origin"], host=info["host"], **common))
            else:
                parts.append(TS_BLOCK.format(
                    full_url=(ch.get("source_url") or "").strip(),
                    host=info["host"], **common))
    return "\n".join(parts)


def generate(out_path=None):
    """يولّد ملف المواقع ويعيد عدد القنوات المكتوبة."""
    out_path = out_path or DEFAULT_OUT
    channels = load_channels()
    channels = {k: v for k, v in channels.items() if v.get("type") in ("vod", "restream")}
    content = build_conf(channels, get_resolver())

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, out_path)
    return len(channels)


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    count = generate(out_path)
    print("[locations] %d قناة -> %s" % (count, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
