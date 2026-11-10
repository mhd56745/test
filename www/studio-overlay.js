/* =====================================================================
   studio-overlay.js — طبقة الاستوديو فوق مشغل الموقع (الشعار + الشريط النصي)
   تُحمَّل في كل صفحات المشغل وتقرأ الإعدادات من /api/branding كل 60 ثانية،
   فتنعكس تعديلات «الاستوديو» في اللوحة مباشرة دون إعادة تحميل الصفحة.
   ملاحظة: الطبقة تظهر في مشغل الموقع فقط — لا يمكن حرقها داخل البث الخام
   (VLC / أجهزة IPTV) لأن ذلك يتطلب ffmpeg غير الموجود في المحطة.
   ===================================================================== */
(function () {
    "use strict";

    if (window.TVStudio) { return; } // لا تكرر التهيئة

    var state = null;          // آخر إعدادات من الخادم
    var box = null;            // حاوية الفيديو
    var logoEl = null;         // عنصر الشعار
    var tickerEl = null;       // شريط النص (الخلفية)
    var tickerTxt = null;      // النص المتحرك
    var nowEl = null;          // شارة «يُبث الآن»
    var tickX = 0;             // إزاحة النص الحالية بالبكسل
    var tickW = 0;             // عرض النص بالبكسل
    var lastT = 0;             // زمن الإطار السابق
    var pageSetNow = false;    // هل غذّت الصفحة «يُبث الآن» بنفسها؟

    var CSS = ""
        + ".tvs-layer{position:absolute;inset:0;pointer-events:none;z-index:40;overflow:hidden}"
        + ".tvs-logo{position:absolute;filter:drop-shadow(0 2px 6px rgba(0,0,0,.55))}"
        + ".tvs-ticker{position:absolute;left:0;right:0;bottom:0;display:none;align-items:center;"
        + "overflow:hidden;white-space:nowrap;font-weight:700;letter-spacing:.2px;"
        + "text-shadow:0 1px 2px rgba(0,0,0,.4)}"
        + ".tvs-ticker span{display:inline-block;will-change:transform;direction:rtl}"
        + ".tvs-now{position:absolute;display:none;background:rgba(0,0,0,.62);color:#fff;"
        + "padding:4px 12px;border-radius:999px;font-size:13px;font-weight:700;"
        + "backdrop-filter:blur(2px);text-shadow:0 1px 2px rgba(0,0,0,.5)}"
        + ".tvs-now b{color:#ffd166}";

    // زوايا الشعار: (أسفل الشريط النصي ارتفاعاً عند الزوايا السفلية)
    var CORNERS = {
        "top-left":     { top: "4%", left: "4%" },
        "top-right":    { top: "4%", right: "4%" },
        "bottom-left":  { bottom: "9%", left: "4%" },
        "bottom-right": { bottom: "9%", right: "4%" }
    };

    function px(v) { return Math.round(v) + "px"; }

    function ensureLayer() {
        var video = document.querySelector("video");
        if (!video || !video.parentElement) { return false; }
        box = video.parentElement;
        if (getComputedStyle(box).position === "static") {
            box.style.position = "relative";
        }
        var layer = box.querySelector(".tvs-layer");
        if (!layer) {
            layer = document.createElement("div");
            layer.className = "tvs-layer";
            logoEl = document.createElement("img");
            logoEl.className = "tvs-logo";
            logoEl.alt = "شعار القناة";
            logoEl.draggable = false;
            nowEl = document.createElement("div");
            nowEl.className = "tvs-now";
            tickerEl = document.createElement("div");
            tickerEl.className = "tvs-ticker";
            tickerTxt = document.createElement("span");
            tickerEl.appendChild(tickerTxt);
            layer.appendChild(logoEl);
            layer.appendChild(nowEl);
            layer.appendChild(tickerEl);
            box.appendChild(layer);
        } else {
            logoEl = layer.querySelector(".tvs-logo");
            nowEl = layer.querySelector(".tvs-now");
            tickerEl = layer.querySelector(".tvs-ticker");
            tickerTxt = tickerEl ? tickerEl.querySelector("span") : null;
        }
        return true;
    }

    // ---------- الشعار ----------
    function applyLogo(logo, logoUrl) {
        if (!logoEl) { return; }
        if (!logo || !logo.enabled || !logoUrl) {
            logoEl.style.display = "none";
            return;
        }
        logoEl.style.display = "block";
        logoEl.src = logoUrl;
        var c = CORNERS[logo.position] || CORNERS["top-left"];
        logoEl.style.top = c.top || "auto";
        logoEl.style.left = c.left || "auto";
        logoEl.style.right = c.right || "auto";
        logoEl.style.bottom = c.bottom || "auto";
        logoEl.style.width = logo.width_pct + "%";
        logoEl.style.opacity = logo.opacity;
    }

    // ---------- شارة «يُبث الآن» ----------
    function applyNow() {
        if (!nowEl) { return; }
        var show = state && state.branding && state.branding.ticker &&
                   state.branding.ticker.now_playing && nowInfoTitle;
        if (!show) { nowEl.style.display = "none"; return; }
        // تظهر فوق الشريط النصي إن كان مفعّلاً، وإلا قرب أسفل الشاشة
        nowEl.style.bottom = (state.branding.ticker.enabled ? "9%" : "4%");
        nowEl.style.left = "4%";
        nowEl.style.display = "block";
        nowEl.textContent = "";
        var b = document.createElement("b");
        b.textContent = "يُبث الآن: ";
        nowEl.appendChild(b);
        nowEl.appendChild(document.createTextNode(nowInfoTitle));
    }

    // ---------- الشريط النصي ----------
    function applyTicker(t) {
        if (!tickerEl || !tickerTxt) { return; }
        if (!t || !t.enabled || !t.text) {
            tickerEl.style.display = "none";
            return;
        }
        tickerEl.style.display = "flex";
        tickerEl.style.background = t.bg;
        tickerEl.style.color = t.color;
        tickerTxt.textContent = t.text;
        var h = Math.max(20, Math.round(box.clientHeight * 0.075));
        tickerEl.style.height = px(h);
        tickerEl.style.paddingLeft = px(14);
        tickerEl.style.paddingRight = px(14);
        tickerTxt.style.fontSize = px(Math.max(12, Math.round(h * 0.52)));
        // قياس عرض النص وإعادة وضعه من البداية
        tickX = -(box.clientWidth + 40);
        requestAnimationFrame(function () {
            tickW = tickerTxt.getBoundingClientRect().width;
        });
    }

    function tickFrame(ts) {
        requestAnimationFrame(tickFrame);
        if (!state || !box || !tickerEl || tickerEl.style.display === "none") { lastT = ts; return; }
        var t = state.branding.ticker;
        var dt = lastT ? Math.min(100, ts - lastT) : 16;
        lastT = ts;
        var speed = (t.speed || 70) * Math.max(0.6, box.clientWidth / 900); // يتكيّف مع العرض
        tickX += (speed * dt) / 1000;
        if (tickW > 0 && tickX > box.clientWidth + 20) {
            tickX = -(tickW + 60);
        }
        tickerTxt.style.transform = "translateX(" + px(tickX) + ")";
    }

    // ---------- التطبيق الكامل ----------
    var nowInfoTitle = "";

    function applyAll() {
        if (!ensureLayer()) { return; }
        var b = (state && state.branding) || null;
        applyLogo(b && b.logo, state && state.logo_url);
        applyTicker(b && b.ticker);
        applyNow();
    }

    function fetchBranding() {
        return fetch("/api/branding").then(function (r) { return r.json(); }).then(function (d) {
            if (d && d.ok) { state = d; applyAll(); }
        }).catch(function () {});
    }

    // ---------- تغذية «يُبث الآن» ----------
    // 1) الصفحة تستدعي TVStudio.setNow(...) إن كانت تعرف القناة الحالية
    // 2) وإلا نستقصي /api/channels ونعرض أول قناة مكتبة تعمل
    function pollNowFallback() {
        setInterval(function () {
            if (pageSetNow || !state || !state.branding || !state.branding.ticker ||
                !state.branding.ticker.now_playing) { return; }
            fetch("/api/channels").then(function (r) { return r.json(); }).then(function (d) {
                var chans = (d && d.channels) || [];
                var ch = null;
                for (var i = 0; i < chans.length; i++) {
                    if (chans[i].type === "vod" && chans[i].playable) { ch = chans[i]; break; }
                }
                if (!ch) { return; }
                return fetch("/api/now/" + ch.id).then(function (r) { return r.json(); })
                    .then(function (n) {
                        if (n && n.now && n.now.title) { setNow(n.now.title); }
                    });
            }).catch(function () {});
        }, 30000);
    }

    function setNow(title) {
        pageSetNow = true;
        nowInfoTitle = String(title || "");
        applyNow();
    }

    // ---------- الإقلاع ----------
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    function boot() {
        if (!ensureLayer()) {
            setTimeout(boot, 700); // الصفحة لم تجهز الفيديو بعد
            return;
        }
        fetchBranding();
        requestAnimationFrame(tickFrame);
        pollNowFallback();
        setInterval(fetchBranding, 60000); // تعديلات الاستوديو تصل مباشرة
        window.addEventListener("resize", applyAll);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    window.TVStudio = {
        setNow: setNow,
        refresh: fetchBranding
    };
})();
