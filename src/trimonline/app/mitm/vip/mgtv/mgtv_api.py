#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芒果TV 数据封装: 搜索 JSON 解析 + 分集 + 苹果CMS 对象组装。

搜索走 Playwright (mgtv_worker.py search) 抓 pc/search/v2 JSON;
本模块只做纯解析和补充请求, 不启动浏览器。
"""
import datetime, hashlib, json, os, re, time, urllib.parse, uuid
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
API = "https://pcweb.api.mgtv.com"
SEARCH_API = "https://mobileso.bz.mgtv.com/pc/search/v2"
# 搜索接口签名盐值 (来自 so.mgtv.com 前端 JS 2.6e76f43.js)
SIGN_SALT = "xHAa3YZflWLogZUOzl"


def _sign_enc(v):
    # 对应前端 qs.stringify(..., {encoder: encodeURI})
    return urllib.parse.quote(str(v), safe=":/?#[]@!$&'()*+,;=~-._")


def sign_params(params):
    """复现前端签名: md5(salt + 按 key ASCII 排序的 query + salt)。

    空字符串参数不参与签名 (前端 qs filter), 但保留在最终请求里。
    """
    items = [(k, v) for k, v in params.items()
             if not (isinstance(v, str) and v.strip() == "")]
    items.sort(key=lambda kv: kv[0])
    qs = "&".join(f"{_sign_enc(k)}={_sign_enc(v)}" for k, v in items)
    sig = hashlib.md5((SIGN_SALT + qs + SIGN_SALT).encode()).hexdigest()
    full = dict(params)
    full["signature"] = sig
    return full


class Mgtv:
    # 非正片关键词
    BLOCK_TITLE_RE = (
        r"预告|花絮|抢先|先导|混剪|片段|剪辑|看点|幕后|特辑|番外|"
        r"解说|一口气|名场面|盘点|速看|语录|反应|探班|独家|未播|删减|"
        r"合集|精彩片段|几分钟|彩蛋|直拍|纯享|加更|会员版"
    )
    _BLOCK = re.compile(BLOCK_TITLE_RE)

    def __init__(self, state_path="/root/mgtv_login/storage_state.json"):
        self.state_path = state_path
        self.load_state()

    def load_state(self):
        self.ck = {}
        try:
            self._mt = os.path.getmtime(self.state_path)
        except OSError:
            self._mt = None
        try:
            st = json.load(open(self.state_path))
            self.ck = {c["name"]: c["value"] for c in st.get("cookies", [])}
        except Exception:
            self.ck = {}
        self.h = {"User-Agent": UA, "Referer": "https://www.mgtv.com/"}
        if self.ck:
            self.h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.ck.items())

    def refresh(self):
        """cookie 文件有更新（保活/重新登录）时自动重载。"""
        try:
            mt = os.path.getmtime(self.state_path)
        except OSError:
            return
        if mt != getattr(self, "_mt", None):
            self.load_state()

    # ---------------- 搜索 (纯 HTTP, 签名) ----------------
    def search_http(self, kw, pn=1, pc=10):
        """直接调用签名搜索接口, 返回 JSON dict; 失败返回 None。"""
        self.refresh()
        did = (self.ck.get("__STKUUID")
               or self.ck.get("mba_deviceid") or uuid.uuid4().hex)
        params = {
            "allowedRC": "1", "src": "mgtv", "did": did,
            "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "signVersion": "1", "signNonce": uuid.uuid4().hex,
            "q": kw, "pn": str(pn), "pc": str(pc),
            "uid": "", "corr": "1", "_support": "10000000",
        }
        signed = sign_params(params)
        h = {"User-Agent": UA, "Referer": "https://so.mgtv.com/",
             "Accept": "application/json, text/plain, */*"}
        if self.ck:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.ck.items())
        try:
            r = requests.get(SEARCH_API, params=signed, headers=h, timeout=15)
            j = r.json()
            if j.get("code") == 200:
                return j
            return None
        except Exception:
            return None

    @staticmethod
    def _strip_tags(s):
        return re.sub(r"<[^>]+>", "", s or "").strip()

    @classmethod
    def is_main(cls, title, episodes):
        """过滤非正片 (预告/花絮/直拍/纯享等), 以及空专辑。"""
        if not title:
            return False
        if cls._BLOCK.search(title):
            return False
        return True

    # ---------------- 搜索 JSON 解析 ----------------
    def parse_search(self, raw):
        """解析 pc/search/v2 JSON -> 专辑列表。

        返回 [{
          cid, title, pic, story, year, type_name,
          episodes: [{vid, title, page_url}],
          episode_count
        }]
        """
        if isinstance(raw, str):
            try:
                j = json.loads(raw)
            except Exception:
                return []
        else:
            j = raw or {}
        data = j.get("data") or {}
        contents = data.get("contents") or []
        albums = {}
        for item in contents:
            d = item.get("data") or {}
            years = d.get("yearList") or []
            # 结构1: data.sourceList; 结构2: data.yearList[].sourceList
            groups = []
            if d.get("sourceList"):
                groups.append((d, d.get("sourceList") or []))
            for y in years:
                if y.get("sourceList"):
                    groups.append((y, y.get("sourceList") or []))
            for holder, slist in groups:
                for sl in slist:
                    if sl.get("source") != "imgo":
                        continue
                    purl = sl.get("url") or ""
                    m = re.search(r"/b/(\d+)/(\d+)\.html", purl)
                    cid = ""
                    first_vid = ""
                    if m:
                        cid = m.group(1)
                        first_vid = m.group(2)
                    else:
                        # 专辑页 /h/<album>.html, vid 在 sourceList.vid
                        mh = re.search(r"/h/(\d+)\.html", purl)
                        if mh:
                            cid = mh.group(1)
                            first_vid = str(sl.get("vid") or "")
                    if not cid:
                        continue
                    title = self._strip_tags(
                        holder.get("hitTitle") or holder.get("title")
                        or d.get("title") or d.get("hitTitle") or "")
                    pic = holder.get("pic") or d.get("pic") or ""
                    story = holder.get("story") or d.get("story") or ""
                    year = holder.get("year") or d.get("year") or ""
                    # 类型
                    type_name = ""
                    for x in (holder.get("desc") or d.get("desc") or []):
                        if x.get("label") in ("类型", "分类"):
                            type_name = x.get("text") or ""
                            break
                    eps = []
                    for v in (sl.get("videoList") or []):
                        vurl = v.get("url") or ""
                        mm = re.search(r"/b/(\d+)/(\d+)\.html", vurl)
                        if not mm:
                            continue
                        eps.append({
                            "vid": mm.group(2),
                            "album": mm.group(1),
                            "title": (v.get("title") or v.get("subtitle")
                                      or v.get("updateInfo") or "").strip(),
                            "page_url": "https:" + vurl if vurl.startswith("//") else vurl,
                        })
                    if not eps and first_vid:
                        # 单条 / 专辑页: 用 first_vid 拼播放页
                        eps.append({
                            "vid": first_vid, "album": cid,
                            "title": title or "正片",
                            "page_url": f"https://www.mgtv.com/b/{cid}/{first_vid}.html",
                        })
                    if not title:
                        title = eps[0]["title"] or "正片"
                    # 合并同专辑
                    a = albums.get(cid)
                    if a is None:
                        albums[cid] = {
                            "cid": cid, "title": title, "pic": pic,
                            "story": story, "year": year,
                            "type_name": type_name, "episodes": eps,
                            "episode_count": len(eps),
                        }
                    else:
                        # 择优补全, 合并分集
                        if len(eps) > len(a["episodes"]):
                            a["episodes"] = eps
                            a["episode_count"] = len(eps)
                        for k, v in (("title", title), ("pic", pic),
                                     ("story", story), ("year", year),
                                     ("type_name", type_name)):
                            if not a.get(k) and v:
                                a[k] = v
        out = []
        for a in albums.values():
            if not self.is_main(a["title"], a["episodes"]):
                continue
            out.append(a)
        # 集数多的（主专辑）优先
        out.sort(key=lambda x: -(x.get("episode_count") or 0))
        return out

    # ---------------- 分集补充 ----------------
    def showlist(self, album_cid, page=1, page_size=50):
        """variety/showlist: 芒果综艺/剧集分集 (无需签名)。"""
        self.refresh()
        try:
            r = requests.get(f"{API}/variety/showlist",
                             params={"collection_id": str(album_cid),
                                     "page": page, "pageSize": page_size},
                             headers=self.h, timeout=15)
            return r.json()
        except Exception:
            return {}

    def episodes_from_showlist(self, album_cid):
        j = self.showlist(album_cid)
        data = j.get("data") or {}
        eps = []
        for e in (data.get("list") or []):
            url = e.get("url") or ""
            m = re.search(r"/b/(\d+)/(\d+)\.html", url)
            if not m:
                continue
            eps.append({
                "vid": m.group(2), "album": m.group(1),
                "title": (e.get("t1") or e.get("title") or "").strip(),
                "page_url": "https://www.mgtv.com" + url if url.startswith("/") else url,
            })
        return eps

    def video_info(self, vid):
        self.refresh()
        try:
            r = requests.get(f"{API}/video/info", params={"vid": str(vid)},
                             headers=self.h, timeout=15)
            return r.json().get("data") or {}
        except Exception:
            return {}
