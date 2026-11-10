#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芒果TV 采集桥: 苹果CMS V10 采集接口 + m3u8/ts 代理。

架构参考爱奇艺/咪咕:
  - 搜索: Playwright worker 打开 so.mgtv.com 抓 pc/search/v2 JSON,
          解析芒果自有(source=imgo)专辑与分集。
  - 详情: 优先用搜索缓存; 缺失时用 variety/showlist 补分集。
  - 播放: Playwright worker 打开视频页抓 m3u8 URL,
          桥用 requests 拉取 m3u8/ts 并代理 (ts 相对路径改写为 /s/seg.ts)。

接口:
  GET /api.php/provide/vod/?ac=videolist&wd=关键词
  GET /api.php/provide/vod/?ac=detail&ids=<专辑cid>
  GET /p/<单集vid>.m3u8
  GET /s/seg.ts?u=<b64>
  GET /health
"""
import base64, json, os, re, subprocess, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mgtv_api

BASE = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.environ.get("AIMIMANG_CONF", "/usr/local/trim_media_mitm/conf")
DATA_DIR = os.environ.get("AIMIMANG_DATA", "/usr/local/trim_media_mitm/vip_data")
os.makedirs(DATA_DIR, exist_ok=True)
try:
    _main = json.load(open(os.path.join(CONF_DIR, "config.json")))
except Exception:
    _main = {}
CONF = {
    "listen": "127.0.0.1",
    "port": int(_main.get("vip_mgtv_port") or 8772),
    "base": "",
    "storage_state": _main.get("vip_mgtv_state")
                     or os.path.join(CONF_DIR, "mgtv_state.json"),
}
CONF["base"] = "http://127.0.0.1:%d" % CONF["port"]

mgtv = mgtv_api.Mgtv(state_path=CONF["storage_state"])

# ============ 缓存 ============
_cache = {}
_cache_lock = threading.Lock()

CACHE_DB = os.path.join(DATA_DIR, "mgtv_cache.json")
if os.path.exists(CACHE_DB):
    try:
        _cache = json.load(open(CACHE_DB)).get("entries", {})
    except Exception:
        _cache = {}

def cache_get(k, ttl):
    with _cache_lock:
        e = _cache.get(k)
        if e and time.time() - e["t"] < ttl:
            return e["v"]
        return None

def cache_put(k, v):
    with _cache_lock:
        _cache[k] = {"t": time.time(), "v": v}
        try:
            json.dump({"entries": _cache}, open(CACHE_DB, "w"))
        except Exception:
            pass

# ============ worker ============
_worker_lock = threading.Lock()
_running = set()

def _worker_out(kind, key):
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(key))[:80]
    return os.path.join(DATA_DIR, f"work_{kind}_{safe}.json")

def _run_worker(mode, arg, out, timeout=45):
    with _worker_lock:
        tag = (mode, str(arg))
        if tag in _running:
            already = True
        else:
            already = False
            _running.add(tag)
    if not already:
        try:
            try:
                os.remove(out)
            except OSError:
                pass
            env = dict(os.environ)
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-playwright")
            proc = subprocess.Popen(
                ["python3", os.path.join(BASE, "mgtv_worker.py"), mode, str(arg),
                 out, CONF["storage_state"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if os.path.exists(out):
                    break
                if proc.poll() is not None and not os.path.exists(out):
                    break
                time.sleep(0.4)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        finally:
            with _worker_lock:
                _running.discard(tag)
    else:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(out):
                break
            time.sleep(0.4)
    if not os.path.exists(out):
        return None
    try:
        return json.load(open(out))
    except Exception:
        return None

def _page_url_from_vid(vid):
    """page_url 缓存丢失时，用 video/info 的 seriesId 重新拼播放页。"""
    info = mgtv.video_info(vid) or {}
    series = ((info.get("info") or {}).get("seriesId")
              or (info.get("clip") or {}).get("seriesId"))
    if series:
        return f"https://www.mgtv.com/b/{series}/{vid}.html"
    return ""

# ============ 搜索 ============
def _remember_album(a):
    cache_put(f"album:{a['cid']}", a)
    for e in a.get("episodes") or []:
        if e.get("vid") and e.get("page_url"):
            cache_put(f"page:{e['vid']}", e["page_url"])

def search_albums(kw):
    key = re.sub(r"\s+", "", kw)
    cached = cache_get(f"search:{key}", 600)
    if cached is not None:
        return cached
    albums = []
    raw = None
    # 首选: 纯 HTTP 签名搜索 (~250ms)
    try:
        raw = mgtv.search_http(key)
        if raw:
            albums = mgtv.parse_search(raw)
    except Exception as e:
        log("search_http fail", repr(e)[:120])
    # 仅当 HTTP 搜索本身失败时, 才用无头浏览器兜底
    if raw is None:
        out = _worker_out("search", key)
        res = _run_worker("search", key, out, timeout=45)
        if res and res.get("ok") and res.get("json"):
            try:
                albums = mgtv.parse_search(res["json"])
            except Exception as e:
                log("parse_search fail", repr(e)[:120])
    if albums:
        for a in albums:
            _remember_album(a)
        cache_put(f"search:{key}", albums)
    return albums

# ============ 苹果CMS ============
CATEGORIES = {"电视剧": "连续剧", "电影": "电影", "综艺": "综艺", "动漫": "动漫",
              "纪录片": "纪录片", "少儿": "少儿", "体育": "体育", "音乐": "音乐"}

def _base():
    return CONF["base"]

def vod_from_album(a):
    cid = a["cid"]
    return {
        "vod_id": cid,
        "vod_name": a.get("title") or cid,
        "vod_en": cid,
        "type_name": CATEGORIES.get(a.get("type_name"), a.get("type_name") or "正片"),
        "vod_pic": a.get("pic") or "",
        "vod_remarks": (f"共{a.get('episode_count', 0)}集"
                        if a.get("episode_count") else ""),
        "vod_year": a.get("year") or "",
        "vod_area": "",
        "vod_content": (a.get("story") or "")[:500],
        "vod_actor": "",
        "vod_director": "",
        "vod_play_from": "mgtv",
        "vod_play_url": "",  # 留空触发 detail
    }

def mac_list(items, total=0, pg=1):
    return {"code": 1, "msg": "数据列表", "page": pg, "pagecount": 1,
            "limit": str(len(items)), "total": total or len(items), "list": items}

def detail_mac(album_cid):
    a = cache_get(f"album:{album_cid}", 24 * 3600)
    if not a:
        # 缓存缺失: 用 showlist 补
        eps = mgtv.episodes_from_showlist(album_cid)
        info = mgtv.video_info((eps[0]["vid"] if eps else "")) or {}
        ii = info.get("info") or {}
        a = {
            "cid": album_cid,
            "title": ii.get("title") or album_cid,
            "pic": ii.get("pic") or "",
            "story": ii.get("desc") or "",
            "year": "",
            "type_name": "",
            "episodes": eps,
            "episode_count": len(eps),
        }
        _remember_album(a)
    eps = a.get("episodes") or []
    base = _base()
    parts = []
    for i, e in enumerate(eps):
        vid = e.get("vid") or ""
        name = e.get("title") or (f"第{i+1}集" if len(eps) > 1 else "正片")
        name = name.replace("$", " ").replace("#", " ")
        if vid:
            parts.append(f"{name}${base}/p/{vid}.m3u8")
    play_url = "#".join(parts)
    if not play_url:
        play_url = f"正片${base}/p/{album_cid}.m3u8"
    return {
        "vod_id": album_cid,
        "vod_name": a.get("title") or album_cid,
        "vod_en": album_cid,
        "type_name": CATEGORIES.get(a.get("type_name"), a.get("type_name") or "连续剧"),
        "vod_pic": a.get("pic") or "",
        "vod_remarks": (f"共{a.get('episode_count', 0)}集"
                        if a.get("episode_count") else ""),
        "vod_year": a.get("year") or "",
        "vod_area": "",
        "vod_content": (a.get("story") or "")[:1000],
        "vod_actor": "",
        "vod_director": "",
        "vod_play_from": "mgtv",
        "vod_play_url": play_url,
    }

# ============ 工具 ============
def _b64(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")

def _unb64(s):
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")

def _headers():
    mgtv.refresh()
    h = {"User-Agent": mgtv_api.UA, "Referer": "https://www.mgtv.com/"}
    if mgtv.ck:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in mgtv.ck.items())
    return h

def _resolve_playlist(url):
    import requests
    cur = url
    for _ in range(4):
        r = requests.get(cur, headers=_headers(), timeout=20, allow_redirects=True)
        if r.status_code != 200:
            log("m3u8 status", r.status_code)
            return None, None
        text = r.text.strip()
        if "#EXTM3U" in text:
            return text, cur
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if lines and lines[0].startswith("http"):
            cur = lines[0]
            continue
        return text, cur
    return None, None

def _rewrite_playlist(playlist, base_url, bridge_base):
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    out = []
    for line in playlist.splitlines():
        s = line.strip()
        # 去掉芒果私有的 #EXT-MGTV-* 标签。部分播放器看到未知标签
        # 会反复重试/不推进时间轴，表现为“一个分片卡 30 秒”。
        if s.startswith("#EXT-MGTV"):
            continue
        if s and not s.startswith("#") and (".ts" in s or ".m4s" in s):
            abs_url = s if s.startswith("http") else base_dir + s
            out.append(f"{bridge_base}/s/seg.ts?u={_b64(abs_url)}")
        else:
            out.append(line)
    if out and out[0].strip() == "#EXTM3U":
        if not any(x.strip().startswith("#EXT-X-PLAYLIST-TYPE") for x in out):
            out.insert(1, "#EXT-X-PLAYLIST-TYPE:VOD")
    return "\n".join(out)

# ============ HTTP ============
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass

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

    def _m3u8(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path
            if path.startswith("/api.php/provide/vod"):
                ac = (q.get("ac") or ["videolist"])[0]
                if ac == "detail":
                    ids = (q.get("ids") or [""])[0]
                    macs = []
                    for cid in [x for x in ids.split(",") if x]:
                        try:
                            macs.append(detail_mac(cid))
                        except Exception as e:
                            log("detail fail", cid, repr(e)[:120])
                    # 预热前 3 集（首播解析要开无头浏览器，约 7 秒）
                    for m in macs[:1]:
                        vids = re.findall(r"/p/(\d+)\.m3u8", m.get("vod_play_url") or "")
                        for vid0 in vids[:3]:
                            threading.Thread(target=_warm, args=(vid0,), daemon=True).start()
                    return self._json(mac_list(macs))
                wd = (q.get("wd") or [""]).pop()
                if not wd:
                    return self._json(mac_list([]))
                try:
                    wd = wd.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
                albums = search_albums(wd)
                macs = [vod_from_album(a) for a in albums]
                if macs:
                    m = re.search(r"/p/", macs[0].get("vod_play_url") or "")
                    # 预热第一条专辑首集
                    a0 = albums[0]
                    if a0.get("episodes"):
                        threading.Thread(target=_warm, args=(a0["episodes"][0]["vid"],), daemon=True).start()
                return self._json(mac_list(macs))
            if path.startswith("/api.php/provide/vod/list"):
                return self._json(mac_list([]))
            if path.startswith("/p/"):
                vid = path.rsplit("/", 1)[-1].split(".")[0]
                return self.serve_playlist(vid)
            if path.startswith("/s/"):
                return self.serve_segment(q.get("u", [""])[0])
            if path == "/health":
                try:
                    mgtv.refresh()
                except Exception:
                    pass
                ck = mgtv.ck or {}
                state = {
                    "cookies": bool(ck),
                    "uid": ck.get("UID") or ck.get("loginAccount") or ck.get("id", ""),
                    "logged": bool(ck.get("UID") or ck.get("loginAccount") or ck.get("id")),
                }
                return self._json({"ok": 1, "service": "mgtv-bridge", "login": state})
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            try:
                self._json({"code": 0, "msg": f"bridge error: {traceback.format_exc()[:800]}"}, 500)
            except Exception:
                pass

    def serve_playlist(self, vid):
        cached = cache_get(f"playlist:{vid}", 180)
        if cached:
            return self._m3u8(cached)
        m3u8_url = cache_get(f"m3u8:{vid}", 1800)
        if not m3u8_url:
            page_url = cache_get(f"page:{vid}", 24 * 3600)
            if not page_url:
                # 尝试从 album 缓存找
                with _cache_lock:
                    for k, e in list(_cache.items()):
                        if not k.startswith("album:"):
                            continue
                        for ep in (e.get("v") or {}).get("episodes") or []:
                            if ep.get("vid") == vid:
                                page_url = ep.get("page_url")
                                break
                        if page_url:
                            break
            if not page_url:
                # page_url 缓存丢失时，用 video/info 的 seriesId 重新拼播放页。
                # 这能救回大量“专辑缓存过期但内容还在”的条目。
                page_url = _page_url_from_vid(vid)
                if page_url:
                    cache_put(f"page:{vid}", page_url)
            if not page_url:
                log("no page_url for", vid)
                return self.send_error(404)
            out = _worker_out("play", vid)
            res = _run_worker("play", page_url, out, timeout=45)
            if not res or not res.get("ok") or not res.get("m3u8"):
                log("play worker fail", vid)
                return self.send_error(503)
            m3u8_url = res["m3u8"]
            cache_put(f"m3u8:{vid}", m3u8_url)
        try:
            playlist, base_url = _resolve_playlist(m3u8_url)
        except Exception as e:
            log("resolve fail", vid, repr(e)[:120])
            playlist, base_url = "", ""
        if not playlist:
            # 缓存的签名 URL 可能过期；清掉后重新开一次浏览器解析。
            log("playlist retry worker", vid)
            cache_del(f"m3u8:{vid}")
            cache_del(f"playlist:{vid}")
            page_url = cache_get(f"page:{vid}", 24 * 3600)
            out = _worker_out("play", vid)
            res = _run_worker("play", page_url or _page_url_from_vid(vid) or "", out, timeout=45)
            if not res or not res.get("ok") or not res.get("m3u8"):
                log("playlist retry fail", vid)
                return self.send_error(502)
            m3u8_url = res["m3u8"]
            cache_put(f"m3u8:{vid}", m3u8_url)
            try:
                playlist, base_url = _resolve_playlist(m3u8_url)
            except Exception as e:
                log("resolve retry fail", vid, repr(e)[:120])
                return self.send_error(502)
            if not playlist:
                return self.send_error(502)
        rewritten = _rewrite_playlist(playlist, base_url, _base())
        cache_put(f"playlist:{vid}", rewritten)
        log("m3u8 ok", vid, "segs=", rewritten.count("/s/seg.ts"))
        return self._m3u8(rewritten)

    def serve_segment(self, u64):
        url = ""
        try:
            url = _unb64(u64)
        except Exception:
            url = ""
        if not url.startswith("http"):
            return self.send_error(400)
        import requests
        hdr = _headers()
        rng = self.headers.get("Range")
        if rng:
            hdr["Range"] = rng
        try:
            r = requests.get(url, headers=hdr, timeout=30)
        except Exception as e:
            log("ts fail", repr(e)[:120])
            return self.send_error(502)
        if r.status_code not in (200, 206):
            log("ts status", r.status_code)
            return self.send_error(502)
        ct = r.headers.get("Content-Type", "video/mp2t")
        if "octet-stream" in ct:
            ct = "video/mp2t"
        body = r.content
        code = r.status_code
        crange = r.headers.get("Content-Range", "")
        if rng and code == 200:
            # 上游不支持 Range 时本地切片，仍给播放器一个标准的 206。
            import re as _re
            m = _re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                total = len(body)
                start = int(m.group(1) or 0)
                end = int(m.group(2) or total - 1)
                end = min(end, total - 1)
                if start < total:
                    body = body[start:end + 1]
                    code = 206
                    crange = "bytes %d-%d/%d" % (start, end, total)
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if crange:
            self.send_header("Content-Range", crange)
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

def _warm(vid):
    try:
        cached = cache_get(f"m3u8:{vid}", 1800)
        if cached:
            return
        page_url = cache_get(f"page:{vid}", 24 * 3600)
        if not page_url:
            return
        out = _worker_out("play", vid)
        res = _run_worker("play", page_url, out, timeout=45)
        if res and res.get("ok") and res.get("m3u8"):
            cache_put(f"m3u8:{vid}", res["m3u8"])
            log("prewarmed", vid)
    except Exception:
        pass

if __name__ == "__main__":
    addr = (CONF.get("listen", "127.0.0.1"), int(CONF.get("port", 8772)))
    srv = ThreadingHTTPServer(addr, Handler)
    print(f"mgtv-bridge listening on :{addr[1]}  base={CONF['base']}")
    srv.serve_forever()
