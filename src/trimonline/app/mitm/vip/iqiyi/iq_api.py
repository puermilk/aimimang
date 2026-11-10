#!/usr/bin/env python3
"""爱奇艺公开数据封装: 搜索(苹果CMS V10) + 剧集列表 / 登录态复用"""
import json, os, re, time, urllib.parse
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
DEFAULT_DEV = "dfa1caa4c808888f3ded4071d41e9bb5"
DEFAULT_UID = "2401485736"

class Iqiyi:
    def __init__(self, state_path="/root/iqiyi_login/storage_state.json", dev=None, uid=None):
        self.state_path = state_path
        self.load_state()
        self.dev = dev or DEFAULT_DEV
        self.uid = uid or DEFAULT_UID

    def load_state(self):
        try:
            self._mt = os.path.getmtime(self.state_path)
        except OSError:
            self._mt = None
        try:
            st = json.load(open(self.state_path))
        except (OSError, ValueError):
            st = {}
        self.ck = {c["name"]: c["value"] for c in (st.get("cookies") or [])
                   if c.get("domain") and "iqiyi" in c["domain"]}
        self.P = self.ck.get("P00001", "")
        self.h = {
            "User-Agent": UA,
            "Referer": "https://www.iqiyi.com/",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.ck.items()),
        }

    def refresh(self):
        """cookie 文件有更新时自动重新加载 (保活脚本会定期更新)。"""
        try:
            mt = os.path.getmtime(self.state_path)
        except OSError:
            return
        if mt != getattr(self, "_mt", None):
            try:
                self.load_state()
            except Exception:
                pass

    def search(self, kw, page=1):
        self.refresh()
        kwq = urllib.parse.quote(kw)
        url = ("https://mesh.if.iqiyi.com/portal/lw/search/homePageV3"
               f"?key={kwq}&current_page={page}&mode=1&source=input"
               "&pcv=17.104.26764&version=17.104.26764&pageNum=1&pageSize=25"
               f"&pu={self.uid}&u={self.dev}&deviceId={self.dev}")
        if self.P:
            url += f"&authcookie={urllib.parse.quote(self.P)}"
        r = requests.get(url, headers=self.h, timeout=12)
        r.raise_for_status()
        j = r.json()
        albums = []
        def walk(o):
            if isinstance(o, dict):
                ai = o.get("albumInfo")
                if isinstance(ai, dict) and ai:
                    albums.append(ai)
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        walk(j)
        return albums

    @staticmethod
    def parse_playurl(qips):
        """playUrl 'qips://tvid=..;vid=..;vtype=0;..' -> dict"""
        if not (qips or "").startswith("qips://"):
            return {}
        out = {}
        for kv in qips[7:].split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def video_page_url(self, tvid):
        """电影等无 pageUrl 时, 用播放信息接口取播放页"""
        self.refresh()
        url = ("https://mesh.if.iqiyi.com/player/pcw/video/playervideoinfo"
               f"?id={tvid}&locale=zh_cn")
        try:
            j = requests.get(url, headers=self.h, timeout=10).json()
            vu = (j.get("data") or {}).get("vu") or ""
            if vu.startswith("http://"):
                vu = "https://" + vu[7:]
            return vu
        except Exception:
            return ""

    # ---- 苹果CMS 风格对象 ----
    MAIN_CHANNEL = {"电视剧", "电影", "动漫", "综艺", "纪录片", "少儿", "体育",
                    "音乐", "片库", "全部", "片单"}
    BLOCK_CHANNEL = {"片花", "原创", "漫剧", "资讯", "娱乐", "剪辑", "混剪",
                     "解说", "搞笑", "高能", "盘点", "游戏", "生活", "时尚",
                     "旅游", "军事", "教育", "文化", "影视", "母婴", "三农",
                     "汽车", "房产", "财经", "动漫短片"}
    # 标题里出现这些词的多为解说/预告/花絮/剪辑等非正片
    BLOCK_TITLE_RE = (
        r"解说|预告|花絮|抢先|先导|混剪|片段|剪辑|看点|幕后|特辑|番外|"
        r"一口气|名场面|高能|盘点|速看|语录|反应|动态漫|漫画版|抢先版|"
        r"探班|独家|未播|删减|合集|精彩片段|几分钟|top\d|TOP\d|"
        r"笑出声|上头|爆哭|名导|深度解析|细节|细思极恐|彩蛋"
    )
    _BLOCK_TITLE = re.compile(BLOCK_TITLE_RE)

    @classmethod
    def is_main_album(cls, a):
        """判断是否为适合播放的“正片”(排除解说/预告/花絮/抢鲜/动态漫画等)"""
        ch = (a.get("channel") or "").split(",")[0].strip()
        title = (a.get("title") or "").strip()
        if not title:
            return False
        if ch in cls.BLOCK_CHANNEL:
            return False
        if cls._BLOCK_TITLE.search(title):
            return False
        total = a.get("totalNumber")
        vids = len(a.get("videos") or [])
        subs = (a.get("subscriptContent") or "").strip()
        # 备注是时长(如 02:44) 的多为片段/花絮 -> 判为非正片(电影备注是评分, 豁免)
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", subs) and ch != "电影":
            return False
        if ch in cls.MAIN_CHANNEL:
            return True
        return (isinstance(total, int) and total >= 2) or vids >= 2

    def native_albums(self, kw, page=1):
        """只保留爱奇艺自有版权(vtype=0)且适合播放的正片专辑"""
        seen, out = set(), []
        for a in self.search(kw, page):
            v0 = self.parse_playurl(a.get("playUrl") or "")
            if v0.get("vtype") != "0" or not v0.get("tvid"):
                continue
            if not self.is_main_album(a):
                continue
            albumid = v0.get("albumid") or a.get("qipuId")
            if not albumid or albumid in seen:
                continue
            seen.add(albumid)
            eps = [v for v in (a.get("videos") or [])
                   if self.parse_playurl(v.get("playUrl") or "").get("vtype") == "0"]
            if not eps:
                tv = v0.get("tvid")
                if tv:
                    page_url = a.get("pageUrl") or self.video_page_url(tv)
                    eps = [{"title": a.get("title") or "正片",
                            "number": "1",
                            "playUrl": f"qips://tvid={tv};vtype=0",
                            "pageUrl": page_url}]
            a["_episodes"] = eps
            out.append(a)
        # 正片优先: 主频道、集数多、有评分、非单片 排前
        ch_rank = {c: i for i, c in enumerate(
            ["电视剧", "电影", "动漫", "综艺", "纪录片", "少儿", "体育", "音乐"])}
        def rank(a):
            ch = (a.get("channel") or "").split(",")[0].strip()
            tot = a.get("totalNumber") or len(a.get("videos") or [])
            return (ch_rank.get(ch, 99), -(tot or 0))
        out.sort(key=rank)
        return out
