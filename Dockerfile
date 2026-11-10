# syntax=docker/dockerfile:1
# =====================================================================
#  koyeb-tv — صورة محطة تلفزية خفيفة: nginx + nginx-vod-module فقط
#  لا ffmpeg نهائياً — الوحدة تعيد تغليف MP4 إلى HLS مباشرةً
# =====================================================================

# ---------- مرحلة البناء ----------
FROM debian:bookworm-slim AS build

ARG NGINX_VERSION=1.28.3
ARG VOD_MODULE_REPO=https://github.com/dio-az/nginx-vod-module
ARG VOD_MODULE_REF=c739a51a70a5c64e2f92927f4f401e25f385cac8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential wget ca-certificates git \
        libpcre2-dev zlib1g-dev libssl-dev libxml2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN wget -q https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
    && tar xzf nginx-${NGINX_VERSION}.tar.gz

# وحدة nginx-vod-module — الفرع النشط الصيانَط (fork dio-az)
# مع تفعيل demuxer MKV: مصادر Matroska موجودة ومُجمَّعة في هذا الـ fork
# لكن تسجيلها في media_formats[] معطَّل بتعليق — نعيده ونضيف الترويسة
RUN git clone --depth 1 ${VOD_MODULE_REPO} vod-module \
    && cd vod-module && (git fetch --depth 1 origin ${VOD_MODULE_REF} && git checkout FETCH_HEAD || true) \
    && sed -i 's|#include "vod/mp4/mp4_format.h"|#include "vod/mp4/mp4_format.h"\n#include "vod/mkv/mkv_format.h"|' ngx_http_vod_module.c \
    && sed -i 's|^\([[:space:]]*\)// XXXXX: add &mkv_format,|\1\&mkv_format,|' ngx_http_vod_module.c \
    && grep -q '&mkv_format,' ngx_http_vod_module.c \
    && grep -q 'vod/mkv/mkv_format.h' ngx_http_vod_module.c

WORKDIR /build/nginx-${NGINX_VERSION}

RUN ./configure \
        --prefix=/opt/nginx \
        --add-module=/build/vod-module \
        --with-http_ssl_module \
        --with-http_v2_module \
        --with-http_sub_module \
        --with-threads \
        --with-http_gzip_static_module \
        --with-cc-opt="-O2" \
    && make -j"$(nproc)" \
    && make install

# ---------- مرحلة التشغيل ----------
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 gettext-base ca-certificates \
        libpcre2-8-0 zlib1g libssl3 libxml2 libcrypt1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /media /data /var/vod/maps /var/vod/cache /var/www /var/log/nginx

COPY --from=build /opt/nginx/ /opt/nginx/
COPY nginx/nginx.conf.template /opt/tv/nginx.conf.template
COPY scripts/ /opt/tv/scripts/
COPY www/ /var/www/
COPY media/ /media/

# إصلاح إلزامي: الرفع عبر واجهة GitHub يحفظ الملفات بصلاحية 644 (غير تنفيذية)
# ويكسر ذلك إقلاع الحاوية برمز الخطأ 13 (EACCES) — لذا نضبط الصلاحية وننظف \r
RUN sed -i 's/\r$//' /opt/tv/scripts/entrypoint.sh /opt/tv/scripts/*.py /opt/tv/nginx.conf.template \
    && chmod +x /opt/tv/scripts/entrypoint.sh

# ضع ملفات MP4 في مجلد media/ قبل البناء (اختياري — أو أضفها من اللوحة بالروابط)
# MAX_LIBRARY_MB=1500: قرص الخطة المجانية 2GB كامل — نترك هامشاً للصورة والنظام
# (وضع «عن بُعد» في اللوحة يبث من الروابط مباشرة بلا حد للقرص)
ENV PORT=8000 \
    APP_NAME="قناتي" \
    ADMIN_PASSWORD="admin123" \
    SEGMENT_DURATION=10000 \
    LIVE_WINDOW=60000 \
    MAX_VIDEO_MB=1024 \
    MAX_LIBRARY_MB=1500

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/healthz', timeout=4)" || exit 1

# التشغيل عبر bash مباشرة حتى لو فُقدت صلاحية التنفيذ للملف (حماية مزدوجة)
ENTRYPOINT ["/bin/bash", "/opt/tv/scripts/entrypoint.sh"]
