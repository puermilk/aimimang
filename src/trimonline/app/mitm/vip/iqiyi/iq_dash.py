#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱奇艺 /dash 直连取流（纯标准库，无浏览器）。

来源：media-dl 1.9.0 的 app/sites/iqiyi.py（签名算法与参数已对拍实测）。
本模块只做三件事：
  1. tvid -> 整集 m3u8（内嵌在 /dash 响应里，分片是明文 TS，无 EXT-X-KEY）
  2. tvid -> 字幕（program.stl[].webvtt / srt / xml）
  3. 档位（bid）选择：800(4K) > 700(2K) > 600(1080P) > 500(720P) ...

要点（踩过的坑，别改）：
  * authKey = md5(md5(up) + tm + tvid)，tm 是**绝对毫秒时间戳**
  * vf = md5(URL 去掉 scheme://host + 32 字符后缀)，后缀用 bundle 那套
  * ut=<会员类型> 必须发；匿名发 ut=0。会员类型从 ctl.ut 回读并缓存
  * pck = Cookie 的 P00001/P00007，VIP 正片靠它才给全片（否则只给 6 分钟）
  * 分片 URL 带 qd_sc 签名，**与本机出口 IP 绑定**，所以必须由 NAS 代理
  * m3u8 有效期约 600s（va - t），过期要重取 /dash
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DASH_URL = "https://cache.video.iqiyi.com/dash"
DASH_RETRY = "https://retry-cache.video.iqiyi.com/dash"
STORAGE = "/root/iqiyi_login/storage_state.json"
BASE = os.path.dirname(os.path.abspath(__file__))
UT_FILE = os.path.join(BASE, "dash_ut.json")

PROFILE = {
    "src": "01010031010000000000", "ori": "pcw", "qd_v": "a1", "k_tag": "1",
    "locale": "zh_cn", "fr": "120_120_120_120_120_120", "bop_version": "10.0",
    "ft": {"k_ft1": 706436220846084, "k_ft4": 1170017879597060,
           "k_ft5": 17592337039361, "k_ft7": 26422940794884,
           "k_ftx": "CA", "k_ftx2": "g"},
    "b_ft1": 60,
}

BID_LABELS = {"100": "极速", "200": "360P", "300": "高清", "400": "超清",
              "500": "720P", "600": "1080P", "601": "HDR", "610": "1080P50",
              "700": "2K", "800": "4K", "1020": "8K"}

_cache = {}          # tvid -> {ts, m3u8, sub, bid, dur, ut, stl}
_drm = set()         # 出现 EXT-X-KEY 的集: 不碰 DRM, 直接拒
_unplayable = set()  # /dash 正常返回但没有可播流(花絮/无版权等): 别再去起浏览器
_lock = threading.Lock()
_ck_cache = {"ts": 0, "ck": {}}
_ut = None
_session = None


def _sess():
    """复用一个连接池, 省掉每次 /dash 的 TLS 握手。"""
    global _session
    if _session is None:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://www.iqiyi.com/",
                          "Accept": "application/json, text/plain, */*"})
        ad = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16,
                                           max_retries=0)
        s.mount("https://", ad)
        s.mount("http://", ad)
        _session = s
    return _session


def log(*a):
    print(time.strftime("%H:%M:%S"), "dash", *a, flush=True)


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _suffix_bundle(text):
    a = text
    for t in range(4):
        for i in range(2):
            for n in range(4):
                r = (70 * t + 677 * i + 21 * n + 87 * t * i * n + 59) % 30
                r += 48 if r < 9 else 88
                a += chr(r)
    return a


def _make_auth_key(tvid, tm, up=""):
    return _md5(_md5(up) + str(tm) + str(tvid))


def _make_vf(url):
    bare = re.sub(r"^(http|https)://[^/]+", "", url, flags=re.I)
    return _md5(_suffix_bundle(bare))


def cookies():
    """读扫码登录的 Cookie（文件变了就重读）。"""
    try:
        mt = os.path.getmtime(STORAGE)
    except OSError:
        return {}
    if _ck_cache["ts"] == mt and _ck_cache["ck"]:
        return _ck_cache["ck"]
    try:
        st = json.load(open(STORAGE))
        ck = {c["name"]: c["value"] for c in st.get("cookies", [])}
    except Exception:
        return _ck_cache["ck"]
    _ck_cache.update(ts=mt, ck=ck)
    return ck


def _vip_uts(ck):
    """会员类型列表：优先上次 ctl.ut 回读值，否则 Cookie 的 vipTypes，最后 [4,14]。"""
    global _ut
    if _ut:
        return _ut
    try:
        raw = urllib.parse.unquote(ck.get("vipTypes") or "")
        got = [v.strip() for v in raw.split(",") if v.strip().isdigit()]
        if got:
            _ut = got
            return _ut
    except Exception:
        pass
    try:
        got = json.load(open(UT_FILE))
        if isinstance(got, list) and got:
            _ut = [str(x) for x in got]
            return _ut
    except Exception:
        pass
    _ut = ["4", "14"]
    return _ut


def _save_uts(uts):
    global _ut
    try:
        vals = [str(x) for x in (uts or []) if str(x).isdigit()]
        if vals and vals != _ut:
            _ut = vals
            tmp = UT_FILE + ".tmp"
            json.dump(_ut, open(tmp, "w"))
            os.replace(tmp, UT_FILE)
    except Exception:
        pass


def _build(tvid, bid, ck, base=DASH_URL):
    now = int(time.time() * 1000)
    uid = ck.get("P00003") or ""
    dfp = (ck.get("__dfp") or "").split("@")[0]
    k_uid = ck.get("QC005") or ck.get("QC006") or _md5(secrets.token_hex(8))
    pck = ck.get("P00001") or ck.get("P00007") or ""
    ft = PROFILE["ft"]
    p = {
        "tvid": str(tvid), "bid": str(bid), "vid": None,
        "src": PROFILE["src"], "vt": "0", "rs": "1", "uid": uid,
        "ori": PROFILE["ori"], "ps": "0", "k_uid": k_uid, "pt": "0",
        "d": "0", "s": "", "lid": "0", "cf": "0", "ct": "0",
        "authKey": _make_auth_key(tvid, now), "k_tag": PROFILE["k_tag"],
        "dfp": dfp, "locale": PROFILE["locale"], "up": "", "qd_v": PROFILE["qd_v"],
        "tm": str(now), "k_ft1": str(ft["k_ft1"]), "k_ft4": str(ft["k_ft4"]),
        "k_ft5": str(ft["k_ft5"]), "k_ft7": str(ft["k_ft7"]),
        "k_ftx": ft["k_ftx"], "k_ftx2": ft["k_ftx2"],
        "fr_300": PROFILE["fr"], "fr_500": PROFILE["fr"],
        "fr_600": PROFILE["fr"], "fr_800": PROFILE["fr"],
        "bop": json.dumps({"version": PROFILE["bop_version"], "dfp": dfp,
                           "b_ft1": PROFILE["b_ft1"]}, separators=(",", ":")),
        "sr": "1", "pck": pck,
    }
    p = {k: v for k, v in p.items() if v is not None}
    url = base + "?" + urllib.parse.urlencode([(k, str(v)) for k, v in p.items()])
    for ut in _vip_uts(ck):
        url += "&ut=" + ut
    return url + "&vf=" + _make_vf(url)


def _get(url, timeout=15):
    r = _sess().get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def _call(tvid, bid):
    ck = cookies()
    last = ""
    for base in (DASH_URL, DASH_RETRY):
        try:
            raw = _get(_build(tvid, bid, ck, base))
        except Exception as e:
            last = repr(e)[:100]
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            last = "bad json"
            continue
        code = d.get("code")
        if code != "A00000":
            last = f"code={code} msg={d.get('msg')}"
            continue
        return d
    log("call fail", tvid, bid, last)
    return None


def _pick(data):
    """从 /dash 响应里取选中档的 m3u8、字幕、时长。"""
    prog = (data.get("data") or {}).get("program") or {}
    vids = [v for v in (prog.get("video") or [])
            if isinstance(v, dict) and v.get("m3u8")]
    if not vids:
        return None
    # 有多档带清单时取最高档（bid 数字越大越清晰）
    def _bid(v):
        try:
            return int(v.get("bid") or 0)
        except Exception:
            return 0
    sel = max(vids, key=_bid)
    stl = prog.get("stl") or []
    sub = ""
    # 优先主字幕(lid=1)，其次 main=1，最后第一条
    cand = [s for s in stl if isinstance(s, dict) and s.get("webvtt")]
    pick = None
    for s in cand:
        if str(s.get("lid")) == "1" or s.get("main") == 1:
            pick = s
            break
    if pick is None and cand:
        pick = cand[0]
    if pick:
        base = (data.get("data") or {}).get("dstl") or "http://meta.video.iqiyi.com"
        sub = base.rstrip("/") + pick["webvtt"]
    dur = 0
    try:
        dur = int(float(sel.get("duration") or 0))
    except Exception:
        dur = 0
    ctl = (data.get("data") or {}).get("ctl") or {}
    return {"m3u8": sel["m3u8"], "sub": sub, "bid": str(ctl.get("bid") or sel.get("bid") or ""),
            "dur": dur, "stl": stl, "ut": ctl.get("ut")}


def get(tvid, force=False, ttl=420):
    """取整集 m3u8（带缓存）。失败返回 None。"""
    tvid = str(tvid)
    now = time.time()
    with _lock:
        c = _cache.get(tvid)
    if c and not force and now - c["ts"] < ttl:
        return c
    # 强制刷新也限速: 5s 内已经刷过就直接复用(避免分片批量 4xx 时打爆 /dash)
    if force and c and now - c["ts"] < 5:
        return c
    # 档位从高到低试，服务端会给它实际能给的最高档。
    # 优先缓存带字幕(sub)的结果，避免某次/dash重签时爱奇艺偶尔
    # 不返回 program.stl 导致主清单不注入字幕轨(字幕间歇性失效)。
    got_any = False
    best = None      # 带字幕的结果(优先)
    fallback = None  # 第一个可播结果(无字幕时兜底)
    for bid in ("800", "700", "600", "500", "300"):
        d = _call(tvid, bid)
        if not d:
            continue
        got_any = True
        got = _pick(d)
        if not got:
            continue
        if "#EXT-X-KEY" in got["m3u8"]:
            log("DRM/加密清单, 拒绝:", tvid)
            with _lock:
                _drm.add(tvid)
            return None
        got.update(ts=time.time(), tvid=tvid)
        _save_uts(got.get("ut"))
        if got.get("sub"):
            best = got
            break           # 有字幕就用这个，不再继续试低档
        if fallback is None:
            fallback = got  # 先记个兜底，继续找有字幕的
    got = best or fallback
    if got:
        with _lock:
            _cache[tvid] = got
        log("ok", tvid, "bid", got["bid"], "segs", got["m3u8"].count(".ts"),
            "dur", got["dur"], "sub", bool(got["sub"]))
        return got
    if got_any:
        # 接口正常但没有可播流: 标记, 避免回落到 25s 的浏览器等待
        log("no-stream:", tvid)
        with _lock:
            _unplayable.add(tvid)
    return None


def seg_urls(m3u8):
    """清单里的分片 URL 列表（按顺序）。"""
    return [l.strip() for l in (m3u8 or "").splitlines()
            if l.strip().startswith("http") and ".ts?" in l]


def seg_at(tvid, idx):
    """取第 idx 个分片的最新地址（过期重签用）。"""
    d = get(tvid, force=True)
    if not d:
        return None
    arr = seg_urls(d["m3u8"])
    try:
        i = int(idx)
    except Exception:
        return None
    if 0 <= i < len(arr):
        return arr[i]
    return None


def subtitle_text(tvid):
    """字幕正文（官方 WebVTT 优先）。"""
    d = get(tvid)
    if not d or not d.get("sub"):
        # 缓存里没字幕，强制重签再试一次：爱奇艺接口偶尔不返回
        # program.stl，导致主清单不注入字幕轨(字幕间歇性失效)。
        d = get(tvid, force=True)
    if not d or not d.get("sub"):
        return None
    try:
        r = _sess().get(d["sub"], timeout=15)
        r.raise_for_status()
        body = r.content.decode("utf-8", "replace")
        if not body.startswith("WEBVTT"):
            return None
        # 补 HLS 要求的时间戳映射（官方文件没带）
        if "X-TIMESTAMP-MAP" not in body:
            body = body.replace("WEBVTT", "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0", 1)
        return body
    except Exception as e:
        log("sub fetch fail", tvid, repr(e)[:80])
        return None


def duration(tvid):
    d = get(tvid)
    return int(d.get("dur") or 0) if d else 0


def has_subtitle(tvid):
    """轻量级：是否有可用字幕(不拉取 VTT 正文)。

    缓存里没 sub 时 force 重签再试一次，避免爱奇艺接口偶尔
    不返回 program.stl 导致字幕间歇性失效。
    """
    d = get(tvid)
    if d and d.get("sub"):
        return True
    d = get(tvid, force=True)
    return bool(d and d.get("sub"))


def is_dash_url(u):
    """是否 /dash 直连的签名分片(带 qd_sc, 由 NAS 代理拉取)。"""
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    return (host.endswith(".iqiyi.com") and p.path.startswith("/videos/")
            and ".ts" in p.path and "qd_sc=" in (p.query or ""))


def is_drm(tvid):
    return str(tvid) in _drm


def is_unplayable(tvid):
    return str(tvid) in _unplayable
