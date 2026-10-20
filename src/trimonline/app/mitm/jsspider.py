#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱迷茫 · JS 蜘蛛运行时（drpy2 on QuickJS）

干什么：
  把 TVBox 的 **drpy2 规则源**（配置里 type:3、api 指向 drpy2.min.js、ext 指向站点 js）
  真正跑起来 —— 不是模拟，是原样加载 drpy2 运行时本体 + drpy 改造版 cheerio +
  海阔视界那套 pdfh/pdfa 选择器方言，跑在一个内嵌的 QuickJS 引擎里。

为什么是 QuickJS：
  体积小（.so 约 6.6MB）、支持 ES2020（?. ?? 箭头函数）且能直接调 Python 宿主函数。
  fnOS 的 /usr/bin/python3 是 3.11，PyPI 有对应 manylinux wheel，不必带编译器。

对外接口（都是同步的，调用方自己丢线程池）：
  available()                -> (ok, 说明)
  info()                     -> {"engine":..., "sites":[...]} 供配置页展示
  probe(site, keyword)       -> 自检：home / category / search 三项，带耗时
  search(site, kw, page)     -> [{"vod_id","vod_name","vod_pic","vod_remarks"}]
  detail(site, vod_id)       -> {"vod_play_url":..., "vod_content":..., ...} 或 None
  home(site) / category(site, tid, pg) / play(site, flag, url)

设计取舍：
  · 一个 QuickJS 上下文 + 一把锁。drpy2 的 rule 是模块级状态，切源就重新 init。
    好处是内存有界（约 30MB），坏处是同一时刻只跑一个源的 JS —— 够用。
  · 站点 js 落盘缓存（conf/js_cache/），站点/网络抖动时仍可用上次的规则。
  · 任何异常都不外抛，只返回空 —— 绝不影响点播主链路。
"""
import base64
import gzip
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(HERE, "runtime")
CONF_DIR = os.environ.get("TRIM_CONF_DIR") or os.path.join(HERE, "conf")
CACHE_DIR = os.path.join(CONF_DIR, "js_cache")

NET_TIMEOUT = 12
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 11; M2007J3SC Build/RKQ1.200826.002; wv) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/77.0.3865.120 "
             "MQQBrowser/6.2 TBS/045714 Mobile Safari/537.36")

_IMPORT_ERR = ""


def _ensure_lib():
    """把 lib 目录塞进 sys.path（FPK 用 PYTHONPATH 也可以，这里兜底）"""
    for d in (os.path.join(HERE, "lib"), os.path.join(os.path.dirname(HERE), "lib")):
        if os.path.isdir(d) and d not in os.sys.path:
            os.sys.path.insert(0, d)


_ensure_lib()
try:
    import quickjs  # noqa
except Exception as e:                                   # pragma: no cover
    quickjs = None
    _IMPORT_ERR = "quickjs 引擎不可用：%s" % e


def available():
    """引擎是否可用 → (bool, 说明)"""
    if quickjs is None:
        return False, _IMPORT_ERR
    missing = [f for f in ("runtime_bundle.js", "host_pre.js", "host_post.js")
               if not os.path.exists(os.path.join(RUNTIME_DIR, f))]
    if missing:
        return False, "缺少运行时文件：%s" % ", ".join(missing)
    return True, "drpy2 + cheerio + QuickJS 就绪"


# ----------------------------------------------------------------- 网络 ---
def _idna_host(url):
    """把 URL 里非 ASCII 的**域名**转成 punycode。

    urllib 只用 latin-1 编码 Host 头，中文域名会直接抛
    UnicodeEncodeError('latin-1', 'www.某订阅站.cc', ...)。
    只动域名，路径/查询原样保留。
    """
    try:
        s = urllib.parse.urlsplit(url)
    except Exception:
        return url
    netloc = s.netloc
    if not netloc:
        return url
    try:
        netloc.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    host = s.hostname or ""
    if not host:
        return url
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except Exception:
        return url
    userinfo = ""
    if s.username:
        userinfo = s.username + ((":" + s.password) if s.password else "") + "@"
    try:
        port = ":%d" % s.port if s.port else ""
    except ValueError:
        port = ""
    return urllib.parse.urlunsplit(
        (s.scheme, userinfo + host_ascii + port, s.path, s.query, s.fragment))


def encode_url(url):
    """URL 里可能出现非 ASCII，分两种情形处理：
       · **域名**含中文 → 转 punycode（Host 头只能是 ASCII）
       · 路径/查询含中文或 gbk 站点塞进来的原始字节 → 百分号编码
    """
    url = _idna_host(url)
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    if all(ord(c) <= 0xFF for c in url):
        return "".join(c if ord(c) < 0x80 else "%%%02X" % ord(c) for c in url)
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def http_fetch(url, options):
    """drpy 的 req(url, options) —— 返回 {"content": str, "headers": {...}}"""
    method = (options.get("method") or "GET").upper()
    headers = dict(options.get("headers") or {})
    body = options.get("body", options.get("data"))
    timeout = min(float(options.get("timeout") or 10000) / 1000.0, 30.0)
    encoding = options.get("encoding") or "utf-8"
    want_b64 = options.get("buffer") == 2

    data = None
    if body is not None and method in ("POST", "PUT", "PATCH"):
        if isinstance(body, (dict, list)):
            data = urllib.parse.urlencode(body).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = str(body).encode("utf-8", "replace")

    req = urllib.request.Request(encode_url(url), data=data, method=method)
    for k, v in headers.items():
        if v is not None:
            try:
                req.add_header(k, str(v))
            except Exception:
                pass
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, code = resp.read(), resp.status
            hdrs = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        raw, code, hdrs = e.read(), e.code, dict(e.headers.items())

    ce = (hdrs.get("Content-Encoding") or "").lower()
    if ce == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif ce == "deflate":
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    hdrs["status"] = code
    if want_b64:
        return {"content": base64.b64encode(raw).decode(), "headers": hdrs}
    try:
        text = raw.decode(encoding or "utf-8", "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    return {"content": text, "headers": hdrs}


# ------------------------------------------------------------- 站点 JS 缓存 ---
_cache_lock = threading.Lock()


def fetch_site_js(src, force=False):
    """src 可以是 http(s) URL、**base64 文本、或直接的 JS 原文。带落盘缓存。"""
    src = (src or "").strip()
    if not src:
        return "", "空"
    if not src.lower().startswith(("http://", "https://")):
        return _decode_maybe_b64(src), "内联"
    key = hashlib.md5(src.encode()).hexdigest()[:16]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".js")
    with _cache_lock:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 1e9
        if not force and age < 86400 and os.path.getsize(path) > 0:
            return open(path, encoding="utf-8", errors="replace").read(), "缓存"
    try:
        req = urllib.request.Request(encode_url(src), headers={"User-Agent": UA_MOBILE})
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace")
        text = _decode_maybe_b64(text)
        if len(text) < 20:
            raise ValueError("内容过短(%d)" % len(text))
        with _cache_lock:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return text, "下载"
    except Exception as e:
        with _cache_lock:
            if os.path.exists(path):
                return open(path, encoding="utf-8", errors="replace").read(), "缓存(下载失败:%s)" % e
        raise


def _decode_maybe_b64(text):
    t = (text or "").strip()
    if t.startswith("**"):
        try:
            return base64.b64decode(t[2:]).decode("utf-8", "replace")
        except Exception:
            return t
    return t


# --------------------------------------------------------------- 引擎本体 ---
class _Engine(object):
    """全局单例。所有 QuickJS 操作都在锁里，供线程池调用。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.ctx = None
        self.cur_site = None          # 当前 init 的站点标识
        self.cur_key = None
        self.logs = []
        self.err = ""
        self.stats = {"calls": 0, "errors": 0}

    # -- 宿主函数 -------------------------------------------------------
    def _jslog(self, level, argstr):
        try:
            parts = json.loads(argstr) if isinstance(argstr, str) else [argstr]
        except Exception:
            parts = [argstr]
        msg = " ".join(str(x) for x in (parts if isinstance(parts, list) else [parts]))
        self.logs.append(msg)
        if len(self.logs) > 400:
            del self.logs[:200]

    def _req(self, url, options_json):
        try:
            opts = json.loads(options_json or "{}")
        except Exception:
            opts = {}
        if not opts.get("headers"):
            opts["headers"] = {"User-Agent": UA_MOBILE}
        try:
            return json.dumps(http_fetch(url, opts))
        except Exception as e:
            self.logs.append("[HTTP ERR] %s -> %s" % (url, e))
            return json.dumps({"content": "", "headers": {"error": str(e)}})

    def _boot(self):
        """建立上下文并注入运行时（只做一次）"""
        if self.ctx is not None:
            return
        ctx = quickjs.Context()
        ctx.add_callable("__host_req", self._req)
        ctx.add_callable("__host_urljoin",
                         lambda b, u: urllib.parse.urljoin(b or "", u or ""))
        ctx.add_callable("__host_log", self._jslog)
        # 内存版 local 存储（cookie 等）
        self.store = {}
        ctx.add_callable("__host_local_get", lambda k: self.store.get(k))
        ctx.add_callable("__host_local_set",
                         lambda k, v: self.store.__setitem__(k, v))
        ctx.add_callable("__host_local_del", lambda k: self.store.pop(k, None))
        ctx.eval("var console={log:function(){__host_log('log',JSON.stringify("
                 "Array.prototype.slice.call(arguments)))},"
                 "error:function(){__host_log('err',JSON.stringify("
                 "Array.prototype.slice.call(arguments)))},warn:function(){},info:function(){}};")
        for f in ("host_pre.js", "runtime_bundle.js", "host_post.js"):
            ctx.eval(open(os.path.join(RUNTIME_DIR, f), encoding="utf-8").read())
        self.ctx = ctx

    def _init_site(self, site_stem, js_text):
        """把站点规则加载进 drpy（同一份就跳过）"""
        if self.cur_key == site_stem:
            return
        self.ctx.eval("globalThis.__site_js = %s;" % json.dumps(js_text))
        self.ctx.eval("globalThis.__drpy.init(globalThis.__site_js)")
        self.cur_key = site_stem

    def call(self, site, fn, *args):
        """在锁内初始化站点并调用 drpy 的某个方法（返回 JSON 字符串）"""
        with self.lock:
            self._boot()
            self.stats["calls"] += 1
            key = site.get("_stem") or site.get("name") or "?"
            try:
                self._init_site(key, site["_js"])
                a = ",".join(json.dumps(x, ensure_ascii=False) for x in args)
                out = self.ctx.eval("globalThis.__drpy.%s(%s)" % (fn, a))
                return out if isinstance(out, str) else json.dumps(out or {})
            except Exception as e:
                self.stats["errors"] += 1
                self.logs.append("[%s] %s: %s" % (key, fn, e))
                raise


# 每个站点一个独立的 QuickJS 上下文：drpy 的 rule 是模块级状态，共用上下文就必须
# 全局串行（一个搜索要 1 次 search + N 次 detail，串行能拖到十几秒）。
# 分开之后不同源可以真并行，代价是每个上下文约 30MB、启动时 eval 一遍运行时。
_ENGINES = {}
_ENGINES_LOCK = threading.Lock()
MAX_ENGINES = 6


def _engine_for(stem):
    with _ENGINES_LOCK:
        eng = _ENGINES.get(stem)
        if eng is None:
            if len(_ENGINES) >= MAX_ENGINES:      # 简单 FIFO 淘汰，防内存无界
                _ENGINES.pop(next(iter(_ENGINES)), None)
            eng = _Engine()
            _ENGINES[stem] = eng
        return eng


def _call(site, fn, *args):
    key = site.get("_stem") or site.get("name") or "?"
    return _engine_for(key).call(site, fn, *args)


def _prep(site):
    """把配置里的源补上 _js / _stem 字段（并做磁盘缓存）"""
    s = dict(site or {})
    if not s.get("js"):
        raise ValueError("JS 源没有 js 字段")
    if s.get("_js"):
        return s
    text, how = fetch_site_js(s["js"])
    s["_js"] = text
    s["_how"] = how
    s["_stem"] = hashlib.md5(str(s.get("js")).encode()).hexdigest()[:16]
    return s


# --------------------------------------------------------------- 对外接口 ---
def _loads(s):
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def home(site):
    return _loads(_call(_prep(site), "home"))


def category(site, tid, pg=1, extend=None):
    return _loads(_call(_prep(site), "category", str(tid), int(pg), False, extend or {}))


def search(site, kw, page=1):
    d = _loads(_call(_prep(site), "search", kw, False, int(page)))
    return [it for it in (d.get("list") or []) if it.get("vod_name")]


def detail(site, vod_id):
    d = _loads(_call(_prep(site), "detail", str(vod_id)))
    lst = d.get("list") or []
    return lst[0] if lst else None


def play(site, flag, url):
    """免嗅：把剧集地址过一遍站点自己的 play_parse / lazy 规则"""
    return _loads(_call(_prep(site), "play", flag or "", url, ""))


def probe(site, keyword="你好"):
    """自检：home / category / search 各跑一次，带耗时与条数"""
    t0 = time.time()
    res = {"name": site.get("name") or "", "ok": False, "ms": 0,
           "classes": 0, "cate": 0, "search": 0, "first": "", "err": "", "how": ""}
    try:
        s = _prep(site)
        res["how"] = s.get("_how", "")
        h = home(s)
        cls = h.get("class") or []
        res["classes"] = len(cls)
        if cls:
            res["first"] = cls[0].get("type_name") or ""
            try:
                c = category(s, cls[0].get("type_id"), 1)
                res["cate"] = len(c.get("list") or [])
            except Exception as e:
                res["err"] = "分类: %s" % str(e)[:80]
        try:
            res["search"] = len(search(s, keyword, 1))
        except Exception as e:
            res["err"] = (res["err"] + " 搜索: " + str(e)[:60]).strip()
        res["ok"] = bool(res["classes"] or res["cate"] or res["search"])
    except Exception as e:
        res["err"] = str(e)[:140]
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def info():
    ok, why = available()
    with _ENGINES_LOCK:
        engs = list(_ENGINES.values())
    stats = {"calls": 0, "errors": 0, "contexts": len(engs)}
    for e in engs:
        stats["calls"] += e.stats["calls"]
        stats["errors"] += e.stats["errors"]
    return {"available": ok, "why": why,
            "engine": "QuickJS + drpy2" if ok else "",
            "stats": stats if ok else {}}


def logs(n=40):
    with _ENGINES_LOCK:
        engs = list(_ENGINES.values())
    out = []
    for e in engs:
        out.extend(e.logs)
    return out[-n:]


if __name__ == "__main__":                              # 命令行自测
    import sys
    ok, why = available()
    print("引擎:", ok, why)
    if len(sys.argv) > 1:
        site = {"name": "测试", "js": sys.argv[1]}
        kw = sys.argv[2] if len(sys.argv) > 2 else "你好"
        print(json.dumps(probe(site, kw), ensure_ascii=False, indent=2))
