#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱迷茫 · 在线源配置页（内置 Web UI）

由 mitm.py 挂在 /v/__mitm__/ 下，浏览器打开：

    http://<NAS 的 IP>:5666/v/__mitm__/config

它做四件事：
  1. 读写 conf/sources.json（增删改源、主用 / 兜底分组、单线路 / 多线路标记）
  2. 导入一个 TVBox 配置地址：自动挑出其中「能直接用」的明文采集接口，
     并把网盘类、加密 JAR、JS 蜘蛛一类用不了的全部跳过（并说明原因）
  3. 保存后立刻逐个测响应时间 / 命中数 / 可放线路数
  4. 按测速结果给出推荐方案（谁能当主用、谁只配当兜底），可一键应用

只依赖标准库；探测逻辑复用 source_check.py，保证「配置页测出来的」与
「真正播放时走的」完全一致。
"""
import concurrent.futures
import base64
import gzip
import json
import re
import ssl
import time
import urllib.request
import zlib

import source_check

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 很多 TVBox 配置站按 User-Agent 发牌：用浏览器 UA 往往只能拿到一句「你好！」，
# 只有客户端 UA 才给真配置。按顺序试，谁先拿到配置就用谁。
CFG_UAS = (
    "okhttp/3.12.0",
    "okHttp/Mod-1.5u.0.0",
    "Dalvik/2.1.0 (Linux; U; Android 11; M2007J3SC Build/RKQ1.200826.002)",
    UA,
)
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

LINES_LABEL = {"single": "单线路", "multi": "多线路", "auto": "自动"}


# --------------------------------------------------------------- TVBox 导入 ---
# 网盘类站点特征：名字里带这些字，或 ext 里有 Cloud-drive，或 key 就是下面这些
NETDISK_WORDS = ("网盘", "云盘", "盘搜", "盘她", "盘他", "夸克", "夸父", "四盘",
                 "优汐", "阿里", "115", "天翼", "迅雷", "磁力", "阿里云盘")
NETDISK_KEYS = {"UC", "ZPan", "JPan", "YpanSo", "BpanSo", "MDrive", "抠搜",
                "push_agent", "点我切源"}

# 这些 api 一看就不是「能直接调用的采集接口」
_CMS_HINT = re.compile(r"(api\.php|/provide/vod|/api\.php|vod/|/at/|xml)", re.I)


def _netdisk_hint(name, key, ext, api="", site_url=None):
    """这个站点像不像网盘类：像就返回一句「凭什么」的理由，否则返回 ""。

    只给理由，不给结论 —— 调用方自己决定怎么措辞（有的站点既是网盘、
    api 又是加密 JAR，两个事实都得说，不然用户会以为是网盘才跑不了）。
    """
    for w in NETDISK_WORDS:
        if w in (name or "") or w in (key or ""):
            return "名字/key 里有「%s」" % w
    if key in NETDISK_KEYS:
        return "key=%s" % key
    if isinstance(ext, dict):
        if "Cloud-drive" in ext:
            return "ext 是 Cloud-drive 网盘驱动"
        su = str(site_url if site_url is not None
                 else ext.get("siteUrl", ""))
        if "pan" in su.lower():
            return "ext.siteUrl 指向 %s" % su[:40]
    low = (api or "").lower()
    if "pan." in low or "quark" in low or "aliyundrive" in low:
        return "接口域名看着是网盘"
    return ""


def _js_source(s):
    """TVBox type:3（drpy JS 蜘蛛）→ 我们自带引擎能跑的站点规则。

    TVBox 约定：type=3 时 api 指向运行时（drpy2.min.js），ext 指向**站点规则**。
    我们自带 drpy2 运行时，所以只取 ext。
    返回 (js, why)：js 非空表示可用（网址 / **base64 / 内联规则文本）。"""
    api = str(s.get("api") or "").strip()
    ext = s.get("ext")
    name = str(s.get("name") or "")
    key = str(s.get("key") or "")
    typ = str(s.get("type") or "")

    # ---- 先排掉「根本不是蜘蛛」和「我们一定跑不了的」----
    if typ == "3" or api.lower().split("?")[0].endswith((".js", ".mjs")):
        nd = _netdisk_hint(name, key, ext, api)
        if api.startswith(("csp_", "js_", "py_", "php_")):
            w = "加密 JAR 蜘蛛（需要原厂 Java 运行时，已剔除）"
            return "", (w + "；而且它确实是网盘类（%s）" % nd) if nd else w
        if nd:
            return "", "网盘类（%s）" % nd
    else:
        return "", "不是 JS 蜘蛛"

    if isinstance(ext, str) and ext.strip():
        e = ext.strip()
        if e.lower().startswith(("http://", "https://")):
            return e, ""
        if e.startswith("**"):                       # drpy 的 base64 规则
            return e, ""
        if "{" in e and ("rule" in e or "url" in e):
            return e, ""
        return "", "ext 不是可用的规则地址"
    if isinstance(ext, dict) and ext:
        # 规则直接内联成对象：转成 var rule={...} 交给 drpy
        return "var rule=" + json.dumps(ext, ensure_ascii=False) + ";", ""
    # type:3 但 api 本身就是站点规则（没有 ext）——只认那种明显不是运行时的
    low = api.lower().split("?")[0]
    if low.endswith((".js", ".mjs")) and not re.search(r"drpy|spider|catvod|runtime|\.min\.js",
                                                       low):
        return api, ""
    return "", "JS 蜘蛛没有给出 ext 规则（api 只是运行时）"


def _why_unusable(s):
    """判断一个 TVBox 站点为什么用不了；能用则返回 ""。

    说清楚「凭什么」——配置页会把这句话原样显示在「被跳过的站点明细」里，
    用户要能自己核对判定对不对（不对就点「仍要加入」去实测）。
    """
    api = str(s.get("api") or "").strip()
    key = str(s.get("key") or "").strip()
    name = str(s.get("name") or "").strip()
    ext = s.get("ext")
    if not api:
        return "没有 api 字段"
    nd = _netdisk_hint(name, key, ext, api)
    if api.startswith(("csp_", "js_", "py_", "php_")):
        w = "加密 JAR / 蜘蛛（需要 Java 引擎，纯 Python 跑不了）"
        return (w + "；而且它确实是网盘类（%s）" % nd) if nd else w
    if nd:
        return "网盘类（%s）" % nd
    if api.lower().endswith((".js", ".mjs", ".json")) or "/raw/" in api.lower():
        return "JS 蜘蛛（没有标注 type:3 / 没给 ext 规则，已跳过）"
    if not api.startswith(("http://", "https://")):
        return "不是 http(s) 接口"
    if not _CMS_HINT.search(api):
        return "不像苹果CMS 采集接口（拿不准，已跳过）"
    return ""


def _skip_cat(why):
    """把跳过原因归类，给界面上的按钮用。

    「网盘」优先：只要理由里带网盘字样，就归 netdisk —— 界面上的
    「网盘类的也全部加入」要能把它们一个不漏地列给用户去核
    （其中有的同时是加密 JAR，加进去也是 ❌，按钮旁边会说明）。
    """
    w = why or ""
    if "网盘" in w:
        return "netdisk"
    if "JAR" in w or "蜘蛛" in w:
        return "spider"
    if w == "同名重复":
        return "dup"
    return "other"


def _loads_config(body):
    """把配置文本解成 dict。容忍 // 注释、BOM、前后夹的说明文字、尾部垃圾。"""
    if body.startswith("\ufeff"):
        body = body[1:]
    lines = [l for l in body.splitlines() if not l.strip().startswith("//")]
    body = "\n".join(lines)
    dec = json.JSONDecoder()
    tried = 0
    for m in re.finditer(r"\{", body):
        if tried >= 8:
            break
        tried += 1
        try:
            conf, _ = dec.raw_decode(body, m.start())
        except Exception:
            continue
        if isinstance(conf, dict):
            return conf
    raise ValueError("不是合法 JSON（收到的开头是 %r）" % body.lstrip()[:60])


def parse_tvbox(text):
    """解析一份 TVBox 配置文本 → (CMS 源, 跳过的源, JS 源, 元信息)。"""
    conf = _loads_config(text)
    if not isinstance(conf, dict):
        raise ValueError("配置顶层不是对象")
    sites = conf.get("sites")
    if not isinstance(sites, list):
        raise ValueError("配置里没有 sites 数组")

    kept, skipped, jskept = [], [], []
    seen = set()

    def _skip(item, why):
        """记一条被跳过的站点：原因 + 分类（界面按分类给「仍要加入」按钮）。"""
        item["why"] = why
        item["cat"] = _skip_cat(why)
        skipped.append(item)

    for s in sites:
        if not isinstance(s, dict):
            continue
        name0 = str(s.get("key") or s.get("name") or "").strip()[:24]

        # ---- 先认 JS 蜘蛛（type:3）：这是本版本新救回来的那批 ----
        js, jwhy = _js_source(s)
        if js:
            label = str(s.get("name") or "").strip()[:40]
            name = name0 or label or "JS源"
            item = {"name": name, "label": label, "js": js,
                    "referer": "", "lines": "auto", "kind": "js"}
            if name in seen:
                _skip(item, "同名重复")
                continue
            seen.add(name)
            ext = js
            if ext.lower().startswith(("http://", "https://")):
                item["referer"] = "https://" + ext.split("//", 1)[-1].split("/", 1)[0] + "/"
            jskept.append(item)
            continue

        why = jwhy if jwhy and jwhy != "不是 JS 蜘蛛" else _why_unusable(s)
        api = str(s.get("api") or "").strip()
        item = {
            "name": name0,
            "label": str(s.get("name") or "").strip()[:40],
            "api": api,
            "referer": "",
        }
        if why:
            _skip(item, why)
            continue
        host = api.split("//", 1)[-1].split("/", 1)[0]
        if not item["name"]:
            item["name"] = host.split(".")[0]
        if item["name"] in seen:
            _skip(item, "同名重复")
            continue
        seen.add(item["name"])
        item["referer"] = "https://" + host + "/"
        kept.append(item)

    why_count = {}
    for s in skipped:
        k = str(s.get("why") or "其它")[:24]
        why_count[k] = why_count.get(k, 0) + 1
    meta = {
        "spider": str(conf.get("spider") or "")[:80],
        "sites": len(sites),
        "kept": len(kept),
        "skipped": len(skipped),
        "js": len(jskept),
        # 跳过原因分布：让「这份配置为什么一个都用不了」一眼看明白
        "why": sorted(why_count.items(), key=lambda kv: -kv[1]),
    }
    return kept, skipped, jskept, meta


def _http_get(url, ua, timeout=40, limit=6 * 1024 * 1024):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read(limit)


def _maybe_unzip(b):
    """有的站点先把配置 gzip / deflate，再套一层 base64。"""
    if not b:
        return None
    if b[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(b)
        except Exception:
            return None
    if b[:1] == b"\x78":
        try:
            return zlib.decompress(b)
        except Exception:
            return None
    return None


def _text_like_json(b):
    """字节流看起来像配置（以 { 或 [ 开头）就返回文本，否则 None。"""
    if not b:
        return None
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            t = b.decode(enc)
        except Exception:
            continue
        if t.lstrip("\ufeff \t\r\n")[:1] in "{[":
            return t
    return None


def _payload_from(raw):
    """响应字节 → 配置文本；挖不到返回 None。

    TVBox 配置常见的两种伪装：
      · 直接就是一坨 JSON（可能带 BOM / // 注释 / 前后夹着说明文字）
      · **一张真图片**，后面贴「随便几位 + ** + base64(配置)」——
        所以不能按文本 decode，得按字节找最后一个 ** 再解 base64。
        （不少订阅站就是这么发的：JPEG 在前，配置 base64 在后。）
    """
    if not raw:
        return None

    # ① 有 ** 就按 ** 后面的 base64 解
    i = raw.rfind(b"**")
    if i >= 0:
        blob = b"".join(raw[i + 2:].split())
        try:
            dec = base64.b64decode(blob, validate=False)
        except Exception:
            dec = b""
        for cand in (dec, _maybe_unzip(dec)):
            t = _text_like_json(cand)
            if t:
                return t

    # ② 本来就是文本（或 gzip 过的文本）
    for cand in (raw, _maybe_unzip(raw)):
        t = _text_like_json(cand)
        if t:
            return t
    return None


def _why_friendly(e):
    """把底层异常翻译成一句人话。"""
    s = repr(e)
    if "gaierror" in s or "Name or service not known" in s or "nodename" in s:
        return "域名解析不了（地址可能写错了）"
    if "Connection refused" in s:
        return "对方拒绝连接"
    if "timed out" in s or "timeout" in s or "TimeoutError" in s:
        return "超时"
    if "CERTIFICATE" in s or "SSL" in s:
        return "证书 / SSL 有问题"
    if "HTTPError" in s:
        return "服务器返回错误"
    if "Too many redirects" in s:
        return "重定向次数过多"
    return s[:50]


def fetch_config(url, timeout=40):
    """下载一份 TVBox 配置（.json / 伪装成 .jpg / 伪装成图片+base64 都认）。

    踩过的三个坑，都是真站点上的：
      1) urllib 只会用 latin-1 编码 Host —— 中文域名要先转 punycode（否则
         UnicodeEncodeError: 'latin-1', 'www.某订阅站.cc' ...）；
      2) 站点按 UA 发牌 —— 浏览器 UA 常常只回一句「你好！」；
      3) 配置可能整段是「图片 + base64」。
    """
    url = source_check.idna_url(url)      # 坑 1
    reasons = []
    for ua in CFG_UAS:                    # 坑 2：轮着试
        try:
            raw = _http_get(url, ua, timeout)
        except Exception as e:
            reasons.append(_why_friendly(e))
            continue
        text = _payload_from(raw)         # 坑 3：按字节挖
        if text is not None:
            return text
        hint = raw[:40].decode("utf-8", "replace").replace("\n", " ").strip()
        reasons.append("拿到的不是配置（%d 字节，开头是「%s」）" % (len(raw), hint))
    uniq = list(dict.fromkeys(reasons))   # 四个 UA 同一个原因就只说一次
    raise ValueError("抓不到配置（试了 %d 个 UA）：%s"
                     % (len(CFG_UAS), "；".join(uniq)[:220]))


# --------------------------------------------------------------- 测速 / 推荐 ---
def _web(base):
    """从 api 地址猜一个能当 referer 的首页。"""
    m = re.match(r"(https?://[^/]+)", base or "")
    return (m.group(1) + "/") if m else ""


def test_sites(sites, keyword="凡人", timeout=25.0, workers=6):
    """并发探测每个源，返回逐条结果（含响应时间 / 命中 / 线路数）。"""
    out = []

    def one(s):
        api = str(s.get("api") or "").strip()
        ref = str(s.get("referer") or "").strip() or _web(api)
        r = source_check.probe(api, ref, keyword, timeout)
        lines = str(s.get("lines") or "auto").lower()
        if lines not in ("single", "multi"):
            lines = "auto"
        return {
            "name": str(s.get("name") or api)[:24],
            "api": api,
            "referer": ref,
            "ms": r.get("ms"),
            "hits": r.get("hits"),
            "playable": r.get("playable"),
            "routes": r.get("routes"),
            "title": r.get("title") or "",
            "fmt": r.get("fmt") or "",
            "err": r.get("err") or "",
            "lines": lines,
            "level": str(s.get("level") or "primary"),
        }

    if not sites:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(one, sites):
            out.append(res)
    return out


def recommend(results, cms_limit=0, js_limit=4, mode="priority"):
    """按「可放线路数优先、响应时间其次」挑出建议启用的源。
    cms_limit=0 时推荐 4 个采集源（旧默认）；>0 时按用户设置的数量推荐。
    js_limit=0 表示 JS 源不限。"""
    ok = [r for r in results if (r.get("playable") or 0) > 0]
    dead = [r for r in results if not (r.get("playable") or 0)]
    is_js = lambda r: bool(r.get("js") or r.get("kind") == "js")   # noqa: E731

    def rank(r):
        # 能放 → 先看线路多少（多线路更抗失效），再看快慢
        return (-(r.get("playable") or 0), r.get("ms") or 9e9)

    ok.sort(key=rank)
    cms_ok = [r for r in ok if not is_js(r)]
    js_ok = [r for r in ok if is_js(r)]
    n = cms_limit if cms_limit > 0 else 4
    chosen = cms_ok[:n]
    half = (len(chosen) + 1) // 2
    primary = chosen[:half]
    fallback = chosen[half:]
    js_chosen = js_ok if js_limit <= 0 else js_ok[:js_limit]
    for r in cms_ok + js_ok:
        r["suggest"] = ""
    for r in primary:
        r["suggest"] = "primary"
    for r in fallback:
        r["suggest"] = "fallback"
    for r in js_chosen:
        r["suggest"] = "js"
    for r in dead:
        r["suggest"] = ""

    def tag(r):
        ln = LINES_LABEL.get(detect_lines(r), "自动")
        return "%s（%.0fms·%s）" % (r["name"], r["ms"] or 0, ln)

    if not ok:
        text = ("没有测出可用源：\n"
                "  · 确认 NAS 能直连这些接口（不需要梯子）\n"
                "  · 换个搜索关键词再测一次（有些源只是没有这部片子）\n"
                "  · 换一批源，或导入一份 TVBox 配置地址\n")
    else:
        if primary:
            text = "主用：" + "、".join(tag(r) for r in primary)
        elif js_ok:
            text = "主用：（没有可用的采集源）"
        else:
            text = "主用：（无）"
        if fallback:
            text += "\n兜底：" + "、".join(tag(r) for r in fallback)
        if js_chosen:
            text += "\nJS 源：" + "、".join(tag(r) for r in js_chosen)
        if mode == "parallel":
            text += ("\n\n说明：当前是「全部同时」——主用和兜底会一起查询，结果合并，"
                     "不再等主用落空才查兜底。源越多越慢，建议按实际需要设置数量。")
        else:
            text += ("\n\n说明：主用按顺序查，命中即用；只有主用全部落空才轮到底源。"
                     "多线路的源更抗单条线路失效，所以同等速度下优先当主用。")
        text += "JS 源与采集源并行查询，慢一些但收录面完全不同。"
    return {"primary": [r["name"] for r in primary],
            "fallback": [r["name"] for r in fallback],
            "js": [r["name"] for r in js_chosen],
            "text": text,
            "usable": len(ok),
            "dead": len(dead),
            "source_mode": mode,
            "source_limit": cms_limit,
            "js_source_limit": js_limit}


def detect_lines(r):
    """由探测结果推断单线路 / 多线路。"""
    if r.get("lines") in ("single", "multi"):
        return r["lines"]
    routes = r.get("routes") or 0
    return "multi" if routes > 1 else "single"


# ------------------------------------------------------------------ 前端页面 ---
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>爱迷茫 · 在线源设置</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
      --acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 12px;color:var(--dim);font-weight:600}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px;margin-bottom:16px}
.note{border-left:3px solid var(--warn);background:#1f1a0e;color:#e3d3a3;
      font-size:12.5px;padding:10px 12px;border-radius:6px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);
      vertical-align:middle}
th{color:var(--dim);font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
input,select{background:#0d1117;border:1px solid var(--line);color:var(--fg);
      border-radius:6px;padding:7px 9px;font-size:13px;width:100%}
input:focus,select:focus{outline:none;border-color:var(--acc)}
button{background:var(--acc);color:#04121f;border:0;border-radius:6px;
      padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:1.1fr 2.4fr 1.6fr .9fr .9fr auto;
      gap:8px;align-items:center}
.grid.js{grid-template-columns:1fr 2.4fr 1.2fr .8fr .9fr auto}
.grid.head{color:var(--dim);font-size:12px;font-weight:600}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11.5px;
     border:1px solid var(--line)}
.tag.ok{color:var(--ok);border-color:#1f6f36}
.tag.warn{color:var(--warn);border-color:#6b5518}
.tag.bad{color:var(--bad);border-color:#7d2620}
.tag.dim{color:var(--dim)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
      word-break:break-all;color:var(--dim)}
pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;
    padding:12px;white-space:pre-wrap;font-size:12.5px;margin:0}
.sep{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:12px;
     margin:14px 0}
.sep::before,.sep::after{content:"";flex:1;height:1px;background:var(--line)}
#log{font-size:12.5px;color:var(--dim);white-space:pre-wrap;min-height:18px}
</style></head>
<body><div class="wrap">

<h1>爱迷茫</h1>
<div class="sub">飞牛影视在线源插件 · 源设置（<span class="mono" id="file">conf/sources.json</span>）</div>

<div class="note"><b>免责声明</b>：本插件只是一个「播放器侧」的中转工具，不含任何影视内容，
也不内置任何片源。你在下面填写的源地址、以及由此播放的内容，均由你自己提供并自负其责；
请仅接入你有权访问的内容，并遵守当地法律法规与源站的服务条款。作者不对任何第三方源的
合法性、可用性和内容负责。如不同意，请不要使用本插件。</div>

<div id="warn" style="display:none"></div>

<div class="card">
  <h2>导入 TVBox 配置地址</h2>
  <div class="row">
    <input id="cfgurl" placeholder="粘贴一份 TVBox 订阅地址（.json / .txt / 伪装成 .jpg 的文本都行；常见的订阅格式都认）" style="flex:1">
    <button class="ghost" id="btnImport">导入并筛选</button>
  </div>
  <div id="importOut" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>主用源（primary）· 按顺序查，命中即用</h2>
  <div id="tblPrimary"></div>
  <div class="sep">兜底源仅在主用全部落空时才查，可以慢</div>
  <h2>兜底源（fallback）</h2>
  <div id="tblFallback"></div>
  <div class="row" style="margin-top:12px">
    <button class="ghost" id="btnAddP">+ 加主用源</button>
    <button class="ghost" id="btnAddF">+ 加兜底源</button>
  </div>
</div>

<div class="card">
  <h2>JS 源（drpy）· 插件自带引擎，直接跑站点规则</h2>
  <div class="sub" style="margin:-6px 0 10px;font-size:12.5px">
    就是 TVBox 配置里 <b>type: 3</b> 的那批站点。这里只填<b>规则文件地址</b>（配置里的
    <span class="mono">ext</span>）—— drpy2 运行时已内置，不用再填 <span class="mono">api</span>。
    支持 <span class="mono">http(s)</span> 网址、<span class="mono">**</span>base64、或直接粘贴规则文本。
  </div>
  <div id="tblJs"></div>
  <div class="row" style="margin-top:12px">
    <button class="ghost" id="btnAddJ">+ 加 JS 源</button>
    <span class="tag dim" id="jsEngine">引擎检测中…</span>
  </div>
</div>

<div class="card">
  <h2>查询方式与启用数量</h2>
  <div class="sub" style="margin:-6px 0 10px;font-size:12.5px">
    「主用优先」是旧行为：先查主用，主用有结果就不查兜底。
    「全部同时」会把主用 + 兜底一起查，所有启用的源都参与搜索和换源；
    源越多覆盖越广，但搜索也会更慢，按实际需要限制数量即可。
  </div>
  <div class="row">
    <span style="color:var(--dim)">查询方式</span>
    <select id="srcMode" style="width:250px">
      <option value="priority">主用优先（旧行为）</option>
      <option value="parallel">全部同时（推荐，所有启用的源一起查）</option>
    </select>
    <span style="color:var(--dim)">采集源最多启用</span>
    <input id="srcLimit" type="number" min="0" max="100" value="0" style="width:80px">
    <span style="color:var(--dim)">个（0=不限）</span>
    <span style="color:var(--dim)">JS 源最多</span>
    <input id="jsLimit" type="number" min="0" max="50" value="4" style="width:70px">
    <span style="color:var(--dim)">个（0=不限）</span>
  </div>
  <div id="srcPolicyMsg" class="sub" style="margin:8px 0 0;font-size:12px"></div>
</div>

<div class="card">
  <h2>在线源可见范围（按飞牛影视账号）</h2>
  <div class="sub" style="margin:-6px 0 10px;font-size:12.5px">
    只影响<b>在线源</b>：搜索结果、豆瓣伪库、在线影片的「继续播放」。
    本地媒体库、本地播放记录、本地继续播放<b>不受影响</b>。
    默认<b>仅管理员</b>可见；要分享给家人，改成「所有用户」或「指定用户」。
  </div>
  <div class="row">
    <span style="color:var(--dim)">谁可以看到在线源</span>
    <select id="visMode" style="width:220px">
      <option value="admin">仅管理员（默认）</option>
      <option value="all">所有用户</option>
      <option value="users">指定用户</option>
    </select>
    <span class="tag dim" id="visTag">读取中…</span>
  </div>
  <div id="userBox" style="display:none;margin-top:10px"></div>
  <div id="visMsg" class="sub" style="margin:8px 0 0;font-size:12px"></div>
</div>

<div class="card">
  <h2>测速与推荐</h2>
  <div class="row">
    <span style="color:var(--dim)">搜索关键词</span>
    <input id="kw" value="凡人" style="width:160px">
    <span style="color:var(--dim)">单个超时(秒)</span>
    <input id="to" value="25" style="width:70px">
    <button id="btnSave">保存并测速</button>
    <button class="ghost" id="btnTest">只测速（不保存）</button>
  </div>
  <div id="log" style="margin-top:12px"></div>
  <div id="testOut" style="margin-top:12px"></div>
</div>

<div class="card">
  <h2>写操作来源（决定你保存得下去没有）</h2>
  <div class="sub" style="margin:-6px 0 10px;font-size:12.5px">
    为避免把 5666 映射到公网后被人乱改源，<b>保存 / 导入</b>这类写操作默认只放行
    <b>内网</b>、<b>IPv6 内网段</b>、<b>叠加网</b>（Tailscale、ZeroTier 之类）和白名单地址。
    另外<b>本机第一次改配置会自动信任一次，并给这台浏览器发一张写凭证</b> ——
    之后你这台浏览器换 IP、换网络（宽带重播号、切 4G、走飞牛远程访问）都还能改，
    凭证存在 conf/config.json 的 <span class="mono">write_token</span> 里，别外传。</div>
  <div id="wgtInfo" style="margin-bottom:10px"></div>
  <div class="row">
    <button class="ghost" id="btnAllowIp">把当前来源 IP 加进白名单</button>
    <button class="ghost" id="btnAllowAny">不限制来源：开 / 关</button>
  </div>
  <div class="sub" style="margin:10px 0 0;font-size:12px">
    万一这页也被挡住（提示 403）——在 NAS 上执行下面这条即可解锁（走本机 socket，永远放行）：
  </div>
  <pre id="wcl" style="margin:6px 0 0"></pre>
</div>

<div class="card">
  <h2>命令行等价操作</h2>
  <pre id="cli"></pre>
</div>

<script>
var S = {primary:[], fallback:[], js:[], VIS:"admin", ALLOW:[], USERS:[],
         MODE:"priority", LIMIT:0, JLIMIT:4};
var LAST = null;
var UNLOCK_CMD = "curl -s --unix-socket /var/run/trim_media.sock -X POST " +
  "-H 'Content-Type: application/json' " +
  "-d '{\"write_allow_any\":true}' http://x/v/__mitm__/api/config";

function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}

function linesTag(v, r){
  var t = (v==="single") ? "单线路" : (v==="multi" ? "多线路" : "自动");
  var cls = (v==="single") ? "dim" : (v==="multi" ? "ok" : "warn");
  var extra = "";
  if (r && r.routes){ extra = " · 实测 " + r.routes + " 线"; }
  return '<span class="tag '+cls+'">'+t+extra+'</span>';
}

function renderTable(level, mount){
  var rows = S[level] || [];
  var h = '<div class="grid head"><div>名称</div><div>采集接口</div>' +
          '<div>Referer（留空自动）</div><div>线路</div><div>启用</div><div></div></div>';
  if (!rows.length){
    h += '<div style="color:var(--dim);padding:10px 0">（还没有）</div>';
  }
  rows.forEach(function(s, i){
    var rr = (LAST||[]).filter(function(x){return x.api===s.api;})[0];
    h += '<div class="grid" style="margin-top:6px">' +
      '<input value="'+esc(s.name)+'" oninput="S.'+level+'['+i+'].name=this.value">' +
      '<input value="'+esc(s.api)+'" oninput="S.'+level+'['+i+'].api=this.value">' +
      '<input value="'+esc(s.referer||"")+'" oninput="S.'+level+'['+i+'].referer=this.value">' +
      '<select onchange="S.'+level+'['+i+'].lines=this.value">' +
        ["auto","single","multi"].map(function(v){
          return '<option value="'+v+'"'+(s.lines===v?" selected":"")+'>' +
                 (v==="auto"?"自动":(v==="single"?"单线路":"多线路"))+'</option>';
        }).join("") + '</select>' +
      '<div>' + linesTag(s.lines, rr) + '</div>' +
      '<button class="ghost" onclick="del(\''+level+'\','+i+')">删</button>' +
      '</div>';
  });
  document.getElementById(mount).innerHTML = h;
}

function del(level, i){ S[level].splice(i,1); draw(); }

// 从导入结果里单个加入（quiet=true 时不刷日志，给「全部加进去」用）
function addCms(s, quiet){
  if ((S.primary||[]).some(function(x){ return x.api === s.api; })){
    if (!quiet) log("已经在采集源里了：" + s.name); return false;
  }
  S.primary.push({name:s.name, api:s.api, referer:s.referer||"", lines:"auto"});
  if (!quiet){ draw(); log("已加入采集源：" + s.name + " —— 点「保存并测速」生效。"); }
  return true;
}
function addJs(s, quiet){
  if ((S.js||[]).some(function(x){ return x.js === s.js; })){
    if (!quiet) log("已经在 JS 源里了：" + s.name); return false;
  }
  S.js.push({name:s.name, js:s.js, referer:s.referer||"", lines:"auto"});
  if (!quiet){ draw(); log("已加入 JS 源：" + s.name + " —— 点「保存并测速」生效。"); }
  return true;
}
// 「仍要加入」：被判定跳过的站点也照加（网盘类、拿不准的都行），
// 加完让「保存并测速」去证伪 —— 判定错了就留用，判对了它会显示 ❌。
function addSkip(s){
  var ok;
  if (s.js){ ok = addJs({name:s.name, js:s.js, referer:s.referer||"", lines:"auto"}); }
  else if (s.api){ ok = addCms({name:s.name, api:s.api, referer:s.referer||"", lines:"auto"}); }
  else { log("这条既没有接口也没有规则，加不进去。"); return false; }
  if (ok){
    log("已照样加入：" + (s.name||"?") + "（原判定：" + (s.why||"") + "）" +
        " —— 点「保存并测速」看它到底能不能用。");
  }
  return ok;
}

function renderJsTable(){
  var rows = S.js || [];
  var h = '<div class="grid js head"><div>名称</div><div>规则地址（ext）</div>' +
          '<div>Referer（可空）</div><div>线路</div><div>状态</div><div></div></div>';
  if (!rows.length){
    h += '<div style="color:var(--dim);padding:10px 0">（还没有 JS 源）</div>';
  }
  rows.forEach(function(s, i){
    var rr = (LAST||[]).filter(function(x){return x.js===s.js;})[0];
    var st = rr ? linesTag(s.lines, rr) : '<span class="tag dim">未测</span>';
    h += '<div class="grid js" style="margin-top:6px">' +
      '<input value="'+esc(s.name)+'" oninput="S.js['+i+'].name=this.value">' +
      '<input value="'+esc(s.js)+'" oninput="S.js['+i+'].js=this.value" ' +
        'placeholder="https://…/站点.js">' +
      '<input value="'+esc(s.referer||"")+'" oninput="S.js['+i+'].referer=this.value">' +
      '<select onchange="S.js['+i+'].lines=this.value">' +
        ["auto","single","multi"].map(function(v){
          return '<option value="'+v+'"'+(s.lines===v?" selected":"")+'>' +
                 (v==="auto"?"自动":(v==="single"?"单线路":"多线路"))+'</option>';
        }).join("") + '</select>' +
      '<div>' + st + '</div>' +
      '<button class="ghost" onclick="del(\'js\','+i+')">删</button>' +
      '</div>';
  });
  document.getElementById("tblJs").innerHTML = h;
}

function draw(){
  renderTable("primary", "tblPrimary");
  renderTable("fallback", "tblFallback");
  renderJsTable();
  renderSourcePolicy();
  document.getElementById("cli").textContent =
    "# 看当前生效的源\n" +
    "curl -s --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/sources\n\n" +
    "# 热加载（配置页保存时已自动做）\n" +
    "curl -s --unix-socket /var/run/trim_media.sock " +
    "'http://x/v/__mitm__/sources?reload=1'\n\n" +
    "# 命令行测速\n" +
    "python3 /usr/local/trim_media_mitm/source_check.py --keyword 凡人\n\n" +
    "# 配置页写不进去时（403）解锁：\n" +
    UNLOCK_CMD + "\n\n" +
    "# 看/改写操作来源策略（write_token 就是你这台浏览器的写凭证）\n" +
    "curl -s --unix-socket /var/run/trim_media.sock http://x/v/__mitm__/api/config\n" +
    "cat /usr/local/trim_media_mitm/conf/config.json";
  renderVis();
}

function renderSourcePolicy(){
  var m = document.getElementById("srcMode");
  if (!m) return;
  m.value = S.MODE || "priority";
  var sl = document.getElementById("srcLimit");
  var jl = document.getElementById("jsLimit");
  if (sl) sl.value = (S.LIMIT === 0 ? 0 : (S.LIMIT || 0));
  if (jl) jl.value = (S.JLIMIT === 0 ? 0 : (S.JLIMIT || 0));
  var msg = document.getElementById("srcPolicyMsg");
  if (msg){
    if ((S.MODE || "priority") === "parallel"){
      msg.textContent = "主用和兜底会一起查询；采集源上限 " +
        (S.LIMIT > 0 ? (S.LIMIT + " 个") : "不限") +
        "，JS 源上限 " + (S.JLIMIT > 0 ? (S.JLIMIT + " 个") : "不限") + "。";
    } else {
      msg.textContent = "先查主用，主用有结果就停；主用全部落空才查兜底。" +
        "采集源上限 " + (S.LIMIT > 0 ? (S.LIMIT + " 个") : "不限") +
        "，JS 源上限 " + (S.JLIMIT > 0 ? (S.JLIMIT + " 个") : "不限") + "。";
    }
  }
}

function renderVis(){
  var sel = document.getElementById("visMode");
  if (!sel) return;
  sel.value = S.VIS || "admin";
  var box = document.getElementById("userBox");
  var tag = document.getElementById("visTag");
  var msg = document.getElementById("visMsg");
  var names = (S.ALLOW || []).map(function(x){return String(x).toLowerCase();});
  var n = (S.USERS || []).filter(function(u){
    return u.is_admin || names.indexOf(String(u.username).toLowerCase()) >= 0 ||
           names.indexOf(String(u.guid).toLowerCase()) >= 0;
  }).length;
  if (tag){
    tag.className = "tag " + (S.VIS === "all" ? "warn" : (S.VIS === "users" ? "ok" : "dim"));
    tag.textContent = S.VIS === "all" ? "所有账号可见"
                    : (S.VIS === "users" ? ("已选 " + n + " 个账号") : "仅管理员");
  }
  if (box) box.style.display = (S.VIS === "users") ? "" : "none";
  if (msg){
    if (S.VIS === "all"){
      msg.textContent = "注意：所有飞牛影视账号（包括访客/儿童账号）都会看到在线搜索和在线继续播放。";
    } else if (S.VIS === "users"){
      msg.textContent = "管理员始终可见；下面勾选的账号也会看到。未勾选的账号只看到本地媒体库。";
    } else {
      msg.textContent = "只有管理员账号能看到在线源；其他账号只看到本地媒体库和本地播放记录。";
    }
  }
  if (S.VIS === "users") renderUsers();
}

function renderUsers(){
  var box = document.getElementById("userBox");
  if (!box) return;
  var users = S.USERS || [];
  if (!users.length){
    box.innerHTML = '<div style="color:var(--warn);font-size:12.5px">' +
      '没读到飞牛影视用户库（可能官方影视还没启动过，或数据库路径不同）。' +
      '可以先选「仅管理员」，或在 config.json 里手填 online_users。</div>';
    return;
  }
  var names = (S.ALLOW || []).map(function(x){return String(x).toLowerCase();});
  var h = '<div class="row" style="gap:10px;flex-wrap:wrap">';
  users.forEach(function(u, i){
    var checked = !!u.is_admin ||
      names.indexOf(String(u.username).toLowerCase()) >= 0 ||
      names.indexOf(String(u.guid).toLowerCase()) >= 0;
    h += '<label style="display:flex;align-items:center;gap:6px;color:var(--fg)">' +
      '<input type="checkbox" ' + (checked ? "checked" : "") +
      (u.is_admin ? " disabled" : "") +
      ' onchange="toggleUser(' + i + ')">' +
      esc(u.username) + (u.is_admin ? ' <span class="tag dim">管理员</span>' : '') +
      '</label>';
  });
  h += '</div>';
  box.innerHTML = h;
}

function toggleUser(i){
  var u = (S.USERS || [])[i];
  if (!u || u.is_admin) return;
  var name = String(u.username || "");
  var key = name.toLowerCase();
  var guidKey = String(u.guid || "").toLowerCase();
  var before = (S.ALLOW || []).length;
  S.ALLOW = (S.ALLOW || []).filter(function(x){
    var k = String(x).toLowerCase();
    return k !== key && k !== guidKey;
  });
  if (S.ALLOW.length === before && name) S.ALLOW.push(name);
  renderVis();
}

function log(s){ document.getElementById("log").textContent = s; }

function api(path, body){
  return fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type":"application/json"} : {},
    body: body ? JSON.stringify(body) : null
  }).then(function(r){
    return r.json().catch(function(){ return {}; }).then(function(d){
      if (!r.ok) throw new Error(d.msg || ("HTTP " + r.status));
      return d;
    });
  });
}

function drawWrite(w){
  S.WRITE = w || {};
  var ip = w.ip || "未知", why = w.why || "";
  var ok = !!w.allowed;
  var box = document.getElementById("wgtInfo");
  box.innerHTML =
    '<div class="row" style="gap:8px">' +
      '<span class="tag ' + (ok ? "ok" : "bad") + '">' +
        (ok ? "写操作已放行" : "写操作被拦截") + '</span>' +
      '<span style="color:var(--dim)">你的来源 IP：<b class="mono">' +
        esc(ip) + '</b>（' + esc(why) + '）</span>' +
      '<span class="tag ' + (w.allow_any ? "bad" : "dim") + '">' +
        (w.allow_any ? "已设为不限制来源" : "默认策略") + '</span>' +
      (w.browser_bound ? '<span class="tag ok">这台浏览器带写凭证</span>' : '') +
    '</div>';
  var warn = document.getElementById("warn");
  if (ok){
    warn.style.display = "none";
  } else if (w.auto_trust_first && !w.token_set){
    // 本机还没发过写凭证：第一次写操作会自动记住你
    warn.style.display = "";
    warn.className = "note";
    warn.innerHTML = "<b>你当前的来源 IP（<span class=\"mono\">" + esc(ip) + "</span>）不在内网，"
      + "但这是本机第一次改配置 —— 直接点「保存并测速」就会自动记住你（只发生一次）。</b><br>"
      + "想更省心：点下面「不限制来源：开 / 关」，以后换 IP、换网络都不用再管。";
  } else {
    warn.style.display = "";
    warn.className = "note";
    warn.innerHTML = "<b>⚠️ 你现在的来源 IP 没被放行，保存 / 导入会被拒。</b><br>" +
      "检测到来源 IP：<b class=\"mono\">" + esc(ip) + "</b>（" + esc(why) + "）。<br>" +
      "<b>三条路任选一条：</b><br>" +
      "① <b>用之前已经改过配置的那台浏览器</b>打开本页 —— 它带着写凭证，跟 IP 无关，" +
      "在那上面点下面「把当前来源 IP 加进白名单」就行（换网络、宽带重播号也不用再管）。<br>" +
      "② 在 NAS 上执行（走本机 unix socket，永远放行）：<br>" +
      "<span class=\"mono\">" + esc(UNLOCK_CMD) + "</span><br>" +
      "③ 编辑 <span class=\"mono\">" + esc(w.config_file || "") + "</span>，" +
      "把 <span class=\"mono\">" + esc(ip) + "</span> 加进 write_allow_ip。<br>" +
      "解锁后刷新本页，再回来点「保存并测速」就行。";
  }
  document.getElementById("wcl").textContent = UNLOCK_CMD;
}

function load(){
  api("/v/__mitm__/api/sources").then(function(d){
    S.primary = (d.primary||[]).slice();
    S.fallback = (d.fallback||[]).slice();
    S.js = (d.js||[]).slice();
    S.VIS = d.online_visibility || "admin";
    S.ALLOW = (d.online_users||[]).slice();
    S.USERS = (d.users||[]).slice();
    S.MODE = d.source_mode || "priority";
    S.LIMIT = (d.source_limit === undefined ? 0 : d.source_limit);
    S.JLIMIT = (d.js_source_limit === undefined ? 4 : d.js_source_limit);
    if (d.file) document.getElementById("file").textContent = d.file;
    if (d.write) drawWrite(d.write);
    if (d.msg) log(d.msg);
    var eg = document.getElementById("jsEngine");
    if (eg){
      var e = d.engine || {};
      eg.className = "tag " + (e.available ? "ok" : "bad");
      eg.textContent = e.available ? ("引擎就绪 · " + (e.engine||"drpy2")) 
                                  : ("引擎不可用：" + (e.why||"未知"));
    }
    draw();
  }).catch(function(e){ log("读取配置失败：" + e.message); });
}

function blank(level){ return {name:"", api:"", referer:"", lines:"auto"}; }
function blankJs(){ return {name:"", js:"", referer:"", lines:"auto"}; }

document.getElementById("visMode").onchange = function(){
  S.VIS = this.value;
  renderVis();
};

document.getElementById("srcMode").onchange = function(){
  S.MODE = this.value;
  renderSourcePolicy();
};
document.getElementById("srcLimit").oninput = function(){
  S.LIMIT = Math.max(0, Math.min(100, parseInt(this.value, 10) || 0));
};
document.getElementById("jsLimit").oninput = function(){
  S.JLIMIT = Math.max(0, Math.min(50, parseInt(this.value, 10) || 0));
};

document.getElementById("btnAddP").onclick = function(){ S.primary.push(blank()); draw(); };
document.getElementById("btnAddF").onclick = function(){ S.fallback.push(blank()); draw(); };
document.getElementById("btnAddJ").onclick = function(){ S.js.push(blankJs()); draw(); };

document.getElementById("btnAllowIp").onclick = function(){
  var btn = this; btn.disabled = true;
  api("/v/__mitm__/api/config", {add_ip:true}).then(function(d){
    drawWrite(d);
    log("已把当前来源 IP 加进白名单。");
  }).catch(function(e){ log("加白名单失败：" + e.message); })
    .then(function(){ btn.disabled = false; });
};

document.getElementById("btnAllowAny").onclick = function(){
  var btn = this; btn.disabled = true;
  var want = !((S.WRITE||{}).allow_any);
  api("/v/__mitm__/api/config", {write_allow_any:want}).then(function(d){
    drawWrite(d);
    log(want ? "已设为不限制来源（任何地址都能改配置）。"
             : "已恢复默认策略（只放行内网 / 叠加网 / 白名单）。");
  }).catch(function(e){ log("切换失败：" + e.message); })
    .then(function(){ btn.disabled = false; });
};

document.getElementById("btnSave").onclick = function(){
  var btn = this; btn.disabled = true; log("保存中…");
  api("/v/__mitm__/api/sources", {primary:S.primary, fallback:S.fallback, js:S.js,
                                  online_visibility:S.VIS, online_users:S.ALLOW,
                                  source_mode:S.MODE, source_limit:S.LIMIT,
                                  js_source_limit:S.JLIMIT})
    .then(function(d){
      if (d.online_visibility) S.VIS = d.online_visibility;
      if (d.online_users) S.ALLOW = d.online_users.slice();
      if (d.users) S.USERS = d.users.slice();
      if (d.source_mode) S.MODE = d.source_mode;
      if (d.source_limit !== undefined) S.LIMIT = d.source_limit;
      if (d.js_source_limit !== undefined) S.JLIMIT = d.js_source_limit;
      draw();
      log("保存成功：" + (d.msg||""));
      return runTest();
    })
    .catch(function(e){ log("保存失败：" + e.message); })
    .then(function(){ btn.disabled = false; });
};

document.getElementById("btnTest").onclick = function(){ runTest(); };

function runTest(){
  var sites = [];
  ["primary","fallback"].forEach(function(lv){
    (S[lv]||[]).forEach(function(s){
      if (s.api && s.api.indexOf("http")===0){
        sites.push({name:s.name, api:s.api, referer:s.referer, lines:s.lines, level:lv});
      }
    });
  });
  (S.js||[]).forEach(function(s){
    if (s.js){
      sites.push({name:s.name||s.js, js:s.js, referer:s.referer,
                  lines:s.lines, level:"js", kind:"js"});
    }
  });
  if (!sites.length){ log("还没有可测的源。"); return Promise.resolve(); }
  var kw = document.getElementById("kw").value || "凡人";
  var to = parseFloat(document.getElementById("to").value) || 25;
  log("正在测速（" + sites.length + " 个源，关键词「" + kw + "」）…" +
      "  JS 源首次要下载规则并启动引擎，会慢一些。");
  return api("/v/__mitm__/api/test", {sites:sites, keyword:kw, timeout:to,
                                       source_mode:S.MODE, source_limit:S.LIMIT,
                                       js_source_limit:S.JLIMIT})
    .then(function(d){
      LAST = d.results || [];
      renderTest(d);
      draw();
      log("测速完成。");
    })
    .catch(function(e){ log("测速失败：" + e.message); });
}

function renderTest(d){
  var h = '<table><tr><th>源</th><th>线路</th><th>响应</th><th>命中</th>' +
          '<th>可放线路</th><th>结论 / 建议</th></tr>';
  var suggested = {};
  (d.recommend && d.recommend.primary || []).forEach(function(n){ suggested[n]="primary"; });
  (d.recommend && d.recommend.fallback || []).forEach(function(n){ suggested[n]="fallback"; });
  (d.results||[]).forEach(function(r){
    var ms = (r.ms==null) ? "-" : Math.round(r.ms)+"ms";
    var hit = (r.hits==null) ? "-" : r.hits;
    var pl = (r.playable==null) ? "-" : r.playable;
    if (r.kind === "js" || r.js){
      var vj, cj;
      if (r.playable){
        cj = "ok";
        vj = "✅ 规则可执行（分类 " + (r.cate||0) + " · 搜索 " + (r.search||0) + "）";
        if (!r.search) vj += " · 搜索没命中，可能只是关键词不对";
        if (suggested[r.name]==="primary") vj += " · 建议主用";
        if (suggested[r.name]==="fallback") vj += " · 建议兜底";
      } else {
        cj = "bad";
        vj = "❌ 跑不起来：" + esc(r.err || "无响应");
      }
      h += '<tr><td>'+esc(r.name)+'<br><span class="tag dim">JS</span></td>' +
           '<td>'+linesTag(r.lines, r)+'</td><td>'+ms+'</td><td>'+hit+'</td>' +
           '<td>'+pl+'</td><td><span class="tag '+cj+'">'+vj+'</span></td></tr>';
      return;
    }
    var verdict, cls;
    if (r.hits==null){ verdict = "❌ 不可用（" + esc(r.err||"无响应") + "）"; cls="bad"; }
    else if (r.hits===0){ verdict = "⚠️ 站点活着，该关键词没结果"; cls="warn"; }
    else if (!r.playable){ verdict = "❌ 搜到《" + esc((r.title||"").slice(0,12)) + "》但无可放线路"; cls="bad"; }
    else {
      cls = "ok";
      verdict = "✅ 可出片源";
      if (suggested[r.name]==="primary") verdict += " · 建议主用";
      if (suggested[r.name]==="fallback") verdict += " · 建议兜底";
    }
    h += '<tr><td>'+esc(r.name)+((r.fmt==="xml")?' <span class="tag dim">XML 接口</span>':'')+
         '</td><td>'+linesTag(r.lines, r)+'</td><td>'+ms+
         '</td><td>'+hit+'</td><td>'+pl+'</td>' +
         '<td><span class="tag '+cls+'">'+verdict+'</span></td></tr>';
  });
  h += '</table>';
  if (d.recommend && d.recommend.text){
    h += '<div class="sep">推荐方案</div><pre>' + esc(d.recommend.text) + '</pre>' +
         '<div class="row" style="margin-top:10px">' +
         '<button id="btnApply">按推荐应用（并保存）</button></div>';
  }
  document.getElementById("testOut").innerHTML = h;
  var b = document.getElementById("btnApply");
  if (b) b.onclick = applyRec;
}

function applyRec(){
  if (!LAST) return;
  var p = [], f = [], j = [];
  LAST.forEach(function(r){
    var lines = (r.lines==="single"||r.lines==="multi") ? r.lines
              : ((r.routes||0)>1 ? "multi" : "single");
    if (r.kind === "js" || r.js){
      if (r.suggest === "js" && (r.playable||0) > 0){
        j.push({name:r.name, js:r.js, referer:r.referer||"", lines:lines});
      }
      return;
    }
    if ((r.playable||0) > 0 &&
        (r.suggest === "primary" || r.suggest === "fallback")){
      var s = {name:r.name, api:r.api, referer:r.referer||"", lines:lines};
      (r.suggest === "primary" ? p : f).push(s);
    }
  });
  S.primary = p; S.fallback = f; S.js = j;
  draw();
  document.getElementById("btnSave").click();
}

document.getElementById("btnImport").onclick = function(){
  var url = document.getElementById("cfgurl").value.trim();
  if (!url){ log("先填一个配置地址。"); return; }
  var btn=this; btn.disabled=true;
  document.getElementById("importOut").innerHTML = '<span style="color:var(--dim)">抓取并筛选中…</span>';
  api("/v/__mitm__/api/import", {url:url}).then(function(d){
    btn.disabled=false;
    if (!d.ok){
      document.getElementById("importOut").innerHTML =
        '<span class="tag bad">导入失败</span> ' + esc(d.msg||"");
      return;
    }
    var h = '<div class="row"><span class="tag ok">采集接口 '+d.meta.kept+' 个</span>' +
            '<span class="tag ok">JS 源 '+(d.meta.js||0)+' 个</span>' +
            '<span class="tag dim">跳过 '+d.meta.skipped+' 个（下面有明细，可「仍要加入」）</span>' +
            '<button class="ghost" id="btnAddAll">把可用的全部加进去</button></div>';
    window.IMP = d;
    if (d.note){
      h += '<div style="color:var(--dim);font-size:12.5px;margin-top:6px">'+esc(d.note)+'</div>';
    }
    if (d.kept.length){
      h += '<table style="margin-top:8px"><tr><th>名称</th><th>接口</th><th></th></tr>';
      d.kept.forEach(function(s, k){
        h += '<tr><td>'+esc(s.name)+'</td><td class="mono">'+esc(s.api)+'</td>' +
             '<td><button class="ghost" onclick="addCms(IMP.kept['+k+'])">加入</button></td></tr>';
      });
      h += '</table>';
    }
    if (d.js && d.js.length){
      h += '<div class="sep">JS 源（TVBox type:3 · 由插件自带的 drpy2 引擎直接执行）</div>' +
           '<table><tr><th>名称</th><th>规则地址（ext）</th><th></th></tr>';
      d.js.forEach(function(s, k){
        h += '<tr><td>'+esc(s.name)+'</td><td class="mono">'+esc(s.js)+'</td>' +
             '<td><button class="ghost" onclick="addJs(IMP.js['+k+'])">加入</button></td></tr>'; });
      h += '</table>';
    }
    if (!d.kept.length && !(d.js||[]).length){
      h += '<div style="color:var(--warn);margin-top:8px">这份配置里没筛出可用源' +
           '——加密 JAR 需要 Java 运行时（本插件不带，也跑不了），网盘类需要账号。' +
           '（下面是每个被跳过的站点、接口和判定理由，不认同就点「仍要加入」去实测）</div>';
    }
    if (d.skipped && d.skipped.length){
      var byWhy = {}, nd = 0, ndJar = 0;
      d.skipped.forEach(function(s){
        byWhy[s.why] = (byWhy[s.why]||0)+1;
        if (s.cat === "netdisk"){
          nd++;
          if (/JAR/.test(s.why||"")) ndJar++;      // 同时也是加密 JAR 的
        }
      });
      h += '<div class="sep">被跳过的原因</div><table><tr><th>原因</th><th>数量</th></tr>';
      Object.keys(byWhy).sort().forEach(function(k){
        h += '<tr><td>'+esc(k)+'</td><td>'+byWhy[k]+'</td></tr>'; });
      h += '</table>';

      h += '<div class="sep">被跳过的站点明细（判得对不对你自己核）';
      if (nd){
        h += '<button class="ghost" id="btnAddNd" style="margin-left:8px">' +
             '网盘类的也全部加入（'+nd+' 个）</button>';
      }
      h += '</div>' +
        '<div style="color:var(--dim);font-size:12.5px;margin-bottom:6px">' +
        '点「仍要加入」＝ 不管这个判定，直接塞进列表；然后点「保存并测速」，' +
        '它到底能不能用当场就有答案（不能用的会显示 ❌）。' +
        (ndJar ? '网盘类里有 '+ndJar+' 个同时是「加密 JAR 蜘蛛」，' +
                 '加进去也还是 ❌ —— 那种要 Java 运行时，本插件不带。' : '') +
        '</div>' +
        '<table><tr><th>名称</th><th>接口 / 规则</th><th>为什么跳过</th><th></th></tr>';
      d.skipped.forEach(function(s, k){
        h += '<tr><td>'+esc(s.name||s.label||"-")+'</td>' +
             '<td class="mono" style="word-break:break-all">'+esc(s.js||s.api||"—")+'</td>' +
             '<td>'+esc(s.why||"")+'</td>' +
             '<td><button class="ghost" onclick="addSkip(IMP.skipped['+k+'])">' +
             '仍要加入</button></td></tr>';
      });
      h += '</table>';

      if (d.skipped.some(function(s){ return /JAR/.test(s.why||""); })){
        h += '<div class="sub" style="font-size:12.5px;margin-top:6px">' +
          '<b>「加密 JAR 蜘蛛」是什么，为什么跑不了：</b>这类站点在 TVBox 里长这样 —— ' +
          '<span class="mono">type:3</span>、接口名像 <span class="mono">csp_xxxGuard</span>、' +
          '配置顶上的 <span class="mono">spider</span> 指向一个 <span class="mono">.jar</span>。' +
          '它们的取流逻辑全在厂家加密过的 Java 包里（还带 ARM 的原生库），' +
          '本插件用的是 JS 引擎（drpy2 + QuickJS），跑不了它不是因为你地址写错了 —— ' +
          '这类配置就得用带 Java 运行时的原版客户端；或者换一份' +
          '「明文采集接口 / JS 规则」为主的配置。</div>';
      }
    }
    document.getElementById("importOut").innerHTML = h;
    var b2 = document.getElementById("btnAddAll");
    if (b2) b2.onclick = function(){
      var a = 0, b = 0;
      d.kept.forEach(function(s){ if (addCms(s, true)) a++; });
      (d.js||[]).forEach(function(s){ if (addJs(s, true)) b++; });
      draw();
      log("已加入 " + a + " 个采集源 + " + b + " 个 JS 源，点「保存并测速」生效。");
      return;
    };
    var b3 = document.getElementById("btnAddNd");
    if (b3) b3.onclick = function(){
      var n = 0, jar = 0;
      d.skipped.forEach(function(s){
        if (s.cat !== "netdisk") return;
        if (/JAR/.test(s.why||"")) jar++;
        if (s.js){ if (addJs(s, true)) n++; }
        else if (s.api){ if (addCms(s, true)) n++; }
      });
      draw();
      log("已把 " + n + " 个带「网盘」标记的源照样加进列表" +
          (jar ? "（其中 " + jar + " 个是加密 JAR，预计 ❌）" : "") +
          " —— 点「保存并测速」，是真网盘还是判错了立刻见分晓。");
      return;
    };
  }).catch(function(e){
    btn.disabled=false;
    document.getElementById("importOut").innerHTML =
      '<span class="tag bad">导入失败</span> ' + esc(e.message);
  });
};

load();
</script>
</div></body></html>
"""
