#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""咪咕视频数据封装: 搜索(苹果CMS V10) + 分集列表 + 播放地址(v3+ddCalcu签名)

参考:
- 爱奇艺源架构 (iqiyi_cms/iq_api.py)
- GitHub jym66/Dlink_Parse 的 migu.py (ddCalcu 签名算法)

播放流程:
1. 搜索: POST /search/v3/open-search/ -> 专集条目 (pID)
2. 分集: GET /program/v1/cont/episode-infos?epsIDS=<专集pID> -> 所有分集 (每个分集 pID)
3. 播放: GET /playurl/v3/play/playurl?contId=<单集pID> -> m3u8 URL
4. 签名: m3u8 URL 需要拼接 ddCalcu 参数 (由 puData/timestamp/ProgramID/Channel_ID 计算)
5. 客户端: 直接从咪咕 CDN 下载 m3u8 和 ts (桥不代理, 避免 NAS IP 被封锁)
"""
import json, os, time, urllib.parse, re
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
GATEWAY = "https://webapi.miguvideo.com/gateway"
CLIENT_ID = "79acd784-cbbb-4cae-8778-8723e001164b"
CHANNEL_ID = "0116_25000509-99000-100300010010001"

# 海报优先级: 竖版(V) > 横版(H); h5pics 为 jpg, pics 为 webp
_PIC_ORDER = ("highResolutionV", "lowResolutionV",
              "highResolutionH", "lowResolutionH")


def pick_pic(obj):
    """从搜索条目 / 详情页数据中选海报, 优先竖版(V)。"""
    if not isinstance(obj, dict):
        return ""
    for group in ("h5pics", "pics", "sharePics"):
        d = obj.get(group) or {}
        for k in _PIC_ORDER:
            v = d.get(k)
            if v:
                # 统一为 https 主域, 避免 http:8080 的混合内容问题
                if v.startswith("http://wapx.cmvideo.cn:8080/"):
                    v = "https://wapx.cmvideo.cn/" + v[len("http://wapx.cmvideo.cn:8080/"):]
                return v
    return ""



class Migu:
    # ============ 苹果CMS 风格分类 (参考爱奇艺) ============
    MAIN_CHANNEL = {"电影", "电视剧", "动漫", "综艺", "纪录片", "少儿", "体育",
                    "音乐", "片库", "全部", "片单"}
    BLOCK_CHANNEL = {"片花", "原创", "漫剧", "资讯", "娱乐", "剪辑", "混剪",
                     "解说", "搞笑", "高能", "盘点", "游戏", "生活", "时尚",
                     "旅游", "军事", "教育", "文化", "影视", "母婴", "三农",
                     "汽车", "房产", "财经", "动漫短片"}
    BLOCK_TITLE_RE = (
        r"解说|预告|花絮|抢先|先导|混剪|片段|剪辑|看点|幕后|特辑|番外|"
        r"一口气|名场面|高能|盘点|速看|语录|反应|动态漫|漫画版|抢先版|"
        r"探班|独家|未播|删减|合集|精彩片段|几分钟|top\d|TOP\d|"
        r"笑出声|上头|爆哭|名导|深度解析|细节|细思极恐|彩蛋"
    )
    _BLOCK_TITLE = re.compile(BLOCK_TITLE_RE)

    def __init__(self, state_path="/root/migu_login/storage_state.json"):
        self.state_path = state_path
        self.load_state()

    def load_state(self):
        self.ck = {}
        self.h = {
            "User-Agent": UA,
            "Referer": "https://m.miguvideo.com/",
            "Origin": "https://m.miguvideo.com",
            "terminalId": "h5",
            "appId": "miguvideo",
            "Accept": "application/json, text/plain, */*",
        }
        try:
            self._mt = os.path.getmtime(self.state_path)
        except OSError:
            self._mt = None
        try:
            st = json.load(open(self.state_path))
            self.ck = {c["name"]: c["value"] for c in st.get("cookies", [])}
        except Exception:
            self.ck = {}
        if self.ck:
            self.h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.ck.items())

    def refresh(self):
        """cookie 文件有更新时自动重新加载 (保活脚本会定期更新)。"""
        try:
            mt = os.path.getmtime(self.state_path)
        except OSError:
            return
        if mt != getattr(self, "_mt", None):
            self.load_state()

    # ============ 正片判断 (参考爱奇艺 is_main_album) ============
    @classmethod
    def is_main_album(cls, it):
        """判断是否为适合播放的"正片"(排除解说/预告/花絮/抢鲜/动态漫画等)"""
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            return False
        ch = (it.get("contDisplayName") or it.get("programType") or "").strip()
        if ch in cls.BLOCK_CHANNEL:
            return False
        if cls._BLOCK_TITLE.search(title):
            return False
        is_long = it.get("isLong")
        if isinstance(is_long, int) and is_long == 0:
            return False
        return True

    # ============ 搜索 ============
    def search(self, kw, page=1, page_size=20):
        """POST /search/v3/open-search/"""
        self.refresh()
        url = f"{GATEWAY}/search/v3/open-search/"
        body = {"keyword": kw, "pageNo": page, "pageSize": page_size}
        r = requests.post(url, json=body, headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _norm_item(it):
        pic = pick_pic(it)
        action = it.get("action") or {}
        cid = it.get("pID") or it.get("contentId") or it.get("assetID") or ""
        if action.get("params"):
            cid = action["params"].get("contentID") or cid
        title = it.get("name") or ""
        sub = it.get("subTitle") or ""
        year = it.get("year") or ""
        area = it.get("mediaArea") or ""
        director = it.get("director") or ""
        actor = it.get("actor") or ""
        type_name = it.get("contDisplayName") or it.get("programType") or "正片"
        is_long = it.get("isLong")
        tip = it.get("tip") or {}
        store = it.get("storeTip") or {}
        is_vip = tip.get("code") == "VIP" or store.get("code") == "VIP"
        return {
            "cid": str(cid),
            "title": title,
            "sub": sub,
            "pic": pic,
            "year": year,
            "area": area,
            "director": director,
            "actor": actor,
            "type_name": type_name,
            "score": it.get("score") or "",
            "desc": it.get("introduction") or it.get("subTitle") or "",
            "update_ep": it.get("updateEP") or "",
            "content_style": it.get("contentStyle") or "",
            "is_long": is_long,
            "is_vip": is_vip,
        }

    def native_albums(self, kw, page=1):
        """搜索 -> 专集列表 (过滤非正片)"""
        j = self.search(kw, page=page)
        body = j.get("body") or {}
        items = body.get("shortMediaAssetList") or []
        out, seen = [], set()
        for it in items:
            if not self.is_main_album(it):
                continue
            n = self._norm_item(it)
            if not n["cid"] or n["cid"] in seen:
                continue
            seen.add(n["cid"])
            out.append(n)
        return out

    # ============ 详情页刮削信息 (__CONTENT_INFO__) ============
    def content_info(self, album_cid):
        """抓取 PC 详情页 HTML，解析 window.__CONTENT_INFO__，返回专辑完整信息。
        包含: name/actor/director/year/area/contentStyle/detail/score/updateEP/
              pics/h5pics/datas(全部分集 pID+name+duration) 等。
        纯 requests 即可，无需浏览器/登录。
        """
        url = f"https://www.miguvideo.com/p/detail/{album_cid}"
        h = {"User-Agent": UA, "Referer": "https://www.miguvideo.com/"}
        try:
            r = requests.get(url, headers=h, timeout=20)
            if r.status_code != 200:
                return None
            html = r.text
        except Exception:
            return None
        raw = self._extract_js_object(html, "window.__CONTENT_INFO__")
        if not raw:
            return None
        try:
            j = json.loads(raw)
        except Exception:
            return None
        # 结构: {album_cid: {"1": {body: {data: {...}}}}}
        node = j.get(str(album_cid)) or (next(iter(j.values())) if j else None)
        if not isinstance(node, dict):
            return None
        for branch in node.values():
            data = ((branch or {}).get("body") or {}).get("data") or {}
            if data:
                return data
        return None

    @staticmethod
    def _extract_js_object(html, marker):
        """从 HTML 中提取 `marker = {...}` 的 JSON 对象（平衡大括号）。"""
        idx = html.find(marker)
        if idx < 0:
            return ""
        i = html.find("{", idx)
        if i < 0:
            return ""
        depth = 0
        in_str = False
        esc = False
        for p in range(i, len(html)):
            ch = html[p]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[i:p + 1]
        return ""

    # ============ 分集列表 (fallback) ============
    def episode_infos(self, album_cid):
        """GET /program/v1/cont/episode-infos?epsIDS=<专辑cid>"""
        self.refresh()
        url = f"{GATEWAY}/program/v1/cont/episode-infos"
        params = {"epsIDS": str(album_cid)}
        r = requests.get(url, params=params, headers=self.h, timeout=15)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _parse_episodes(j):
        body = j.get("body") or {}
        data = body.get("data") or []
        eps = []
        eps_count = 0
        update_ep = ""
        for item in data:
            album_cid = item.get("epsID") or ""
            eps_count = item.get("epsCount") or 0
            update_ep = item.get("updateEP") or ""
            datas = item.get("datas") or []
            for d in datas:
                pid = d.get("pID") or ""
                name = d.get("name") or "正片"
                duration = d.get("duration") or ""
                tip = d.get("tip") or {}
                is_vip = tip.get("code") == "VIP" or tip.get("code") == "FREE_LIMIT"
                if pid:
                    eps.append({
                        "cid": str(pid),
                        "name": name,
                        "duration": duration,
                        "is_vip": is_vip,
                        "album_cid": str(album_cid),
                        "eps_count": eps_count,
                        "update_ep": update_ep,
                    })
        return eps, eps_count, update_ep

    # ============ 播放地址 (v3 + ddCalcu) ============
    def playurl_v3(self, cid):
        """GET /playurl/v3/play/playurl -> 返回 m3u8 URL (不需要登录 cookie)"""
        url = f"{GATEWAY}/playurl/v3/play/playurl"
        params = {"contId": str(cid)}
        h = {"User-Agent": UA, "Referer": "https://m.miguvideo.com/"}
        r = requests.get(url, params=params, headers=h, timeout=20)
        r.raise_for_status()
        return r.json()

    def m3u8_url_v3(self, cid):
        """取 v3 m3u8 URL + 内容信息。返回 (m3u8_url, info_dict)"""
        j = self.playurl_v3(cid)
        body = j.get("body") or {}
        content = body.get("content") or {}
        url_info = body.get("urlInfo") or {}
        m3u8 = url_info.get("url") or ""
        info = {
            "contId": content.get("contId") or cid,
            "contName": content.get("contName") or "",
            "duration": content.get("duration") or "",
            "assetId": content.get("assetId") or "",
            "needAuth": content.get("needAuth"),
            "isVip": (content.get("tip") or {}).get("code") == "VIP",
            "urlType": (url_info or {}).get("urlType"),
            "authResult": (body.get("auth") or {}).get("authResult"),
            "logined": (body.get("auth") or {}).get("logined"),
        }
        return m3u8, info

    @staticmethod
    def dd_calcu(m3u8_url):
        """GitHub jym66/Dlink_Parse migu.py 的 ddCalcu 签名算法。
        由 puData / timestamp / ProgramID / Channel_ID 重排生成。
        """
        params = {}
        query = m3u8_url.split("?")[-1]
        for kv in query.split("&"):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            params[k] = v
        pu = params.get("puData", "")
        if not pu:
            return ""
        pu_list = list(pu)
        p = 0
        result = []
        while (2 * p) < len(pu_list):
            result.append(pu_list[len(pu_list) - p - 1])
            if p < len(pu_list) - p - 1:
                result.append(pu[p])
            if p == 1:
                result.append('e')
            if p == 2:
                ts = params.get("timestamp", "")
                result.append(list(ts)[6] if len(ts) > 6 else "")
            if p == 3:
                pid = params.get("ProgramID", "")
                result.append(list(pid)[2] if len(pid) > 2 else "")
            if p == 4:
                cid = list(params.get("Channel_ID", ""))
                result.append(cid[len(cid) - 4] if len(cid) >= 4 else "")
            p += 1
        return ''.join(result)

    @classmethod
    def signed_m3u8_url(cls, m3u8_url):
        """给 m3u8 URL 拼接 ddCalcu 签名参数"""
        if not m3u8_url:
            return m3u8_url
        if "ddCalcu=" in m3u8_url:
            return m3u8_url
        dd = cls.dd_calcu(m3u8_url)
        if dd:
            return m3u8_url + "&ddCalcu=" + dd
        return m3u8_url
