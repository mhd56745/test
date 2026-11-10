#!/bin/bash
# =====================================================================
# entrypoint.sh — إقلاع المحطة التلفزية (لوحة تحكم بأسلوب Viloud + nginx)
# 1) يجهّز المجلدات والمتغيرات   2) يهيئ البيانات والخرائط والمواقع
# 3) يشغّل لوحة التحكم (API + مجدول + تنزيلات)   4) يشغّل nginx في المقدمة
# =====================================================================
set -u

# ---------- قيم افتراضية ----------
export PORT="${PORT:-8000}"
export APP_NAME="${APP_NAME:-قناتي}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}"
export ADMIN_PORT="${ADMIN_PORT:-9000}"
export DATA_DIR="${DATA_DIR:-/data}"
export MEDIA_DIR="${MEDIA_DIR:-/media}"
export MAPS_DIR="${MAPS_DIR:-/var/vod/maps}"
export CACHE_DIR="${CACHE_DIR:-/var/vod/cache}"
export CHANNELS_CONF="${CHANNELS_CONF:-/var/vod/channel_locations.conf}"
export WWW_DIR="${WWW_DIR:-/var/www}"
export SEGMENT_DURATION="${SEGMENT_DURATION:-10000}"
export LIVE_WINDOW="${LIVE_WINDOW:-60000}"
export SCHEDULE_INTERVAL="${SCHEDULE_INTERVAL:-15}"
SCHEDULE_EPOCH_MS="${SCHEDULE_EPOCH_MS:-1767225600000}"
export SCHEDULE_EPOCH_MS
ERROR_LOG="${ERROR_LOG:-/var/log/nginx/error.log}"
export ERROR_LOG
NGINX_BIN="${NGINX_BIN:-/opt/nginx/sbin/nginx}"
export NGINX_BIN

mkdir -p "$DATA_DIR" "$MEDIA_DIR" "$MAPS_DIR/live" "$CACHE_DIR" "$(dirname "$ERROR_LOG")"

# ---------- محلل DNS (مطلوب لـ proxy_pass بمتغيرات) ----------
DNS_RESOLVER="$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null)"
if [ -z "${DNS_RESOLVER}" ]; then
    DNS_RESOLVER="1.1.1.1"
fi
export DNS_RESOLVER

# ---------- تنزيل وسائط من MEDIA_URL (توافق مع النسخة السابقة) ----------
if [ -n "${MEDIA_URL:-}" ]; then
    echo "[entrypoint] تنزيل الوسائط من MEDIA_URL ..."
    python3 /opt/tv/scripts/media_fetch.py || echo "[entrypoint] تحذير: تعذّر تنزيل بعض الوسائط"
fi

# ---------- تهيئة البيانات: بذرة SEED_JSON + ترحيل المتغيرات القديمة ----------
# + استيراد ملفات media/ المدمجة في الصورة + أول توليد للخرائط ومواقع القنوات
echo "[entrypoint] تهيئة لوحة التحكم والقنوات ..."
python3 /opt/tv/scripts/admin.py --bootstrap || echo "[entrypoint] تحذير: فشلت التهيئة"

# ضمان وجود ملف المواقع حتى لو فشلت التهيئة (وإلا لن يقوم nginx بالإقلاع)
[ -s "$CHANNELS_CONF" ] || printf '# لا توجد قنوات بعد — أضفها من لوحة التحكم /admin/\n' > "$CHANNELS_CONF"

# ---------- تشغيل لوحة التحكم في الخلفية (مع إعادة تشغيل تلقائية) ----------
echo "[entrypoint] تشغيل لوحة التحكم على 127.0.0.1:${ADMIN_PORT} ..."
(
    while true; do
        python3 /opt/tv/scripts/admin.py >> /var/vod/admin.log 2>&1
        echo "[entrypoint] admin.py توقف — إعادة التشغيل بعد 3 ثوان" >> /var/vod/admin.log
        sleep 3
        # تقليم السجل حتى لا يتضخم
        if [ -f /var/vod/admin.log ] && [ "$(wc -c < /var/vod/admin.log)" -gt 1000000 ]; then
            tail -n 200 /var/vod/admin.log > /var/vod/admin.log.new
            mv /var/vod/admin.log.new /var/vod/admin.log
        fi
    done
) &

# ---------- توليد إعدادات nginx من القالب (خطوة حرجة) ----------
# nginx بُني بـ --prefix=/opt/nginx فيقرأ إعداداته من /opt/nginx/conf/nginx.conf
# يجب تصيير القالب إلى هذا المسار قبل الإقلاع — وإلا أقلع nginx بالإعدادات
# الافتراضية المدمجة عند التصريف (منفذ 80) وفشل فحص الصحة على منفذ $PORT.
# قائمة بيضاء: تُستبدل متغيرات القالب فقط ولا تُمس متغيرات nginx مثل $tv_proto
NGINX_CONF="$(dirname "$NGINX_BIN")/../conf/nginx.conf"
mkdir -p "$(dirname "$NGINX_CONF")"
envsubst '$PORT,$ERROR_LOG,$MAPS_DIR,$CHANNELS_CONF,$ADMIN_PORT,$WWW_DIR,$DNS_RESOLVER' \
    < /opt/tv/nginx.conf.template > "$NGINX_CONF"
echo "[entrypoint] أُنتجت إعدادات nginx: $NGINX_CONF"

# فحص الإعدادات قبل الإقلاع — عند الفشل نطبع الملف الناتج لتشخيص سريع من السجل
"$NGINX_BIN" -t > /tmp/nginx_test.log 2>&1
TEST_RC=$?
tail -n 3 /tmp/nginx_test.log
if [ "$TEST_RC" -ne 0 ]; then
    echo "[entrypoint] تحذير: فشل فحص إعدادات nginx — آخر 40 سطراً من الملف الناتج:"
    tail -n 40 "$NGINX_CONF"
fi

echo "[entrypoint] nginx جاهز على المنفذ ${PORT} — لوحة التحكم: /admin/ — صفحة القنوات: /"

# ---------- nginx في المقدمة ----------
exec "$NGINX_BIN" -g 'daemon off;'
