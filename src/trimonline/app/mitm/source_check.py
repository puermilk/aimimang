#!/usr/bin/env python3
"""采集站体检：按 mitm.py 的**真实调用**探测「可用性 / 速度 / 能不能出片源」。

用法：
  python3 source_check.py                          # 测 conf/sources.json 里配置的全部源
  python3 source_check.py --keyword 庆余年          # 换搜索关键词（默认「凡人」）
  python3 source_check.py https://x/api.php/provide/vod/ ...   # 测临时指定的接口
  python3 source_check.py --file /path/sources.json

探测内容（与 mitm.py 完全一致）：
  1) <api>?ac=videolist&wd=关键词     —— 搜索，看命中数与耗时
  2) <api>?ac=detail&ids=<vod_id>     —— 搜索没带播放地址时补拉详情
  3) vod_play_url 里有没有「直接可放」的线路（.m3u8 / 直链，
     不是 /share/ 这类 HTML 分享页）—— 这一项决定能不能真的点开就播

判读：
  ✅ 可出片源    ⚠️ 站点活着但该关键词没搜到（换个词再试）
  ❌ 不可用      ❌ 搜到条目但无可放线路（只有分享页/无地址，等于用不了）

只依赖标准库，不需要 PYTHONPATH。
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_FILE = "/usr/local/trim_media_mitm/conf/sources.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 采集站多为自签/域名杂乱的 HTTPS，体检时不做证书校验（只读探测，无凭据）
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _looks_m3u8(url):
    """与 mitm.py 的同名函数保持一致：判是否为「可直接放」的地址。"""
    if not url:
        return False
    u = url.lower().split("?")[0]
    if u.endswith((".m3u8", ".m3u")):
        return True
    if re.search(r"/share/|/detail|/vod|/index\.html|/\w+\.html?$", u):
        return False
    return True


def playable_lines(play_url):
    """vod_play_url 里有多少条线路是直接可放的。
    格式："集名$地址#集名$地址"，多线路以 $$$ 分隔。"""
    n = 0
    for line in (play_url or "").split("$$$"):
        urls = []
        for part in line.split("#"):
            if "$" in part:
                u = part.split("$", 1)[1].strip()
                if u.startswith("http"):
                    urls.append(u)
        if urls and all(_looks_m3u8(u) for u in urls[:3]):
            n += 1
    return n


def idna_url(url):
    """把 URL 里非 ASCII 的**域名**转成 punycode。

    背景：urllib / http.client 只会用 latin-1 编码 Host 头，遇到中文域名直接抛
        UnicodeEncodeError('latin-1', 'www.某订阅站.cc', 4, 7, ...)
    （aiohttp 走 yarl，自带 IDNA，所以只有 urllib 这条路要补。）

    只动域名，路径 / 查询串原样保留 —— 中文路径由各个调用方自己决定怎么编码。
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
        return url                      # 本来就是纯 ASCII，一个字不改
    except UnicodeEncodeError:
        pass
    host = s.hostname or ""
    if not host:
        return url
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except Exception:
        return url                      # 转不了就原样返回，让上层去报错
    if ":" in host_ascii:               # IPv6 字面量要带回方括号
        host_ascii = "[" + host_ascii + "]"
    userinfo = ""
    if s.username:
        userinfo = s.username + ((":" + s.password) if s.password else "") + "@"
    try:
        port = ":%d" % s.port if s.port else ""
    except ValueError:
        port = ""
    return urllib.parse.urlunsplit(
        (s.scheme, userinfo + host_ascii + port, s.path, s.query, s.fragment))


def fetch(url, referer, timeout, tries=2):
    """带一次重试：采集站常有个别请求慢到超时，重试一次再判失败更准。"""
    url = idna_url(url)                 # 中文域名 → punycode（否则 latin-1 报错）
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or url,
                "Accept": "application/json,text/plain,*/*",
            })
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                raw = r.read()
            return raw, time.time() - t0
        except Exception as e:
            last = e
            if i + 1 < tries:
                time.sleep(1.0)
    raise last


# ------------------------------------------------- 返回体解析（JSON / XML）---
# 苹果CMS 的采集接口有两种输出，站点靠地址区分：
#   .../provide/vod/          或  .../provide/vod/at/json/  → JSON
#   .../provide/vod/at/xml/                                → XML
# XML 长这样：
#   <?xml version="1.0" encoding="utf-8"?><rss version="5.1"><list ...>
#     <video><id>4538</id><name><![CDATA[凡人修仙传]]></name>…
#       <dl><dd flag="iqym3u8"><![CDATA[第01集$http://…m3u8#第02集$http://…]]></dd></dl>
#     </video>…
#   </list></rss>
# 只认 JSON 的话，XML 站点会直接报
#   JSONDecodeError: Expecting value: line 1 column 1 (char 0)
# 这里把两种都归一化成**同一个结构**，上层（搜索 / 详情 / 体检）一行都不用改：
#   {"list": [{"vod_id": …, "vod_name": …, "vod_play_from": …, "vod_play_url": …}]}

# XML 标签 → JSON 字段名（与苹果CMS JSON 接口的命名对齐）
_XML_FIELDS = (
    ("id", "vod_id"), ("tid", "type_id"), ("name", "vod_name"),
    ("type", "vod_class"), ("pic", "vod_pic"), ("lang", "vod_lang"),
    ("area", "vod_area"), ("year", "vod_year"), ("state", "vod_state"),
    ("note", "vod_remarks"), ("actor", "vod_actor"),
    ("director", "vod_director"), ("writer", "vod_writer"),
    ("last", "vod_time"), ("letter", "vod_letter"), ("serial", "vod_serial"),
    ("total", "vod_total"), ("version", "vod_version"),
)


def _decode(raw):
    """采集站里 GBK 编码并不少见，UTF-8 解不动就退到 gb18030。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _from_json(raw):
    obj = json.loads(_decode(raw))
    if isinstance(obj, list):           # 少数站点顶层直接给数组
        return {"list": obj}
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层既不是对象也不是数组")
    return obj


def _from_xml(raw):
    """XML → 与 JSON 同构的 dict。

    安全：带 DOCTYPE / ENTITY 的 XML 一律不解析（没必要为第三方返回体
    承担实体展开的风险）。
    """
    head = raw[:8192]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ValueError("XML 带 DOCTYPE/ENTITY，为安全起见不解析")
    root = ET.fromstring(raw)
    items = []
    for v in root.iter("video"):
        it = {}
        for tag, key in _XML_FIELDS:
            el = v.find(tag)
            if el is not None and (el.text or "").strip():
                it[key] = el.text.strip()
        if "vod_content" not in it:      # 正文标签各家写法不一：des / desc
            for tag in ("des", "desc", "content"):
                el = v.find(tag)
                if el is not None and (el.text or "").strip():
                    it["vod_content"] = el.text.strip()
                    break
        dls = []
        dl = v.find("dl")
        if dl is not None:
            for dd in dl.findall("dd"):
                dls.append(((dd.get("flag") or "").strip(), (dd.text or "").strip()))
        if dls:                          # 每条 <dd> = 一条线路，等价于 JSON 的 $$$ 分组
            it["vod_play_from"] = "$$$".join(f for f, _ in dls)
            it["vod_play_url"] = "$$$".join(u for _, u in dls)
        if it:
            items.append(it)
    return {"list": items, "xml": True}


def payload_fmt(raw):
    """返回体是 JSON 还是 XML（给体检结果里显示用）。"""
    head = raw.lstrip()[:200].lower()
    if head[:5] == b"<?xml" or head[:4] == b"<rss" or head[:4] == b"<xml":
        return "xml"
    if b"<list" in raw[:4096] or b"<video" in raw[:4096]:
        return "xml"
    return "json"


def load_payload(raw):
    """采集站返回体 → 统一结构。JSON / XML 都吃。

    失败时给出人话，而不是 `Expecting value: line 1 column 1` 这种。
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if not raw.strip():
        raise ValueError("返回体是空的（站点没给内容）")
    fmt = payload_fmt(raw)
    if fmt == "xml":
        try:
            return _from_xml(raw)
        except ET.ParseError as e:
            raise ValueError("看着像 XML 但解析失败：%s" % str(e)[:60])
    try:
        return _from_json(raw)
    except Exception as je:
        if b"<list" in raw[:8192]:
            return _from_xml(raw)        # 没声明头的 XML
        head = raw.lstrip()[:120].lower()
        if head.startswith((b"<!doctype html", b"<html")):
            raise ValueError("返回的是网页（HTML）而不是采集接口 —— "
                             "地址可能不对，或站点已挂/被墙")
        if isinstance(je, ValueError) and str(je).startswith("Expecting value"):
            raise ValueError("返回的既不是 JSON 也不是 XML —— "
                             "地址可能不对，或站点返回了别的东西")
        raise


def _json(raw):
    """兼容旧调用：等同于 load_payload（保留这个名字，避免外部调用点全改）。"""
    return load_payload(raw)


def routes_of(play_url):
    """vod_play_url 里一共有几条线路（以 $$$ 分组，不管能不能放）。
    用来区分「单线路源 / 多线路源」——多线路的源更抗单条线路失效。"""
    return len([g for g in (play_url or "").split("$$$") if g.strip()])


def probe(api, referer, kw, timeout):
    """按 mitm.py 的真实调用探测一个源。返回 dict。

    返回字段：ms 响应毫秒 / hits 命中数 / playable 可放线路数 /
              routes 线路总数 / title 首条片名 / fmt 接口格式(json|xml) /
              err 错误说明
    """
    out = {"ms": None, "hits": None, "playable": None, "routes": None,
           "title": "", "fmt": "", "err": ""}
    sep = "&" if "?" in api else "?"
    try:
        raw, dt = fetch(api + sep + "ac=videolist&wd=" + urllib.parse.quote(kw),
                        referer, timeout)
        out["ms"] = dt * 1000.0
        out["fmt"] = payload_fmt(raw)
        lst = load_payload(raw).get("list") or []
        out["hits"] = len(lst)
        if not lst:
            return out
        it = lst[0]
        out["title"] = (it.get("vod_name") or "").strip()
        pu = it.get("vod_play_url") or ""
        n = playable_lines(pu)
        if not n:                       # 搜索没带可放地址 → 补拉详情（代码也这么做）
            vid = it.get("vod_id")
            if vid:
                raw2, _ = fetch(api + sep + "ac=detail&ids=" +
                                urllib.parse.quote(str(vid)), referer, timeout)
                d2 = load_payload(raw2).get("list") or []
                if d2:
                    pu = d2[0].get("vod_play_url") or ""
                    n = playable_lines(pu)
        out["playable"] = n
        out["routes"] = routes_of(pu)
    except Exception as e:
        msg = str(e)
        # JSONDecodeError 的原文（Expecting value: line 1 column 1）对用户没意义
        if isinstance(e, ValueError) and msg.startswith("Expecting value"):
            msg = "返回的不是 JSON（也不是 XML）—— 地址可能不对，或站点挂了"
        out["err"] = "%s: %s" % (type(e).__name__, msg[:60])
    return out


def load_file(path):
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    rows = []
    for level in ("primary", "fallback"):
        for it in (obj.get(level) or []):
            if isinstance(it, dict) and it.get("api"):
                rows.append((level, it.get("name") or "-", it["api"],
                             it.get("referer") or ""))
    return rows


def main():
    ap = argparse.ArgumentParser(description="苹果CMS 采集站体检")
    ap.add_argument("urls", nargs="*", help="直接指定采集接口（不读配置文件）")
    ap.add_argument("--file", default=DEFAULT_FILE, help="sources.json 路径")
    ap.add_argument("--keyword", default="凡人", help="搜索关键词（默认 凡人）")
    ap.add_argument("--timeout", type=float, default=25.0, help="单个请求超时秒数")
    a = ap.parse_args()

    if a.urls:
        rows = [("-", u, u, "") for u in a.urls]
        src = "命令行参数"
    else:
        if not os.path.exists(a.file):
            print("找不到配置文件：%s" % a.file)
            print("提示：也可以直接把接口地址当参数传进来")
            return 2
        rows = load_file(a.file)
        src = a.file

    print("源清单：%s（%d 个）   搜索关键词：%s" % (src, len(rows), a.keyword))
    print()
    print("%-9s %-12s %-9s %-6s %-6s %-5s %s" %
          ("级别", "名称", "搜索耗时", "命中", "可放线", "格式", "判定"))
    print("-" * 90)

    good = []
    for level, name, api, referer in rows:
        r = probe(api, referer, a.keyword, a.timeout)
        ms = "%.0fms" % r["ms"] if r["ms"] is not None else "-"
        hit = "-" if r["hits"] is None else str(r["hits"])
        pl = "-" if r["playable"] is None else str(r["playable"])
        if r["hits"] is None:
            verdict = "❌ 不可用（%s）" % (r["err"] or "无响应")
        elif r["hits"] == 0:
            verdict = "⚠️ 站点活着，该关键词无结果（换个词再试）"
        elif not r["playable"]:
            verdict = "❌ 搜到《%s》但无可放线路（只有分享页/无地址）" % r["title"][:14]
        else:
            verdict = "✅ 可出片源（%s 条线路）" % r["playable"]
            good.append((r["ms"], name, api))
        print("%-9s %-12s %-9s %-6s %-6s %-5s %s" %
              (level, name[:12], ms, hit, pl,
               (r["fmt"] or "-").upper(), verdict))

    print()
    if good:
        good.sort()
        print("按搜索耗时排序（可用的源，建议放进 primary）：")
        for ms, name, api in good:
            print("  %6.0fms  %-12s %s" % (ms, name, api))
    else:
        print("没有可用源：检查网络，或换关键词 / 换源。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
