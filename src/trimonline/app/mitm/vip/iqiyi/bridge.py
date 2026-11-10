#!/usr/bin/env python3
"""爱奇艺采集桥: 标准苹果CMS V10 采集接口 + 播放地址实时解析服务。

接口:
  GET /api.php/provide/vod/?ac=videolist&wd=关键词   搜索(直接带分集)
  GET /api.php/provide/vod/?ac=detail&ids=qipuId    详情(全集 vod_play_url)
  GET /p/<tvid>.m3u8                                实时解析出真 m3u8(缓存1h)
  GET /health                                       健康检查/登录态状态

红线: 只解析登录账号已有权限的内容; 无权限返回空/错误, 不碰DRM。
"""
import json, os, re, subprocess, threading, time, urllib.parse, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import iq_api
import iq_dash

BASE = os.path.dirname(os.path.abspath(__file__))
# 2.0：配置文件由爱迷茫主程序统一写在 conf/config.json；缓存/签名文件写到
# 可写的 vip_data，避免安装目录被应用中心只读挂载时起不来。
CONF_DIR = os.environ.get("AIMIMANG_CONF", "/usr/local/trim_media_mitm/conf")
DATA_DIR = os.environ.get("AIMIMANG_DATA", "/usr/local/trim_media_mitm/vip_data")
os.makedirs(DATA_DIR, exist_ok=True)
try:
    _main = json.load(open(os.path.join(CONF_DIR, "config.json")))
except Exception:
    _main = {}
CONF = {
    "listen": "127.0.0.1",
    "port": int(_main.get("vip_iqiyi_port") or 8770),
    "base": "",
    "storage_state": _main.get("vip_iqiyi_state")
                     or os.path.join(CONF_DIR, "iqiyi_state.json"),
    "m3u8_ttl": 900,
    "resolve_timeout": 260,
    "device": "dfa1caa4c808888f3ded4071d41e9bb5",
    "uid": "2401485736",
}
CONF["base"] = "http://127.0.0.1:%d" % CONF["port"]
CACHE_DB = os.path.join(DATA_DIR, "iqiyi_cache.json")

# 登录态文件缺失时仍要能启动：搜索可用，点播时由爱奇艺返回无权限。
try:
    iq = iq_api.Iqiyi(state_path=CONF["storage_state"],
                      dev=CONF.get("device"), uid=CONF.get("uid"))
except Exception as _e:
    print("iqiyi login state unavailable:", repr(_e)[:120], flush=True)
    iq = iq_api.Iqiyi.__new__(iq_api.Iqiyi)
    iq.state_path = CONF["storage_state"]
    iq.ck, iq.P = {}, ""
    iq.h = {"User-Agent": iq_api.UA, "Referer": "https://www.iqiyi.com/"}
    iq.dev, iq.uid = CONF["device"], CONF["uid"]
    iq._mt = None
iq_dash.STORAGE = CONF["storage_state"]
iq_dash.UT_FILE = os.path.join(DATA_DIR, "iqiyi_dash_ut.json")

# ---------- 缓存 ----------
_cache = json.load(open(CACHE_DB))["entries"] if os.path.exists(CACHE_DB) else {}
_cache_lock = threading.Lock()
_cache_dirty = False

def cache_get(k, ttl):
    with _cache_lock:
        e = _cache.get(k)
        if e and time.time() - e["t"] < ttl:
            return e["v"]
        return None

def cache_put(k, v):
    global _cache_dirty
    with _cache_lock:
        _cache[k] = {"t": time.time(), "v": v}
        _cache_dirty = True

def cache_flush():
    global _cache_dirty
    with _cache_lock:
        if _cache_dirty:
            json.dump({"entries": _cache}, open(CACHE_DB, "w"))
            _cache_dirty = False

def flusher():
    while True:
        cache_flush()
        time.sleep(30)

# ---------- 解析器(单并发队列) ----------
_res_lock = threading.Lock()
_res_running = False
_res_cond = threading.Condition(_res_lock)

_running = {}       # tvid -> Popen
_run_lock = threading.Lock()

def _res_path(tvid):
    return os.path.join(BASE, f"res_{tvid}.json")

def read_res_file(tvid):
    p = _res_path(tvid)
    if not os.path.exists(p):
        return None
    try:
        j = json.load(open(p))
        if time.time() - j.get("saved_at", 0) > CONF["m3u8_ttl"]:
            return None
        return j
    except Exception:
        return None

def get_res(tvid):
    full = cache_get(f"res:{tvid}", CONF["m3u8_ttl"])
    if full and full.get("ok"):
        return full
    return read_res_file(tvid)

def ensure_resolver(tvid, page_url, force=False):
    if get_res(tvid) and not force:
        return
    with _run_lock:
        if tvid in _running:
            return
        # 切换播放: 杀掉其它集的解析器, 立刻解析当前集(抢占)
        for t, pr in list(_running.items()):
            if t != tvid:
                try:
                    log("preempt resolver:", t, "->", tvid)
                    pr.kill()
                except Exception:
                    pass
                _running.pop(t, None)
    log("start resolver:", tvid)
    try:
        proc = subprocess.Popen(
            ["python3", os.path.join(BASE, "resolver_worker.py"),
             page_url, _res_path(tvid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log("resolver spawn err:", tvid, str(e)[:80])
        return
    with _run_lock:
        _running[tvid] = proc

    def waiter():
        try:
            proc.wait(timeout=CONF["resolve_timeout"])
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        r = read_res_file(tvid)
        if r and r.get("ok"):
            r2 = dict(r)
            r2.pop("partial", None)
            cache_put(f"res:{tvid}", r2)
        with _run_lock:
            if _running.get(tvid) is proc:
                _running.pop(tvid, None)
    threading.Thread(target=waiter, daemon=True).start()

# ---------- 常驻实时签名会话 ----------
_signers = {}
_sign_lock = threading.Lock()
_playing_tvids = set()      # 正在被播放的集(不抢占其会话)
_last_seg = {}              # tvid -> 最近一次分片请求时间(用于回收空闲会话)

def _sign_path(tvid):
    return os.path.join(BASE, f"sign_{tvid}.json")

def read_sign(tvid):
    p = _sign_path(tvid)
    if not os.path.exists(p):
        return None
    try:
        j = json.load(open(p))
        if time.time() - j.get("ts", 0) > 40:
            return None
        return j
    except Exception:
        return None

def ensure_signer(tvid, page_url, force=False, start=0):
    with _sign_lock:
        if not force:
            for t, pr in list(_signers.items()):
                if t != tvid and t in _playing_tvids:
                    continue            # 正在播的集: 不抢占
                if t != tvid:
                    try:
                        log("preempt signer:", t, "->", tvid)
                        pr.kill()
                    except Exception:
                        pass
                    _signers.pop(t, None)
        pr = _signers.get(tvid)
        if pr and pr.poll() is None:
            return
    log("start signer:", tvid, "start=", int(start))
    env = dict(os.environ)
    env["SIGN_START"] = str(max(0.0, float(start or 0)))
    try:
        proc = subprocess.Popen(
            ["python3", os.path.join(BASE, "signer_worker.py"), page_url, tvid, BASE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            env=env)
    except Exception as e:
        log("signer spawn err:", tvid, str(e)[:80])
        return
    with _sign_lock:
        _signers[tvid] = proc

def signer_killer():
    while True:
        time.sleep(60)
        try:
            with _sign_lock:
                for t, pr in list(_signers.items()):
                    if pr.poll() is not None:
                        _signers.pop(t, None)
                # 清理"在播"标记: 4 分钟没有分片请求 -> 认为已停止播放, 回收会话
                now = time.time()
                for t, pr in list(_signers.items()):
                    if now - float(_last_seg.get(t, 0) or 0) > 240:
                        try:
                            log("reclaim signer (idle):", t)
                            pr.kill()
                        except Exception:
                            pass
                        _signers.pop(t, None)
                        _playing_tvids.discard(t)
        except Exception:
            pass

# ---------- 苹果CMS 组装 ----------
CATEGORIES = {"电视剧": "连续剧", "电影": "电影", "综艺": "综艺", "动漫": "动漫"}

def vod_from_album(a):
    epid = a.get("qipuId")
    v0 = iq.parse_playurl(a.get("playUrl") or "")
    albumid = v0.get("albumid") or epid
    eps = a.get("_episodes") or a.get("videos") or []
    total = a.get("totalNumber") or len(eps)
    sub = a.get("subscriptContent") or f"共{total}集"
    title = a.get("title") or ""
    cat = (a.get("channel") or "").split(",")[0]
    pic = a.get("img") or ""
    # vod_play_url: 每集 -> 桥地址 (集名$URL # ...)
    parts = []
    for v in eps:
        tv = iq.parse_playurl(v.get("playUrl") or "").get("tvid")
        if not tv:
            continue
        name = v.get("title") or v.get("number") or "正片"
        parts.append(f"{name}${CONF['base']}/p/{tv}.m3u8")
    play_url = "#".join(parts)
    content = a.get("introduction") or (a.get("brief") or {}).get("value") or ""
    if isinstance(content, dict):
        content = content.get("value") or ""
    if isinstance(content, list):
        content = ",".join(str(x.get("title", x)) if isinstance(x, dict) else str(x) for x in content[:10])
    actor = (a.get("actors") or {}).get("value") or ""
    if isinstance(actor, list):
        actor = ",".join(x.get("title", "") for x in actor[:8] if isinstance(x, dict))
    director = (a.get("directors") or {}).get("value") or ""
    if isinstance(director, list):
        director = ",".join(x.get("title", "") for x in director[:5] if isinstance(x, dict))
    ret = {
        "vod_id": str(epid),
        "vod_name": title,
        "vod_en": str(albumid),
        "type_name": CATEGORIES.get(cat, cat or "正片"),
        "vod_pic": pic,
        "vod_remarks": sub,
        "vod_year": (a.get("year") or {}).get("value", ""),
        "vod_area": (a.get("region") or {}).get("value", ""),
        "vod_content": content[:500],
        "vod_actor": actor,
        "vod_director": director,
        "vod_play_from": "iqiyi",
        "vod_play_url": play_url,
    }
    # 缓存 album -> 每集(tvid -> pageUrl) 映射, 供解析服务定位
    runtime = []
    for v in eps:
        tv = iq.parse_playurl(v.get("playUrl") or "").get("tvid")
        if tv:
            runtime.append((tv, v.get("pageUrl") or ""))
    cache_put(f"album:{albumid}", {"_eps_runtime": runtime, "eps": [tv for tv, _ in runtime],
                                   "mac": ret})
    return ret

def mac_list(items, total=0, pg=1):
    return {"code": 1, "msg": "数据列表", "page": pg, "pagecount": 1, "limit": str(len(items)),
            "total": total or len(items), "list": items}

# ---------- HTTP ----------
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _m3u8(self, text, code=200, note=None):
        if note:
            # 注释必须放在 #EXTM3U 之后, 否则清单非法
            lines = text.split("\n", 1)
            text = (lines[0] + f"\n# 注: {note}\n" + (lines[1] if len(lines) > 1 else ""))
        body = text.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            log("m3u8 served, bytes:", len(body))
        except Exception:
            log("m3u8 write aborted (client closed)")

    def do_detail(self, ids):
        """按 ids(专辑 qipuId) 从缓存返回完整卡片"""
        idset = {x for x in ids.split(",") if x}
        with _cache_lock:
            macs = []
            for k, e in list(_cache.items()):
                if not k.startswith("album:"):
                    continue
                if time.time() - e["t"] > 24 * 3600:
                    continue
                mac = (e.get("v") or {}).get("mac")
                if mac and (mac.get("vod_id") in idset or mac.get("vod_en") in idset):
                    macs.append(mac)
        cache_flush()
        if macs:
            threading.Thread(target=prewarm_mac, args=(macs[0],),
                             daemon=True).start()
        return self._json(mac_list(macs))

    def do_HEAD(self):
        # 播放器常先 HEAD 探测
        try:
            u = urllib.parse.urlparse(self.path)
            if u.path.startswith("/s"):
                q = urllib.parse.parse_qs(u.query)
                tvid = (q.get("t") or [""])[0]
                seg = urllib.parse.unquote((q.get("u") or [""])[0])
                import requests as rq
                p = re.match(r'https?://[^/]+(/videos/[^?]+)', seg)
                url = seg
                r = cache_get(f"res:{tvid}", 10 ** 9) or {}
                if p and (r.get("templates") or {}).get(p.group(1)):
                    url = construct_seg_url(r["templates"][p.group(1)], seg)
                rr = rq.get(url, headers={"User-Agent": iq.h["User-Agent"],
                                          "Referer": "https://www.iqiyi.com/"}, timeout=20, stream=True)
                self.send_response(rr.status_code)
                ctype = rr.headers.get("Content-Type", "video/mp2t")
                if "octet-stream" in ctype:
                    ctype = "video/mp2t"
                self.send_header("Content-Type", ctype)
                if rr.headers.get("Content-Length"):
                    self.send_header("Content-Length", rr.headers["Content-Length"])
                else:
                    self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self._cors()
                self.end_headers()
                rr.close()
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
        except Exception:
            try:
                self.send_error(502)
            except Exception:
                pass

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path
            # 采集接口
            if path.startswith("/api.php/provide/vod"):
                ac = (q.get("ac") or ["videolist"])[0]
                if ac == "detail":
                    ids = (q.get("ids") or [""])[0]
                    return self.do_detail(ids)
                wd = (q.get("wd") or [""]).pop()
                if not wd:
                    return self._json(mac_list([]))
                # BaseHTTPRequestHandler 按 latin-1 解码路径，UTF-8 中文关键词需还原
                try:
                    wd = wd.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
                albums = iq.native_albums(wd)
                macs = [vod_from_album(a) for a in albums]
                if macs:
                    threading.Thread(target=prewarm_mac, args=(macs[0],),
                                     daemon=True).start()
                return self._json(mac_list(macs))
            if path.startswith("/api.php/provide/vod/list"):
                pg = int((q.get("pg") or ["1"])[0])
                return self._json(mac_list([]), )
            # 播放地址
            if path.startswith("/p/"):
                tvid = path.rsplit("/", 1)[-1].split(".")[0]
                return serve_playlist(self, tvid)
            if path.startswith("/m/"):
                tvid = path.rsplit("/", 1)[-1].split(".")[0]
                return serve_media(self, tvid)
            if path.startswith("/sp/"):
                tvid = path.rsplit("/", 1)[-1].split(".")[0]
                return serve_sub_playlist(self, tvid)
            if path.startswith("/sub/"):
                tvid = path.rsplit("/", 1)[-1].split(".")[0]
                return serve_subtitle(self, tvid)
            if path.startswith("/s"):
                tvid = (q.get("t") or [""])[0]
                return serve_segment(self, tvid)
            if path == "/health":
                try:
                    iq.refresh()
                except Exception:
                    pass
                state = {"P00001": bool(iq.P), "device": iq.dev, "uid": iq.uid}
                return self._json({"ok": 1, "service": "iqiyi-bridge", "login": state})
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            try:
                self._json({"code": 0, "msg": f"bridge error: {traceback.format_exc()[:800]}"}, 500)
            except Exception:
                pass

def _sess_dir():
    d = os.path.join(BASE, "sessions")
    os.makedirs(d, exist_ok=True)
    return d

def _proc_alive(tvid):
    """有活着的 session_worker 吗"""
    import subprocess
    out = subprocess.run(["pgrep", "-f", f"session_worker.py .* {tvid} "],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())

def ensure_session(tvid):
    """保证该集有一个在跑的实时会话; 返回 pageUrl 或 None"""
    page_url = cache_get(f"page:{tvid}", 24 * 3600) or find_page_url(tvid)
    if not page_url:
        return None
    cache_put(f"page:{tvid}", page_url)
    if _proc_alive(tvid):
        return page_url
    import subprocess
    log("spawn session:", tvid)
    subprocess.Popen(
        ["python3", os.path.join(BASE, "session_worker.py"), page_url, tvid, _sess_dir()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    return page_url

def read_session(tvid):
    p = os.path.join(_sess_dir(), f"{tvid}.state.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None

def seg_key_from_manifest_line(url):
    m = re.search(r'(/videos/[^?]+)\?.*?\bstart=(\d+)', url)
    return f"{m.group(1)}|{m.group(2)}" if m else None

def manifest_seg_times(manifest_text):
    """返回 [(key, 累计秒)] 便于按分片位置 seek"""
    times, acc = {}, 0.0
    for line in manifest_text.splitlines():
        s = line.strip()
        if s.startswith("#EXTINF:"):
            try:
                acc += float(s[8:].split(",")[0])
            except Exception:
                pass
        elif s.startswith("https://data.video.iqiyi.com/") and ".ts?" in s:
            k = seg_key_from_manifest_line(s)
            if k and k not in times:
                times[k] = acc
    return times

# ---------- 播放清单(模板构造, 无需实时浏览器) ----------
def construct_seg_url(template, seg_line):
    """用模板 URL 克隆出目标分片: 只替换 start/end/contentlength/sd"""
    tu = urllib.parse.urlparse(template)
    tq = dict((k, v[0]) for k, v in urllib.parse.parse_qs(tu.query).items())
    sq = urllib.parse.parse_qs(urllib.parse.urlparse(seg_line).query)
    for f in ("start", "end", "contentlength", "sd"):
        if f in sq:
            tq[f] = sq[f][0]
    return tu._replace(query=urllib.parse.urlencode(tq)).geturl()

def _ms(t):
    h, t1 = divmod(int(t), 3600000)
    m, t2 = divmod(t1, 60000)
    s, ms = divmod(t2, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _vts(t):
    h, t1 = divmod(int(t), 3600000)
    m, t2 = divmod(t1, 60000)
    s, ms = divmod(t2, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def xml_to_srt(xml_text):
    import re as _re
    out, i = [], 0
    for m in _re.finditer(
            r"<dia>\s*<st>(\d+)</st>\s*<et>(\d+)</et>\s*<sub>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</sub>\s*</dia>",
            xml_text, _re.S):
        st, et, txt = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if not txt:
            continue
        i += 1
        out.append(f"{i}\n{_ms(st)} --> {_ms(et)}\n{txt}\n")
    return "\n".join(out)

def xml_to_vtt(xml_text):
    import re as _re
    out = ["WEBVTT", "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0", ""]
    for m in _re.finditer(
            r"<dia>\s*<st>(\d+)</st>\s*<et>(\d+)</et>\s*<sub>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</sub>\s*</dia>",
            xml_text, _re.S):
        st, et, txt = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if not txt:
            continue
        out.append(f"{_vts(st)} --> {_vts(et)}")
        out.append(txt)
        out.append("")
    return "\n".join(out)

def _fetch_sub_text(tvid):
    r = read_sign(tvid) or get_res(tvid) or {}
    url = r.get("subtitle")
    if not url:
        return None
    import requests as rq
    rr = rq.get(url, headers={"User-Agent": iq.h["User-Agent"],
                              "Referer": "https://www.iqiyi.com/"}, timeout=15)
    rr.encoding = "utf-8"
    return rr.text

def serve_subtitle(self, tvid, fmt="vtt"):
    """HLS 内嵌字幕: 返回 WebVTT(供 #EXT-X-MEDIA 字幕轨)"""
    # 快路: /dash 的 program.stl 直接给官方 WebVTT
    if fmt != "srt":
        try:
            vtt = iq_dash.subtitle_text(tvid)
        except Exception as e:
            log("subtitle(dash) fail:", tvid, repr(e)[:80])
            vtt = None
        if vtt:
            # 标准化 WebVTT：iOS AVPlayer 对时间戳行格式严格，
            # 官方文件常是 "00:00:00.000  --> 00:00:01.000 "（双空格+尾空格）
            # 会导致字幕不显示；统一成单空格、去尾空格，压缩连续空行。
            out = []
            for line in vtt.split("\n"):
                if "-->" in line:
                    m = re.search(
                        r'(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})', line)
                    if m:
                        line = m.group(1) + " --> " + m.group(2)
                out.append(line)
            vtt = "\n".join(out)
            vtt = re.sub(r'\n{3,}', '\n\n', vtt).strip() + "\n"
            body = vtt.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return
    # 兜底: 浏览器抓到的 meta.video XML
    try:
        xml = _fetch_sub_text(tvid)
    except Exception as e:
        log("subtitle fail:", tvid, str(e)[:80])
        return self.send_error(502)
    if not xml:
        return self.send_error(404)
    if fmt == "srt":
        body = xml_to_srt(xml).encode("utf-8")
        ct = "application/x-subrip; charset=utf-8"
    else:
        body = xml_to_vtt(xml).encode("utf-8")
        ct = "text/vtt; charset=utf-8"
    self.send_response(200)
    self.send_header("Content-Type", ct)
    self.send_header("Content-Length", str(len(body)))
    self._cors()
    self.end_headers()
    try:
        self.wfile.write(body)
    except Exception:
        pass

def serve_sub_playlist(self, tvid):
    """WebVTT 字幕媒体清单(单段, 覆盖整集) —— 供主清单 #EXT-X-MEDIA 引用"""
    # 快路: /dash 已带字幕 -> 时长直接用 /dash 的
    d = iq_dash.get(tvid)
    if d and d.get("sub"):
        dur = int(d.get("dur") or 0) + 1 or 3601
        vtt = f"{CONF['base']}/sub/{tvid}.vtt"
        body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                f"#EXT-X-TARGETDURATION:{dur}\n#EXT-X-MEDIA-SEQUENCE:0\n"
                f"#EXTINF:{dur:.1f},\n{vtt}\n#EXT-X-ENDLIST\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        return
    st = read_sign(tvid) or get_res(tvid)
    if not st or not st.get("manifest"):
        return self.send_error(404)
    # 字幕地址可能晚于清单抓到: 最多等 8s(播放器会重试)
    for _ in range(16):
        if st.get("subtitle"):
            break
        time.sleep(0.5)
        st = read_sign(tvid) or get_res(tvid) or st
    if not st.get("subtitle"):
        st = get_res(tvid) or st
    if not st.get("subtitle"):
        return self.send_error(404)
    try:
        dur = 0.0
        for l in st["manifest"].splitlines():
            s = l.strip()
            if s.startswith("#EXTINF:"):
                dur += float(s[8:].split(",")[0])
        dur = int(dur or 3600) + 1
    except Exception:
        dur = 3600
    vtt = f"{CONF['base']}/sub/{tvid}.vtt"
    body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            f"#EXT-X-TARGETDURATION:{dur}\n#EXT-X-MEDIA-SEQUENCE:0\n"
            f"#EXTINF:{dur:.1f},\n{vtt}\n#EXT-X-ENDLIST\n").encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
    self.send_header("Content-Length", str(len(body)))
    self._cors()
    self.end_headers()
    try:
        self.wfile.write(body)
    except Exception:
        pass

def serve_media(self, tvid):
    """主清单里引用的媒体清单(直接给已生成的, 不再等待)"""
    m = cache_get(f"man:{tvid}", 400)
    if m:
        return self._m3u8(m)
    return serve_playlist(self, tvid, media_only=True)

_RES = {"1020": ("3840x2160", 30000000), "800": ("3840x2160", 24000000),
        "700": ("2560x1440", 14000000), "610": ("1920x1080", 9000000),
        "601": ("1920x1080", 9000000), "600": ("1920x1080", 8000000),
        "500": ("1280x720", 4200000), "300": ("960x540", 2200000),
        "200": ("640x360", 1100000), "100": ("640x360", 800000)}

def _master_m3u8(tvid, sub=False, bid="600"):
    """主清单：一条变体。

    iOS 飞牛客户端不渲染 HLS 内嵌字幕轨(#EXT-X-MEDIA SUBTITLES)，
    只支持外挂字幕(play/info subtitle_guid + /subtitle/dl/os<tvid>)。
    因此不再注入 HLS 字幕轨，避免 iOS 走错路径导致无字幕。
    外挂字幕由 synth_play/synth_play_info 的 subtitle_guid 返回。
    """
    res, bw = _RES.get(str(bid), ("1920x1080", 8000000))
    out = ["#EXTM3U", "#EXT-X-VERSION:3"]
    if sub:
        # 标准 HLS 字幕轨：Web / Android / TV 的 hls.js/ExoPlayer 都能识别。
        out.append(
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",'
            'LANGUAGE="zh",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,'
            f'URI="{CONF["base"]}/sp/{tvid}.m3u8"')
    out.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw}'
               + (',SUBTITLES="subs"' if sub else '')
               + f',RESOLUTION={res}')
    out.append(f'{CONF["base"]}/m/{tvid}.m3u8')
    return "\n".join(out) + "\n"

def _b64(s):
    """URL 安全的 base64(无填充), 用于把分片真地址塞进代理 URL。

    不能用普通 query 参数: 爱迷茫插件的 /v/media/online/seg 会对 u= 做两次
    unquote, 嵌套 URL 里的 & 会被解成裸 &, 桥端 parse_qs 就在第一个 & 处截断。
    base64url 不含 % & =, 双重解码也不会坏。
    """
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")

def _unb64(s):
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")

def _media_from_dash(tvid, d):
    """把 /dash 内嵌 m3u8 的分片地址改写成桥代理（带序号 i，过期可重签）。"""
    out, i = [], 0
    for line in d["m3u8"].splitlines():
        s = line.strip()
        if s.startswith("http") and ".ts?" in s:
            out.append(f"{CONF['base']}/s/seg.ts?t={tvid}&i={i}&b={_b64(s)}")
            i += 1
        else:
            out.append(line)
    return "\n".join(out)

def serve_playlist(self, tvid, media_only=False):
    t0 = time.time()
    # ---- 快路: /dash 直连(纯 HTTP, 无浏览器), 通常 <1s ----
    d = iq_dash.get(tvid)
    if d:
        media = _media_from_dash(tvid, d)
        cache_put(f"man:{tvid}", media)
        log("p ready(dash):", tvid, "in", round(time.time() - t0, 2), "s",
            "bid", d.get("bid"), "segs", len(iq_dash.seg_urls(d["m3u8"])))
        if media_only:
            return self._m3u8(media)
        try:
            prewarm_segments(tvid, d, 3)
        except Exception:
            pass
        return self._m3u8(_master_m3u8(
            tvid, sub=False, bid=d.get("bid") or "600"))
    if iq_dash.is_drm(tvid):
        log("p DRM, 拒绝:", tvid)
        return self.send_error(403)
    if iq_dash.is_unplayable(tvid):
        log("p no-stream, 拒绝:", tvid)
        return self.send_error(404)
    # ---- 兜底: 浏览器实时签名(签名算法变更 / 特殊片源时用) ----
    page_url = cache_get(f"page:{tvid}", 24 * 3600) or find_page_url(tvid)
    if not page_url:
        # 兜底: 任何 tvid 都能用播放信息接口拿到播放页(不依赖专辑缓存)
        page_url = iq.video_page_url(tvid)
    if page_url:
        cache_put(f"page:{tvid}", page_url)
    if not page_url:
        log("p ERR no-pageurl:", tvid)
        return self.send_error(503)
    ensure_signer(tvid, page_url)
    st, waited = None, 0
    # /dash 已是主路径(0.1~0.3s)。这里只给浏览器兜底 8s：
    # 拿不到就快速 503，让播放器/插件重试（重试会命中已预热的 /dash），
    # 不再像以前那样死等 25s 让用户干瞪眼。
    while waited < 8:
        st = read_sign(tvid)
        if st and st.get("manifest") and len(st.get("templates") or {}) >= 2:
            break
        pr = _signers.get(tvid)
        if pr is not None and pr.poll() is not None:
            break  # 浏览器进程已退出，再等也没用
        time.sleep(0.4)
        waited += 0.4
    if st is None or not st.get("manifest"):
        st = read_sign(tvid)
    if not st or not st.get("manifest"):
        log("p ERR not-ready:", tvid, "waited", round(waited, 1))
        return self.send_error(503)
    log("p ready(signer):", tvid, "in", round(time.time() - t0, 1), "s")
    _playing_tvids.add(tvid)
    templates = st.get("templates") or {}
    # ---- 媒体清单(分片指向桥代理) ----
    out, n = [], 0
    for line in st["manifest"].splitlines():
        s = line.strip()
        if s.startswith("https://data.video.iqiyi.com/") and ".ts?" in s:
            if re.match(r'https?://[^/]+(/videos/[^?]+)', s):
                # 分片走桥代理(桥用 NAS 的 IP 去拉, 避免手机直连 CDN 被 IP 绑定拒绝)
                link = f"{CONF['base']}/s/seg.ts?t={tvid}&i={n}&b={_b64(s)}"
                out.append(link)
                n += 1
                continue
        out.append(line)
    media = "\n".join(out)
    cache_put(f"man:{tvid}", media)
    # ---- 返回主清单(不再注入 HLS 字幕轨，改用外挂字幕) ----
    if not media_only:
        log("master for", tvid, "segs:", n, "templates:", len(templates))
        return self._m3u8(_master_m3u8(tvid, sub=False))
    log("playlist for", tvid, "segs:", n, "templates:", len(templates))

    return self._m3u8(media)

def _seg_time(manifest, seg_url):
    """该分片在整集里的时间(秒), 用于让解析器 seek 过去补模板"""
    m = re.match(r'https?://[^/]+(/videos/[^?]+)\?(.*)', seg_url)
    if not m:
        return None
    path = m.group(1)
    q = urllib.parse.parse_qs(m.group(2))
    start = (q.get("start") or [""])[0]
    acc = 0.0
    for line in manifest.splitlines():
        s = line.strip()
        if s.startswith("#EXTINF:"):
            try:
                acc += float(s[8:].split(",")[0])
            except Exception:
                pass
        elif s.startswith("https://data.video") and ".ts?" in s:
            if path in s and (f"start={start}" in s):
                return acc
    return None

def request_seek(tvid, seg_url):
    st = get_res(tvid)
    if not st or not st.get("manifest"):
        return
    t = _seg_time(st["manifest"], seg_url)
    if t is None:
        return
    try:
        with open(os.path.join(BASE, f"seek_{tvid}"), "w") as f:
            f.write(str(max(0.0, t)))
    except Exception:
        pass

def _template_for(tvid, p, seg_url=None, wait=25):
    """取该文件的模板; 缺则让实时会话 seek 过去补签(不重开会话, 不丢已有模板)"""
    def get_tpl():
        s = read_sign(tvid)
        t = (s.get("templates") or {}).get(p) if s else None
        if t:
            return t
        r = get_res(tvid)
        return (r.get("templates") or {}).get(p) if r else None
    t = get_tpl()
    if t:
        return t
    # 会话不在(被回收/崩溃) -> 重新拉起, 再补签
    with _sign_lock:
        pr = _signers.get(tvid)
        alive = bool(pr and pr.poll() is None)
    if not alive:
        pu = cache_get(f"page:{tvid}", 24 * 3600) or find_page_url(tvid) or iq.video_page_url(tvid)
        if pu:
            cache_put(f"page:{tvid}", pu)
            log("restart signer (seg):", tvid)
            ensure_signer(tvid, pu, force=True)
    # 通知实时会话补签该文件
    try:
        with open(os.path.join(BASE, f"want_{tvid}"), "w") as f:
            f.write(p)
    except Exception:
        pass
    chk = 0
    while chk <= wait:
        t = get_tpl()
        if t:
            return t
        time.sleep(1)
        chk += 1
    return None

def _fetch_seg(self, seg_url, tvid, idx=""):
    """用自己的网络栈拉分片。支持两种源:
    - /dash 直连的签名 URL(pcw-data..., 带 qd_sc): 直接拉, 过期按序号重签
    - 浏览器清单里的 data.video 原始 URL: 用 71edge 模板构造真地址
    支持 Range 透传。
    """
    import requests as rq
    direct = iq_dash.is_dash_url(seg_url)
    headers = {"User-Agent": iq.h["User-Agent"], "Referer": "https://www.iqiyi.com/"}
    rng = self.headers.get("Range")
    if rng:
        headers["Range"] = rng
    if direct:
        url = seg_url
        path = ""
    else:
        m = re.match(r'https?://[^/]+(/videos/[^?]+)', seg_url)
        if not m:
            self.send_error(400)
            return
        path = m.group(1)
        tpl = _template_for(tvid, path, seg_url=seg_url)
        if not tpl:
            self.send_error(503)
            return
        url = construct_seg_url(tpl, seg_url)
    rr = rq.get(url, headers=headers, timeout=40)
    body = rr.content
    if rr.status_code in (401, 403, 405, 410):
        if direct:
            # 签名过期(约 600s): 重取 /dash, 按分片序号拿新地址, 重试一次
            log("seg dash", rr.status_code, "-> re-sign idx", idx, tvid)
            nu = iq_dash.seg_at(tvid, idx) if idx != "" else None
            if nu and nu != url:
                try:
                    rr = rq.get(nu, headers=headers, timeout=40)
                    body = rr.content
                except Exception:
                    pass
            if rr.status_code not in (200, 206):
                try:
                    self.send_error(503)
                except Exception:
                    pass
                return
        else:
            # 签名/会话不匹配(不是"过期"): 先同模板重试一次, 再让会话补签该文件
            log("seg CDN", rr.status_code, "retry1", tvid)
            time.sleep(1.0)
            rr = rq.get(url, headers=headers, timeout=40)
            body = rr.content
            if rr.status_code in (401, 403, 405):
                log("seg CDN", rr.status_code, "-> want", tvid)
                try:
                    with open(os.path.join(BASE, f"want_{tvid}"), "w") as f:
                        f.write(path)
                except Exception:
                    pass
                # 等会话补签出新模板, 再用新模板重试一次(避免播放器反复 503)
                for _ in range(6):
                    time.sleep(1)
                    nt = _template_for(tvid, path, wait=0)
                    if nt and nt != tpl:
                        try:
                            rr = rq.get(construct_seg_url(nt, seg_url), headers=headers, timeout=40)
                            body = rr.content
                        except Exception:
                            continue
                        if rr.status_code in (200, 206):
                            break
                if rr.status_code in (401, 403, 405):
                    try:
                        self.send_error(503)
                    except Exception:
                        pass
                    return
    if rr.status_code not in (200, 206):
        log("seg CDN status", rr.status_code, tvid, url.split("/")[-1][:40])
    ctype = rr.headers.get("Content-Type", "")
    if (not ctype) or ("octet-stream" in ctype):
        ctype = "video/mp2t"

    if rng and rr.status_code == 200:
        # 上游忽略 Range -> 自己按区间切片, 返回 206
        mm = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if mm:
            total = len(body)
            s0 = int(mm.group(1) or 0)
            s1 = int(mm.group(2)) if mm.group(2) else total - 1
            s1 = min(s1, total - 1)
            if s0 >= total or s0 > s1:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            blob = body[s0:s1 + 1]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Content-Range", f"bytes {s0}-{s1}/{total}")
            self.send_header("Accept-Ranges", "bytes")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(blob)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

    self.send_response(rr.status_code)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Accept-Ranges", "bytes")
    if rr.headers.get("Content-Range"):
        self.send_header("Content-Range", rr.headers["Content-Range"])
    self._cors()
    self.end_headers()
    try:
        self.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass

# 首段预取缓存：爱奇艺 CDN 冷连接偶尔要 30s，播放器等首个分片会以为卡死。
# 在详情页预热/打开清单时后台抓前 3 段，播放时直接命中内存，做到秒开。
_SEG_CACHE = {}
_SEG_LOCK = threading.Lock()
_SEG_TTL = 600
_SEG_MAX = 40
_SEG_PREFETCHING = set()


def _seg_cache_get(tvid, idx, url):
    with _SEG_LOCK:
        e = _SEG_CACHE.get((str(tvid), str(idx)))
    if not e:
        return None
    if time.time() - e["ts"] > _SEG_TTL or e["url"] != url:
        return None
    return e


def _seg_cache_put(tvid, idx, url, body, ctype):
    with _SEG_LOCK:
        _SEG_CACHE[(str(tvid), str(idx))] = {
            "url": url, "body": body, "ctype": ctype, "ts": time.time()}
        if len(_SEG_CACHE) > _SEG_MAX:
            for k, _v in sorted(_SEG_CACHE.items(),
                                key=lambda kv: kv[1].get("ts") or 0
                                )[:len(_SEG_CACHE) - _SEG_MAX]:
                _SEG_CACHE.pop(k, None)


def prewarm_segments(tvid, d, n=3, timeout=20):
    """后台抓整集前 n 段；失败不影响播放。"""
    import requests as rq
    lines = [l.strip() for l in (d.get("m3u8") or "").splitlines()
             if l.strip().startswith("http") and ".ts?" in l]
    if not lines:
        return

    def one(i, u):
        key = (str(tvid), str(i))
        with _SEG_LOCK:
            if key in _SEG_PREFETCHING:
                return
            _SEG_PREFETCHING.add(key)
        try:
            old = _seg_cache_get(tvid, i, u)
            if old:
                return
            rr = rq.get(u, headers={"User-Agent": iq.h["User-Agent"],
                                    "Referer": "https://www.iqiyi.com/"},
                        timeout=timeout)
            if rr.status_code in (200, 206) and rr.content:
                _seg_cache_put(tvid, i, u, rr.content,
                               rr.headers.get("Content-Type") or "video/mp2t")
                log("seg prewarm ok", tvid, i, len(rr.content))
        except Exception as e:
            log("seg prewarm fail", tvid, i, str(e)[:80])
        finally:
            with _SEG_LOCK:
                _SEG_PREFETCHING.discard(key)

    for i, u in enumerate(lines[:n]):
        threading.Thread(target=one, args=(i, u), daemon=True).start()


def serve_segment(self, tvid):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    idx = (q.get("i") or [""])[0]
    seg = ""
    b = (q.get("b") or [""])[0]
    if b:
        try:
            seg = _unb64(b)
        except Exception:
            seg = ""
    if not seg:
        # 兼容旧的 u= 形式
        seg = urllib.parse.unquote((q.get("u") or [""])[0])
        if seg.startswith("http") and "?" in seg and "qd_sc=" not in seg:
            # 插件对 u= 做了二次 unquote, 嵌套 URL 的 & 被拆成独立参数;
            # 这里把剩余参数按原顺序拼回去(签名可能已失效, 失败会走重签)
            extra = []
            for k, vs in q.items():
                if k in ("t", "i", "u", "b"):
                    continue
                for v in vs:
                    extra.append((k, v))
            if extra:
                seg = seg + "&" + urllib.parse.urlencode(extra)
    if idx != "" and not self.headers.get("Range"):
        hit = _seg_cache_get(tvid, idx, seg)
        # 预取正在抓同一段时，等它完成，避免播放器再发一次一模一样的 CDN 请求
        if not hit:
            for _ in range(100):
                with _SEG_LOCK:
                    busy = (str(tvid), str(idx)) in _SEG_PREFETCHING
                if not busy:
                    break
                time.sleep(0.2)
                hit = _seg_cache_get(tvid, idx, seg)
                if hit:
                    break
        if hit:
            try:
                self.send_response(200)
                self.send_header("Content-Type", hit["ctype"] or "video/mp2t")
                self.send_header("Content-Length", str(len(hit["body"])))
                self.send_header("Accept-Ranges", "bytes")
                self._cors()
                self.end_headers()
                self.wfile.write(hit["body"])
                _last_seg[tvid] = time.time()
                return
            except Exception:
                pass
    if iq_dash.is_dash_url(seg):
        _last_seg[tvid] = time.time()
        try:
            return _fetch_seg(self, seg, tvid, idx)
        except Exception as e:
            log("seg proxy fail(dash):", tvid, str(e)[:100])
            try:
                self.send_error(502)
            except Exception:
                pass
            return
    # 诊断: 没被识别成 /dash 直连地址时, 记录真实内容(排查插件改写/双重编码)
    log("seg not-dash:", tvid, "i=", idx, repr(seg[:200]))
    try:
        _host = (urllib.parse.urlparse(seg).hostname or "").lower()
    except Exception:
        _host = ""
    if _host != "data.video.iqiyi.com":
        return self.send_error(400)
    _last_seg[tvid] = time.time()
    try:
        return _fetch_seg(self, seg, tvid)
    except Exception as e:
        log("seg proxy fail:", tvid, str(e)[:100])
        try:
            self.send_error(502)
        except Exception:
            pass



def prewarm_first_episode(mac):
    """进入详情页时后台预热第一集(/dash 直连, 无浏览器)"""
    m = re.search(r'/p/(\d+)\.m3u8', mac.get("vod_play_url") or "")
    if not m:
        return
    tvid = m.group(1)
    log("prewarm:", tvid)
    d = iq_dash.get(tvid)
    if d:
        try:
            prewarm_segments(tvid, d, 3)
        except Exception:
            pass

def history_prewarm():
    """按观看历史预热最近看过的爱奇艺集(让"继续观看"秒开)"""
    try:
        h = json.load(open("/usr/local/trim_media_mitm/watch_history.json"))
        reg = json.load(open("/usr/local/trim_media_mitm/online_registry.json"))
    except Exception:
        return
    items = sorted(h.items(), key=lambda kv: -(kv[1].get("at", 0) if isinstance(kv[1], dict) else 0))
    n = 0
    for _k, v in items:
        if n >= 1:
            break
        g = (v or {}).get("g")
        r = reg.get(g) if g else None
        if not r or r.get("site") != "爱奇艺":
            continue
        eps = r.get("eps") or []
        title = (v or {}).get("title") or ""
        # 优先找与观看历史标题同名的那一集(多集条目)
        pick = None
        for e in eps:
            if title and (e.get("name") or "") == title:
                pick = e
                break
        if pick is None and eps:
            pick = eps[0]
        if not pick:
            continue
        m = re.search(r"/p/(\d+)\.m3u8", pick.get("url", "") or "")
        if not m:
            continue
        tvid = m.group(1)
        try:
            pos = float((v or {}).get("ts") or 0)
        except Exception:
            pos = 0.0
        start = max(0.0, pos - 15) if pos > 60 else 0.0
        log("history prewarm:", tvid, title, "start=", int(start))
        # /dash 直连整集清单, 客户端 seek 即可; 预热只为把签名与字幕缓存好
        iq_dash.get(tvid)
        n += 1
        break

def history_watcher():
    while True:
        try:
            history_prewarm()
        except Exception:
            pass
        time.sleep(45)

def prewarm_mac(mac):
    """后台预热一个条目的第一集(/dash 直连)"""
    m = re.search(r'/p/(\d+)\.m3u8', mac.get("vod_play_url") or "")
    if not m:
        return
    tvid = m.group(1)
    log("prewarm:", tvid)
    d = iq_dash.get(tvid)
    if d:
        try:
            prewarm_segments(tvid, d, 3)
        except Exception:
            pass

def next_ep_tvid(tvid):
    with _cache_lock:
        for k, e in list(_cache.items()):
            if not k.startswith("album:"):
                continue
            rt = [t for t, _ in (e["v"].get("_eps_runtime") or [])]
            if tvid in rt:
                i = rt.index(tvid)
                if i + 1 < len(rt):
                    return rt[i + 1]
    return None

def _pipeline_next(tvid):
    """预热下一集(边看边备)"""
    try:
        nt = next_ep_tvid(tvid)
        if not nt:
            return
        log("pipeline prewarm:", nt)
        iq_dash.get(nt)
    except Exception:
        pass

def find_page_url(tvid):
    """在 album 缓存里找该集的 pageUrl(搜索接口会带每个分集的 pageUrl)"""
    with _cache_lock:
        for k, e in list(_cache.items()):
            if not k.startswith("album:"):
                continue
            if time.time() - e["t"] > 24 * 3600:
                continue
            for tv, url in (e["v"].get("_eps_runtime") or []):
                if tv == tvid:
                    return url
            # 若查询的是专辑第一集(playQipuId), 用 ep1 pageUrl
            if k.endswith(tvid):
                return url
    return None

if __name__ == "__main__":
    threading.Thread(target=flusher, daemon=True).start()
    threading.Thread(target=signer_killer, daemon=True).start()
    threading.Thread(target=history_watcher, daemon=True).start()
    addr = (CONF.get("listen", "127.0.0.1"), int(CONF.get("port", 8770)))
    srv = ThreadingHTTPServer(addr, Handler)
    print(f"iqiyi-bridge listening on :{addr[1]}  base={CONF['base']}")
    srv.serve_forever()
