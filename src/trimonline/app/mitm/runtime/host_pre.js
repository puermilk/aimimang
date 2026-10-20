// 宿主预置：必须在 runtime_bundle.js 之前执行
globalThis.self = globalThis;

// ---- pdfh/pdfa/pd：延迟绑定到 Jsoup 实例（drpy2 在求值期就取这三个函数）----
globalThis.__jsoup = null;
globalThis.pdfh = function (h, p) { return globalThis.__jsoup.pdfh(h, p); };
globalThis.pdfa = function (h, p) { return globalThis.__jsoup.pdfa(h, p); };
globalThis.pd = function (h, p, b) { return globalThis.__jsoup.pd(h, p, b); };

// ---- 网络请求：交给 Python 宿主（同步）----
globalThis.req = function (url, options) {
    var r = globalThis.__host_req(String(url), JSON.stringify(options || {}));
    return JSON.parse(r);
};
globalThis.__host_req = globalThis.__host_req || function () { throw new Error('host_req 未注入'); };

// ---- 本地存储：交给 Python 宿主 ----
globalThis.local = {
    get: function (k, f) { var v = globalThis.__host_local_get(String(k)); return v === null || v === undefined ? f : v; },
    set: function (k, v) { globalThis.__host_local_set(String(k), String(v)); },
    'delete': function (k) { globalThis.__host_local_del(String(k)); }
};
globalThis.__host_local_get = globalThis.__host_local_get || function () { return null; };
globalThis.__host_local_set = globalThis.__host_local_set || function () { };
globalThis.__host_local_del = globalThis.__host_local_del || function () { };

// ---- 杂项宿主能力 ----
globalThis.getProxy = function () { return 'http://127.0.0.1:9978/proxy?do=js'; };
globalThis.os = { open: function () { return -1; }, read: function () { return 0; }, close: function () { } };

// ---- urljoin：交给 Python 宿主（QuickJS 没有 URL 对象）----
globalThis.joinUrl = function (base, url) {
    return globalThis.__host_urljoin(
        (base === undefined || base === null) ? '' : String(base),
        (url === undefined || url === null) ? '' : String(url));
};
globalThis.__host_urljoin = globalThis.__host_urljoin || function (b, u) { return u; };

// ---- FongMi/TVBox 的 $js 语法糖：$js.toString(fn) 取函数源码 ----
globalThis.$js = {
    toString: function (f) { try { return String(f); } catch (e) { return ''; } },
    toString2: function (f) { return String(f); }
};
