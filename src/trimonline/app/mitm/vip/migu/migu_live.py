#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""咪咕电视直播：频道列表 + 直播 m3u8 解析（无需浏览器）。

频道接口:
  https://program-sc.miguvideo.com/live/v2/tv-data/1ff892f2b5ab4a79be6e25b69d2f5d05
  https://program-sc.miguvideo.com/live/v2/tv-data/<vomsID>   -> body.dataList

取流:
  https://play.miguvideo.com/playurl/v1/play/playurl?...  (Android 720p 签名)
  返回 urlInfo.url，再按前端算法补 ddCalcu，302 到最终 m3u8。
"""
import datetime
import hashlib
import os
import random
import re
import threading
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
CATS_URL = ("https://program-sc.miguvideo.com/live/v2/tv-data/"
            "1ff892f2b5ab4a79be6e25b69d2f5d05")
CAT_URL = "https://program-sc.miguvideo.com/live/v2/tv-data/%s"
PLAY_API = "https://play.miguvideo.com/playurl/v1/play/playurl"
APP_VERSION = "2600034600"

_lock = threading.Lock()
_chan_cache = {"ts": 0, "channels": [], "cats": []}
_url_cache = {}
_url_lock = threading.Lock()


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _b64(s):
    import base64
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _get(url, timeout=12):
    return requests.get(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.miguvideo.com/",
    }, timeout=timeout)


def fetch_channels(ttl=1800):
    """返回 (channels, categories)。channels 按名称全局去重，保留首次分类。"""
    with _lock:
        if _chan_cache["channels"] and time.time() - _chan_cache["ts"] < ttl:
            return list(_chan_cache["channels"]), list(_chan_cache["cats"])
    try:
        j = _get(CATS_URL).json()
        live = (j.get("body") or {}).get("liveList") or []
    except Exception:
        return list(_chan_cache["channels"]), list(_chan_cache["cats"])
    cats = [c for c in live if c.get("name") != "热门" and c.get("vomsID")]
    channels, seen = [], set()
    for c in cats:
        try:
            d = _get(CAT_URL % c["vomsID"]).json()
            data = (d.get("body") or {}).get("dataList") or []
        except Exception:
            continue
        for x in data:
            name = (x.get("name") or "").strip()
            pid = str(x.get("pID") or "")
            if not name or not pid or name in seen:
                continue
            seen.add(name)
            pics = x.get("pics") or {}
            logo = pics.get("highResolutionH") or pics.get("lowResolutionH") or ""
            channels.append({
                "cat": c.get("name") or "",
                "name": name,
                "pid": pid,
                "logo": logo,
            })
    with _lock:
        _chan_cache.update(ts=time.time(), channels=channels,
                           cats=[c.get("name") for c in cats])
        return list(channels), list(_chan_cache["cats"])


def _ddcalcu(puid, pid):
    keys = "cdabyzwxkl"
    d = datetime.datetime.now().strftime("%Y%m%d")
    out = []
    for i in range(len(puid) // 2):
        out.append(puid[len(puid) - i - 1])
        out.append(puid[i])
        if i == 1:
            out.append("v")
        elif i == 2:
            try:
                out.append(keys[int(d[2])])
            except Exception:
                out.append("a")
        elif i == 3:
            try:
                out.append(keys[int(pid[6])])
            except Exception:
                out.append("a")
        elif i == 4:
            out.append("a")
    return "".join(out)


def resolve_pid(pid, ttl=600):
    """频道 pID -> 最终直播 m3u8 URL（带 ddCalcu + 302）。"""
    with _url_lock:
        e = _url_cache.get(pid)
        if e and time.time() - e[0] < ttl:
            return e[1]
    ts = str(int(time.time() * 1000))
    headers = {
        "AppVersion": APP_VERSION,
        "TerminalId": "android",
        "X-UP-CLIENT-CHANNEL-ID": APP_VERSION + "-99000-201600010010028",
        "ClientId": _md5(str(int(time.time() * 1000))),
        "User-Agent": UA,
    }
    # CCTV5 / CCTV5+ 前端会关闭 appCode（否则拿不到直播）
    if pid not in ("641886683", "641886773"):
        headers["appCode"] = "miguvideo_default_android"
    m = _md5(ts + pid + APP_VERSION[:8])
    salt = ("%06d" % random.randint(0, 999999)) + "25"
    sign = _md5(m + "2cac4f2c6c3346a5b34e085725ef7e33migu" + salt[:4])
    params = {
        "sign": sign, "rateType": "3", "contId": pid,
        "timestamp": ts, "salt": salt,
        "flvEnable": "true", "super4k": "true",
    }
    if os.environ.get("MIGU_LIVE_H265") == "1":
        params["h265N"] = "true"
    r = requests.get(PLAY_API, params=params, headers=headers, timeout=12)
    j = r.json()
    u = ((j.get("body") or {}).get("urlInfo") or {}).get("url") or ""
    if not u:
        raise RuntimeError("no play url: %s" % str(j.get("message") or j.get("rid"))[:80])
    if "&puData=" in u:
        puid = u.split("&puData=", 1)[1]
        u += "&ddCalcu=" + _ddcalcu(puid, pid) + "&sv=10004&ct=android"
    # 只取 302 的 Location，绝不跟随播放流（直播流会一直下载）
    try:
        r2 = requests.get(u, headers={"User-Agent": UA},
                          timeout=(5, 8), allow_redirects=False, stream=True)
        loc = r2.headers.get("Location") or ""
        try:
            r2.close()
        except Exception:
            pass
    except Exception:
        loc = ""
    final = loc or u
    with _url_lock:
        _url_cache[pid] = (time.time(), final)
    return final


def _read_text(url, timeout=(5, 8), cap=512 * 1024):
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Referer": "https://www.miguvideo.com/"},
                     timeout=timeout, stream=True)
    try:
        data = b""
        for chunk in r.iter_content(8192):
            data += chunk
            if len(data) >= cap:
                break
        return r.status_code, data.decode("utf-8", "ignore"), (r.url or url)
    finally:
        try:
            r.close()
        except Exception:
            pass


def live_playlist(pid, bridge_base):
    """取频道直播清单，把分片改写为 bridge 的 /s/seg.ts?u=<b64>。"""
    url = resolve_pid(pid)
    st, text, base = _read_text(url)
    if st != 200 or "#EXTM3U" not in text:
        raise RuntimeError("level1 status=%s" % st)
    # 主清单 -> 取第一路变体
    if "#EXT-X-STREAM-INF" in text:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                vu = urllib.parse.urljoin(base, s)
                st2, text2, base2 = _read_text(vu)
                if st2 == 200 and "#EXTM3U" in text2:
                    text = text2
                    base = base2
                break
    base_dir = base.rsplit("/", 1)[0] + "/"
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and (
                ".ts" in s or ".m4s" in s or ".mp4" in s):
            absu = s if s.startswith("http") else urllib.parse.urljoin(base_dir, s)
            out.append("%s/s/seg.ts?u=%s&live=1"
                       % (bridge_base.rstrip("/"), _b64(absu)))
        else:
            out.append(line)
    # 直播清单标记为 EVENT：部分播放器（飞牛 trim_player）需要它才会持续刷新
    if ("#EXT-X-ENDLIST" not in text
            and not any(l.strip().startswith("#EXT-X-PLAYLIST-TYPE")
                        for l in out)):
        out.insert(1, "#EXT-X-PLAYLIST-TYPE:EVENT")
    return "\n".join(out)


def live_list():
    chans, cats = fetch_channels()
    return {"categories": cats, "channels": chans,
            "total": len(chans), "ts": int(time.time())}


if __name__ == "__main__":
    chans, cats = fetch_channels()
    print("分类:", cats)
    print("频道:", len(chans))
    for c in chans[:8]:
        print(" ", c["cat"], c["name"], c["pid"])
    pid = chans[0]["pid"] if chans else "608807420"
    print("解析", pid, "->", resolve_pid(pid)[:160])
    pl = live_playlist(pid, "http://127.0.0.1:8771")
    print("分片行:", sum(1 for l in pl.splitlines() if "/s/seg.ts" in l))
