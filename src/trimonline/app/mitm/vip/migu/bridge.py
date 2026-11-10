#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""咪咕视频采集桥: 苹果CMS V10 采集接口 + m3u8/ts 代理。

架构参考爱奇艺源 (iqiyi_cms/bridge.py)。

核心 (经彻底逆向验证):
  - 搜索: POST /search/v3/open-search/ -> 专集条目 (pID)
  - 分集: GET /program/v1/cont/episode-infos?epsIDS=<专集pID> -> 所有分集
  - 播放: 咪咕 PC Web 端使用 /playurl/secret/v2/play/playurl 加密接口,
          普通 HTTP 无法直接构造, 用无头浏览器 (migu_worker.py) 打开
          /p/detail/<单集cid> 捕获播放器实际请求的 m3u8 URL。
  - 代理: 拿到 m3u8 URL 后, 用 requests 拉取并解析 (多级跳转),
          把相对 ts 路径改写为桥代理 /s/seg.ts, 转发给飞牛影视。

接口:
  GET /api.php/provide/vod/?ac=videolist&wd=关键词   搜索
  GET /api.php/provide/vod/?ac=detail&ids=<专集cid>   详情(全部分集 vod_play_url)
  GET /p/<单集cid>.m3u8                                代理 m3u8 (ts 指向 /s/seg.ts)
  GET /s/seg.ts?u=<b64>                                代理 ts 分片
  GET /health                                          健康检查
"""
import base64, json, os, re, subprocess, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import migu_api
import migu_live

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
    "port": int(_main.get("vip_migu_port") or 8771),
    "base": "",
    "storage_state": _main.get("vip_migu_state")
                     or os.path.join(CONF_DIR, "migu_state.json"),
    "m3u8_ttl": 900,
    "m3u8_fail_ttl": 600,
    "detail_fail_ttl": 600,
    "worker_timeout": 45,
}
CONF["base"] = "http://127.0.0.1:%d" % CONF["port"]

migu = migu_api.Migu(state_path=CONF["storage_state"])

# ============ 缓存 ============
_cache = {}
_cache_lock = threading.Lock()
_cache_dirty = False

# 失败负缓存：内容下架/抓不到 m3u8 时，短时间内不要反复启动 Playwright。
M3U8_FAIL_TTL = int(CONF.get("m3u8_fail_ttl", 600))
DETAIL_FAIL_TTL = int(CONF.get("detail_fail_ttl", 600))
WORKER_TIMEOUT = int(CONF.get("worker_timeout", 45))

CACHE_DB = os.path.join(DATA_DIR, "migu_cache.json")
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
    global _cache_dirty
    with _cache_lock:
        _cache[k] = {"t": time.time(), "v": v}
        _cache_dirty = True

def cache_del(k):
    global _cache_dirty
    with _cache_lock:
        if _cache.pop(k, None) is not None:
            _cache_dirty = True

def cache_flush():
    global _cache_dirty
    with _cache_lock:
        if _cache_dirty:
            try:
                json.dump({"entries": _cache}, open(CACHE_DB, "w"))
            except Exception:
                pass
            _cache_dirty = False

def flusher():
    while True:
        cache_flush()
        time.sleep(30)

# ============ 苹果CMS 组装 ============
CATEGORIES = {"电影": "电影", "电视剧": "连续剧", "综艺": "综艺", "动漫": "动漫",
              "纪录片": "纪录片", "少儿": "少儿", "体育": "体育", "音乐": "音乐"}

def _base():
    return CONF["base"]

def vod_from_item(n):
    cid = n["cid"]
    title = n["title"]
    sub = n.get("update_ep") or n["sub"] or (f"共{n.get('eps_count', 0)}集" if n.get("eps_count") else "")
    if not sub and n.get("is_long"):
        sub = "连续剧"
    score = (n.get("score") or "").strip()
    if score and score not in sub:
        sub = f"{score} {sub}".strip()
    content = n.get("desc") or ""
    style = n.get("content_style") or ""
    if style:
        content = (content + f"\n类型: {style}").strip()
    return {
        "vod_id": cid,
        "vod_name": title,
        "vod_en": cid,
        "type_name": CATEGORIES.get(n["type_name"], n["type_name"] or "正片"),
        "vod_pic": n["pic"],
        "vod_remarks": sub,
        "vod_year": n["year"],
        "vod_area": n["area"],
        "vod_content": content[:500],
        "vod_actor": n["actor"],
        "vod_director": n["director"],
        "vod_play_from": "migu",
        "vod_play_url": "",  # 留空触发客户端调 ac=detail
    }

def mac_list(items, total=0, pg=1):
    return {"code": 1, "msg": "数据列表", "page": pg, "pagecount": 1,
            "limit": str(len(items)), "total": total or len(items), "list": items}

def detail_mac(album_cid):
    base = _base()
    fail_key = f"detailfail:{album_cid}"
    if cache_get(fail_key, DETAIL_FAIL_TTL):
        log("detail negative cache", album_cid)
        return None

    # 优先用 PC 详情页 __CONTENT_INFO__ (含刮削信息 + 全部分集, 无需登录)
    data = None
    try:
        data = migu.content_info(album_cid)
    except Exception as e:
        log("content_info fail", album_cid, repr(e)[:120])

    if data:
        name = (data.get("name") or "").strip() or album_cid
        pic = migu_api.pick_pic(data)
        content = data.get("detail") or ""
        actor = data.get("actor") or ""
        director = data.get("director") or ""
        year = data.get("year") or ""
        area = data.get("area") or ""
        score = (data.get("score") or "").strip()
        update_ep = data.get("updateEP") or ""
        dn = data.get("displayName") or {}
        type_name = (dn.get("first") if isinstance(dn, dict) else "") or "电视剧"
        style = data.get("contentStyle") or ""
        if style:
            content = (content + f"\n类型: {style}").strip()
        datas = data.get("datas") or []
        parts = []
        for d in datas:
            pid = d.get("pID") or ""
            dname = d.get("name") or "正片"
            if pid:
                parts.append(f"{dname}${base}/p/{pid}.m3u8")
        if not parts:
            parts.append(f"{name}${base}/p/{album_cid}.m3u8")
        play_url = "#".join(parts)
        remarks = update_ep or (f"共{len(datas)}集" if datas else "")
        if score:
            remarks = f"{score} {remarks}".strip()
        cache_del(fail_key)
        return {
            "vod_id": album_cid,
            "vod_name": name,
            "vod_en": album_cid,
            "type_name": CATEGORIES.get(type_name, type_name),
            "vod_pic": pic,
            "vod_remarks": remarks,
            "vod_year": year,
            "vod_area": area,
            "vod_content": content[:1000],
            "vod_actor": actor,
            "vod_director": director,
            "vod_play_from": "migu",
            "vod_play_url": play_url,
        }

    # 兜底: episode-infos 接口 (无刮削信息)
    eps, eps_count, update_ep = [], 0, ""
    try:
        j = migu.episode_infos(album_cid)
        eps, eps_count, update_ep = migu_api.Migu._parse_episodes(j)
    except Exception as e:
        log("episode_infos fail", album_cid, repr(e)[:120])

    parts = []
    if eps:
        for e in eps:
            ecid = e.get("cid") or ""
            ename = e.get("name") or "正片"
            if ecid:
                parts.append(f"{ename}${base}/p/{ecid}.m3u8")

    # 两条详情链路都失败时，绝不能返回 title=CID 的假条目。
    # 那会让播放器以为内容存在，实际只会等 90 秒后报“播放失败”。
    if not parts:
        log("detail unavailable", album_cid)
        cache_put(fail_key, 1)
        return None

    play_url = "#".join(parts)
    sub = update_ep or (f"共{eps_count}集" if eps_count else "")
    cache_del(fail_key)
    return {
        "vod_id": album_cid,
        "vod_name": album_cid,
        "vod_en": album_cid,
        "type_name": "连续剧",
        "vod_pic": "",
        "vod_remarks": sub,
        "vod_year": "",
        "vod_area": "",
        "vod_content": "",
        "vod_actor": "",
        "vod_director": "",
        "vod_play_from": "migu",
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
    ck = migu.h.get("Cookie", "")
    h = {
        "User-Agent": migu_api.UA,
        "Referer": "https://www.miguvideo.com/",
        "Origin": "https://www.miguvideo.com",
    }
    if ck:
        h["Cookie"] = ck
    return h

# ============ m3u8 URL 抓取 (Playwright worker) ============
_worker_lock = threading.Lock()
_running = set()

def _worker_out(cid):
    return os.path.join(DATA_DIR, f"work_{cid}.json")

def _get_m3u8_url(cid, force=False):
    """获取单集的 m3u8 URL (worker 抓取 + 缓存)。

    若已有同 cid 的 worker 在跑, 等待其输出文件, 不重复启动。
    """
    fail_key = f"m3u8fail:{cid}"
    if not force:
        cached = cache_get(f"m3u8url:{cid}", 180)
        if cached:
            return cached
        if cache_get(fail_key, M3U8_FAIL_TTL):
            log("m3u8 negative cache", cid)
            return None
    out = _worker_out(cid)
    with _worker_lock:
        already = cid in _running
        if not already:
            _running.add(cid)
    log("start worker" if not already else "wait worker", cid)
    t0 = time.time()
    if not already:
        try:
            try:
                os.remove(out)
            except OSError:
                pass
            env = dict(os.environ)
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-playwright")
            proc = subprocess.Popen(
                ["python3", os.path.join(BASE, "migu_worker.py"), str(cid), out,
                 CONF["storage_state"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env)
            deadline = t0 + WORKER_TIMEOUT
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
                _running.discard(cid)
    else:
        # 等待其它线程启动的 worker 完成
        deadline = t0 + WORKER_TIMEOUT + 2
        while time.time() < deadline:
            if os.path.exists(out):
                break
            time.sleep(0.4)

    if not os.path.exists(out):
        log("worker no output", cid, round(time.time() - t0, 1))
        cache_put(fail_key, 1)
        return None
    try:
        j = json.load(open(out))
        url = j.get("m3u8") or ""
    except Exception:
        url = ""
    if url:
        cache_put(f"m3u8url:{cid}", url)
        cache_del(fail_key)
        log("worker ok", cid, round(time.time() - t0, 1), "s")
        return url
    log("worker empty", cid)
    cache_put(fail_key, 1)
    return None

# ============ m3u8 解析 (多级跳转 + 相对 ts) ============
def _resolve_playlist(url):
    """跟随 m3u8 单行跳转, 返回 (playlist_text, base_url)。"""
    import requests
    cur = url
    for _ in range(4):
        r = requests.get(cur, headers=_headers(), timeout=20, allow_redirects=True)
        if r.status_code != 200:
            log("m3u8 resolve status", r.status_code)
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
    """把 ts 行 (相对或绝对) 改写为桥代理 /s/seg.ts?u=<b64>。"""
    # base 目录 (去掉文件名)
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    out = []
    for line in playlist.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and ".ts" in s:
            if s.startswith("http"):
                abs_url = s
            else:
                abs_url = base_dir + s
            out.append(f"{bridge_base}/s/seg.ts?u={_b64(abs_url)}")
        else:
            out.append(line)
    return "\n".join(out)

# ============ HTTP 服务 ============
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

    def do_live_cms(self, q):
        """把咪咕电视直播频道伪装成苹果CMS采集源，供爱迷茫搜索/播放。"""
        try:
            chans = migu_live.fetch_channels()[0]
        except Exception as e:
            log("live cms fail", repr(e)[:120])
            chans = []
        ac = (q.get("ac") or ["videolist"])[0]
        wd = (q.get("wd") or [""])[0]
        try:
            wd = wd.encode("latin-1").decode("utf-8")
        except Exception:
            pass
        ids = (q.get("ids") or [""])[0]
        if ac == "detail":
            chans = [c for c in chans if str(c.get("pid")) == str(ids)]
        elif wd and wd not in ("直播", "电视", "直播电视", "全部"):
            key = wd.lower()
            chans = [c for c in chans
                     if key in (c.get("name") or "").lower()
                     or key in (c.get("cat") or "").lower()]
        out = []
        for c in chans:
            pid = str(c.get("pid") or "")
            name = (c.get("name") or "").strip()
            if not pid or not name:
                continue
            out.append({
                "vod_id": pid,
                "vod_name": name,
                "vod_en": pid,
                "type_name": "直播",
                "vod_pic": c.get("logo") or "",
                "vod_remarks": c.get("cat") or "直播",
                "vod_year": "",
                "vod_area": "",
                "vod_content": "咪咕电视直播（需网络可用；部分频道受版权/会员限制）",
                "vod_actor": "",
                "vod_director": "",
                "vod_play_from": "migu_live",
                "vod_play_url": name + "$" + _base() + "/live/" + pid + ".m3u8",
            })
        return self._json(mac_list(out, total=len(out)))

    def do_detail(self, ids):
        idset = [x for x in ids.split(",") if x]
        macs = []
        for cid in idset:
            try:
                mac = detail_mac(cid)
                if mac:
                    macs.append(mac)
            except Exception as e:
                log("detail fail", cid, repr(e)[:120])
        # 后台预热第一集
        if macs:
            m = re.search(r"/p/(\d+)\.m3u8", macs[0].get("vod_play_url") or "")
            if m:
                threading.Thread(target=_get_m3u8_url, args=(m.group(1),), daemon=True).start()
        return self._json(mac_list(macs))

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
            if path.startswith("/live.php/provide/vod"):
                return self.do_live_cms(q)
            if path.startswith("/api.php/provide/vod"):
                ac = (q.get("ac") or ["videolist"])[0]
                if ac == "detail":
                    ids = (q.get("ids") or [""])[0]
                    return self.do_detail(ids)
                wd = (q.get("wd") or [""]).pop()
                if not wd:
                    return self._json(mac_list([]))
                try:
                    wd = wd.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
                albums = migu.native_albums(wd)
                macs = [vod_from_item(a) for a in albums]
                return self._json(mac_list(macs))
            if path.startswith("/api.php/provide/vod/list"):
                return self._json(mac_list([]))
            if path.startswith("/p/"):
                cid = path.rsplit("/", 1)[-1].split(".")[0]
                return self.serve_playlist(cid)
            if path == "/live/list":
                try:
                    return self._json(migu_live.live_list())
                except Exception as e:
                    log("live list fail", repr(e)[:120])
                    return self._json({"categories": [], "channels": [], "total": 0})
            if path.startswith("/live/") and path.endswith(".m3u8"):
                pid = path[len("/live/"):-len(".m3u8")]
                return self.serve_live(pid)
            if path.startswith("/s/"):
                return self.serve_segment(
                    q.get("u", [""])[0],
                    live=(q.get("live", [""])[0] == "1"))
            if path == "/health":
                state = {"cookies": bool(migu.ck),
                         "has_mgnd_session": "mgnd_session_id" in migu.ck,
                         "logged": bool(migu.ck.get("MIGU_UID")),
                         "uid": migu.ck.get("MIGU_UID", "")}
                return self._json({"ok": 1, "service": "migu-bridge", "login": state})
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            try:
                self._json({"code": 0, "msg": f"bridge error: {traceback.format_exc()[:800]}"}, 500)
            except Exception:
                pass

    def serve_playlist(self, cid):
        # 缓存已改写的播放列表 (60s)
        cached = cache_get(f"playlist:{cid}", 60)
        if cached:
            return self._m3u8(cached)
        m3u8_url = _get_m3u8_url(cid)
        if not m3u8_url:
            log("m3u8 url unavailable", cid)
            # 404 是“非瞬时错误”：MITM 不会为它重试 3 次 × 30 秒。
            return self.send_error(404)
        try:
            playlist, base_url = _resolve_playlist(m3u8_url)
        except Exception as e:
            log("resolve fail", cid, repr(e)[:120])
            return self.send_error(502)
        if not playlist:
            log("playlist empty", cid)
            return self.send_error(502)
        rewritten = _rewrite_playlist(playlist, base_url, _base())
        cache_put(f"playlist:{cid}", rewritten)
        log("m3u8 ok", cid, "segs=", rewritten.count("/s/seg.ts"))
        return self._m3u8(rewritten)

    def serve_live(self, pid):
        # 直播清单更新快，只缓存 3 秒
        cached = cache_get(f"live:{pid}", 3)
        if cached:
            return self._m3u8(cached)
        try:
            text = migu_live.live_playlist(pid, _base())
        except Exception:
            # 签名 URL 过期时清缓存重试一次
            try:
                migu_live._url_cache.pop(pid, None)
                text = migu_live.live_playlist(pid, _base())
            except Exception as e:
                log("live fail", pid, repr(e)[:120])
                return self.send_error(502)
        cache_put(f"live:{pid}", text)
        return self._m3u8(text)

    def serve_segment(self, u64, live=False):
        url = ""
        try:
            url = _unb64(u64)
        except Exception:
            url = ""
        if not url.startswith("http"):
            return self.send_error(400)
        if live:
            log("live seg:", url[:100])
        import requests
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
        except Exception as e:
            log("ts fail", repr(e)[:120])
            return self.send_error(502)
        if r.status_code not in (200, 206):
            log("ts status", r.status_code)
            return self.send_error(502)
        ct = r.headers.get("Content-Type", "video/mp2t")
        if "octet-stream" in ct:
            ct = "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(r.content)))
        self.send_header("Accept-Ranges", "bytes")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(r.content)
        except BrokenPipeError:
            pass

if __name__ == "__main__":
    threading.Thread(target=flusher, daemon=True).start()
    addr = (CONF.get("listen", "127.0.0.1"), int(CONF.get("port", 8771)))
    srv = ThreadingHTTPServer(addr, Handler)
    print(f"migu-bridge listening on :{addr[1]}  base={CONF['base']}")
    srv.serve_forever()
