#!/usr/bin/env python3
"""飞牛影视 在线源 MITM PoC v2

接管 /var/run/trim_media.sock（官方后端已被挪到 trim_media.real.sock）：
  1. 拦截 GET /v/api/v1/search/list → 本地结果 + 采集站结果合并
  2. 伪造 on* 开头 guid 的 item/season/episode/play/info/stream 等接口
  3. 伪造假媒体库「豆瓣热播」(mediadb/list + mdb/:guid + item/list)，
     条目按标题实时解析到采集站片源
  4. 代理在线 m3u8 清单/分片：同源路径、无 CORS；清单内广告分段自动剔除
  5. 播放地址多线路兜底：主线失效自动尝试其它线路($$$分组)的同集地址

用法: PYTHONPATH=/usr/local/trim_media_mitm/lib /usr/bin/python3 mitm.py
"""
import asyncio
import base64
import concurrent.futures
import functools
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse

from difflib import SequenceMatcher

import aiohttp

try:                                  # 内置配置页（爱迷茫 · 在线源设置）
    import webui
    _WEBUI_ERR = ""
except Exception as _e:               # 缺文件/坏文件也不该影响播放链路
    webui = None
    _WEBUI_ERR = repr(_e)[:120]

try:                                  # JS 蜘蛛引擎（drpy2 on QuickJS）
    import jsspider
    _JS_ERR = ""
except Exception as _e:               # 引擎缺失只影响 JS 源，不影响 CMS 源
    jsspider = None
    _JS_ERR = repr(_e)[:120]

try:                                  # 采集站返回体解析（JSON / XML 都吃）
    import source_check
    _CMS_ERR = ""
except Exception as _e:               # 缺了它只影响 XML 源（JSON 仍可走 json.loads）
    source_check = None
    _CMS_ERR = repr(_e)[:120]

# QuickJS 上下文不能跨线程 → 每个站点一个独立上下文，这里按站点并发
JS_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="drpy")

SOCK = "/var/run/trim_media.sock"
REAL = "/var/run/trim_media.real.sock"
# 官方后端 TCP 端口（--port=8005）。unix socket 文件可能被官方重启/改名破坏，
# 但 TCP 始终可用 → forward 优先走 TCP，unix 仅作回退。
UPSTREAM_TCP = ("127.0.0.1", 8005)
IDLE_TIMEOUT = 90
_DIAG = os.environ.get("MITM_DIAG", "0")          # 客户端连接空闲超时，防连接堆积
_CAPN = [0]                                       # DIAG 响应比对计数器
REG_FILE = "/usr/local/trim_media_mitm/online_registry.json"
ONLINE_TIMEOUT = 7.0
UPSTREAM_TIMEOUT = 40.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# --------------------------------------------- 在线采集站（可配置·无内置源）---
# 采集站需提供苹果CMS V10 标准采集接口（api.php/provide/vod/ 这一类）：
#   <api>?ac=videolist&wd=关键词    搜索
#   <api>?ac=detail&ids=<vod_id>    详情（搜索结果没带播放地址时补拉）
# 响应为 JSON，顶层 list 数组；条目至少要有 vod_id / vod_name / vod_play_url。
# vod_play_url 格式："集名$地址#集名$地址"，多线路以 $$$ 分隔。
# 地址必须是「直接可放」的（.m3u8 / 直链）；/share/ 这类 HTML 分享页放不了，
# 会被 _looks_m3u8() 判掉——所以至少要有「一条」m3u8 线路。
#
# ★ 本插件**不含任何内置源**：装完是空的，用谁的源由你自己决定、自己填。
#   两种填法（改的都是同一个文件 conf/sources.json）：
#     a) 打开配置页填（推荐）：
#          http://<NAS 的 IP>:5666/v/__mitm__/config
#        填地址 → 标单线路/多线路 → 「保存并测速」→ 看推荐方案
#        也能直接粘一个 TVBox 配置地址，自动挑出其中「能直接用」的接口
#     b) 直接编辑 /usr/local/trim_media_mitm/conf/sources.json 后热加载：
#          curl -s --unix-socket /var/run/trim_media.sock \
#               'http://x/v/__mitm__/sources?reload=1'
#
# 字段：
#   name    名称（同时作为同源去重键，不能重复）
#   api     采集接口，必须以 http:// 或 https:// 开头
#   referer 可选，防盗链用；不填按 api 域名推导
#   lines   可选，"single" 单线路 / "multi" 多线路 / "auto" 自动判断
#           （只影响配置页的分组与推荐排序，不影响播放逻辑）
#
# 两级含义：
#   primary  —— 主用：按顺序查询，按标题匹配度评分；priority 下命中即停
#   fallback —— 兜底：priority 下仅当主用全部落空才查；parallel 下与主用一起查
#   查询方式见 conf/config.json 的 source_mode；source_limit 控制最多启用几个源。
CONF_DIR = "/usr/local/trim_media_mitm/conf"
SOURCES_FILE = os.path.join(CONF_DIR, "sources.json")
CONFIG_FILE = os.path.join(CONF_DIR, "config.json")

# 飞牛影视自己的用户库 / 登录令牌库。插件只读，用来判断「当前请求是谁」。
# 在线源的可见范围（所有用户 / 仅管理员 / 指定用户）就靠这张表落地。
TRIM_DATA_DIR = "/usr/local/apps/@appdata/trim.media"
TRIM_MEDIA_DB = os.path.join(TRIM_DATA_DIR, "database", "trimmedia.db")
TRIM_ACTIVITY_DB = os.path.join(TRIM_DATA_DIR, "database", "trimactivity.db")
_TRIM_DB_FALLBACKS = [
    ("/vol1/@appdata/trim.media/database/trimmedia.db",
     "/vol1/@appdata/trim.media/database/trimactivity.db"),
    ("/vol2/@appdata/trim.media/database/trimmedia.db",
     "/vol2/@appdata/trim.media/database/trimactivity.db"),
]

# 写回文件时附带的说明（用户手改时能看懂）
SOURCE_HELP = [
    "爱迷茫 · 在线源清单。改完不用重新打包：",
    "  配置页  http://<NAS 的 IP>:5666/v/__mitm__/config",
    "  热加载  curl -s --unix-socket /var/run/trim_media.sock 'http://x/v/__mitm__/sources?reload=1'",
    "  测速    python3 /usr/local/trim_media_mitm/source_check.py --keyword 凡人",
    "",
    "primary  = 主用：按顺序查询，按标题匹配度评分；priority 下命中即停",
    "fallback = 兜底：priority 下仅当主用全部落空才查；parallel 下与主用一起查",
    "查询方式在 conf/config.json 的 source_mode（priority/parallel）；",
    "source_limit = 最多启用几个采集源（0=不限，按主用→兜底顺序取前 N 个）",
    "字段：name 名称（同时作为同源去重键）、api 采集接口、",
    "      referer 可选（缺省按 api 域名推导）、lines 可选 single/multi/auto",
    "采集站必须支持标准接口：<api>?ac=videolist&wd=关键词 与 <api>?ac=detail&ids=",
    "",
    "js = TVBox 的 drpy 规则源（配置里 type:3 那些）：",
    "      字段：name 名称、js 站点规则文件的网址（TVBox 配置里的 ext）",
    "      referer 可选；lines 可选 single/multi/auto",
    "      插件自带 drpy2 运行时，只要给出站点 js 网址即可，不必再填 api。",
]


def _norm_sources(obj):
    """整理配置里的源，丢掉不合格条目。返回 (primary, fallback, js)；
    结构性错误（不是对象 / 某级不是数组）返回 None。
    注意：**空表是合法的**（本插件不含内置源，装完就是空的）。"""
    if not isinstance(obj, dict):
        return None
    out = []
    for level in ("primary", "fallback"):
        items = obj.get(level)
        if items is None:
            items = []
        if not isinstance(items, list):
            return None
        norm, seen = [], set()
        for it in items:
            if not isinstance(it, dict):
                continue
            api = str(it.get("api") or "").strip()
            if not api.startswith(("http://", "https://")):
                continue
            host = api.split("//", 1)[-1].split("/", 1)[0]
            name = str(it.get("name") or "").strip() or host
            if name in seen:                       # 同名只留第一个
                continue
            seen.add(name)
            ref = str(it.get("referer") or "").strip() or ("https://" + host + "/")
            lines = str(it.get("lines") or "").strip().lower()
            if lines not in ("single", "multi"):
                lines = "auto"
            try:
                site_timeout = float(it.get("timeout") or 0)
            except (TypeError, ValueError):
                site_timeout = 0
            if not (1 <= site_timeout <= 30):
                site_timeout = 0
            norm.append({"name": name, "api": api, "referer": ref,
                         "lines": lines, "kind": "cms",
                         "timeout": site_timeout})
        out.append(norm)

    # ---- JS（drpy）源：js 可以是网址，也可以是 `**`base64 或直接贴的规则文本 ----
    items = obj.get("js")
    if items is None:
        items = []
    if not isinstance(items, list):
        return None
    jnorm, jseen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        js = str(it.get("js") or it.get("ext") or "").strip()
        if not js:
            continue
        ok_url = js.lower().startswith(("http://", "https://"))
        ok_b64 = js.startswith("**")
        ok_txt = ("rule" in js and "{" in js)
        if not (ok_url or ok_b64 or ok_txt):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            if ok_url:
                name = urllib.parse.unquote(js.rstrip("/").rsplit("/", 1)[-1]) or "JS源"
            else:
                m = re.search(r"title\s*:\s*['\"]([^'\"]+)", js)
                name = (m.group(1) if m else "") or "JS源"
        if name in jseen:
            continue
        jseen.add(name)
        lines = str(it.get("lines") or "").strip().lower()
        if lines not in ("single", "multi"):
            lines = "auto"
        ref = str(it.get("referer") or "").strip()
        if not ref and ok_url:
            ref = "https://" + js.split("//", 1)[-1].split("/", 1)[0] + "/"
        jnorm.append({"name": name, "js": js, "referer": ref,
                      "lines": lines, "kind": "js"})
    out.append(jnorm)
    return out[0], out[1], out[2]


def _read_sources_file():
    """读 conf/sources.json → (ok, primary, fallback, js, 说明)
    ok=False 表示没读到可用配置（文件缺失/损坏），说明里写原因。"""
    try:
        with open(SOURCES_FILE, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return False, [], [], [], "conf/sources.json 不存在"
    except Exception as e:
        return False, [], [], [], "读取失败：%s" % repr(e)[:90]
    try:
        got = _norm_sources(json.loads(raw))
    except Exception as e:
        return False, [], [], [], "JSON 解析失败：%s" % repr(e)[:70]
    if got is None:
        return False, [], [], [], "primary / fallback / js 必须是数组"
    return True, got[0], got[1], got[2], ""


def _apply_sources(p, f, js, origin):
    global CMS_PRIMARY, CMS_FALLBACK, CMS_SITES, JS_SITES, SOURCES_ORIGIN
    CMS_PRIMARY, CMS_FALLBACK, SOURCES_ORIGIN = p, f, origin
    CMS_SITES = CMS_PRIMARY + CMS_FALLBACK
    JS_SITES = list(js or [])
    return "已加载 %d 主用 + %d 兜底 + %d JS（%s）" % (
        len(p), len(f), len(JS_SITES), origin)


def load_sources():
    """启动时读 conf/sources.json。返回 (primary, fallback, js, 来源说明, 提示信息)。
    本插件不含内置源：没有配置文件就是「没有源」，由配置页 / 手改文件来填。"""
    ok, p, f, js, why = _read_sources_file()
    if not ok:
        return [], [], [], "未配置", why
    if not (p or f or js):
        return [], [], [], "conf/sources.json", "尚未配置任何源，请打开配置页填写"
    return p, f, js, "conf/sources.json", ""


def save_sources(primary, fallback, js=None):
    """把配置页提交的源写回 conf/sources.json，并立即生效。
    返回 (ok, 说明)。"""
    got = _norm_sources({"primary": primary, "fallback": fallback, "js": js or []})
    if got is None:
        return False, "字段不合法：primary / fallback / js 必须是数组"
    p, f, j = got
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = SOURCES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"_help": SOURCE_HELP, "primary": p,
                       "fallback": f, "js": j},
                      fh, ensure_ascii=False, indent=2)
        os.replace(tmp, SOURCES_FILE)
    except Exception as e:
        return False, "写入失败：%s" % repr(e)[:90]
    _apply_sources(p, f, j, "conf/sources.json")
    return True, "已保存 %d 主用 + %d 兜底 + %d JS" % (len(p), len(f), len(j))


CMS_PRIMARY, CMS_FALLBACK, JS_SITES, SOURCES_ORIGIN, _SOURCES_MSG = load_sources()
CMS_SITES = CMS_PRIMARY + CMS_FALLBACK      # 仅用于排序索引，勿直接遍历查询


def reload_sources():
    """热加载源清单。
    文件正常（含「空清单」）→ 立即生效
    文件缺失 / 损坏         → 保持现有源不变并说明原因（避免手滑把好配置顶掉）"""
    ok, p, f, js, why = _read_sources_file()
    if not ok:
        return "读取失败，保持现有源不变（%s）" % why
    if not (p or f or js):
        return _apply_sources([], [], [], "conf/sources.json（空）")
    return _apply_sources(p, f, js, "conf/sources.json")


# 豆瓣热播 伪媒体库
FAKE_LIB_GUID = "on" + hashlib.md5(b"fnv-douban-hot").hexdigest()[:30]

# 红果短剧 伪媒体库（由本机 HGDJ 服务提供元数据/播放流）
HGDJ_LIB_GUID = "on" + hashlib.md5(b"fnv-hgdj-lib").hexdigest()[:30]
HGDJ_LIB_NAME = "红果短剧"
HGDJ_DEFAULT_URL = "http://127.0.0.1:18098"
HGDJ_TTL = 1800
HGDJ_DETAIL_TTL = 3600
HGDJ_CATS = ("real-drama", "comic-drama", "ai-drama", "comic")
_HGDJ = {"ts": 0.0, "items": {}, "order": [], "task": None,
         "load_task": None, "totals": {}, "total_pages": {},
         "next_page": {}, "all_loaded": False}
_HGDJ_CFG = {"ts": 0.0, "enabled": False, "base": HGDJ_DEFAULT_URL}

# 豆瓣榜单分类 → 伪媒体库（每个分类一个独立库，侧栏分别显示）
# (key, 显示名, 豆瓣type, 豆瓣tag, 条目数, 前端category)
# tag 均取豆瓣 search_subjects 实测可用的标签
DOUBAN_CATS = [
    ("hot_movie",  "豆瓣热门电影", "movie", "热门", 20, "Movie"),
    ("hot_tv",     "豆瓣热门剧集", "tv",    "热门", 20, "TV"),
    ("new_movie",  "豆瓣新片",     "movie", "最新", 20, "Movie"),
    ("cn_movie",   "豆瓣华语电影", "movie", "华语", 20, "Movie"),
    ("us_movie",   "豆瓣欧美电影", "movie", "欧美", 20, "Movie"),
    ("jp_movie",   "豆瓣日本电影", "movie", "日本", 20, "Movie"),
    ("kr_movie",   "豆瓣韩国电影", "movie", "韩国", 20, "Movie"),
    ("anime_movie", "豆瓣动画电影", "movie", "动画", 20, "Movie"),
    ("scifi_movie", "豆瓣科幻电影", "movie", "科幻", 20, "Movie"),
    ("sus_movie",  "豆瓣悬疑电影", "movie", "悬疑", 20, "Movie"),
]
CAT_BY_KEY = {c[0]: c for c in DOUBAN_CATS}


def lib_guid_of(key):
    return "on" + hashlib.md5(("fnv-lib-" + key).encode()).hexdigest()[:30]


def item_guid_of(cat_key, douban_id):
    return "on" + hashlib.md5(
        ("fnv-douban-" + cat_key + "-" + str(douban_id)).encode()).hexdigest()[:30]


LIB_GUIDS = {c[0]: lib_guid_of(c[0]) for c in DOUBAN_CATS}
ALL_LIB_GUIDS = set(LIB_GUIDS.values()) | {FAKE_LIB_GUID, HGDJ_LIB_GUID}
DOUBAN_TTL = 1800          # 豆瓣榜单缓存
RESOLVE_NEG_TTL = 600      # 解析失败后的重试间隔
AD_MAX_DUR = 30.0          # 广告分组时长上限（秒）
MIN_MAIN_DUR = 60.0        # 正片分组时长下限（秒）
DUR_V = 2                  # 时长算法版本（v2 起只统计会播的分组）

# 若干片源方在清单里标的「固定广告时长」——片头 5.48s、片尾 0.56s、
# 以及 5.32+3.36 / 4.0 这类组合广告。这些是片源方的权威标注，比启发式阈值更准，
# 命中即整组剔除。纯被动数据表：只有真的遇到这些时长才生效，不引入任何源。
KNOWN_AD_DURS = {5.48, 5.32, 4.00, 0.56, 3.36, 5.0, 4.5}

# 前端官方图片接口前缀（在线海报会被直接拼在其后，无分隔符）
SYS_IMG_PREFIX = "/v/api/v1/sys/img"

# 部分源站图片域名的可用镜像映射：源站 → CDN（绕过防盗链/加速）。
# 同样是被动数据表：只在 URL 里出现这些域名时才改写，不引入任何源。
HOSTS_MIRROR = [
    ("img1.wsyzy.org", "fan.cloudflare.182682.xyz"),
    ("fiizvfck.top", "fan.cloudflare.182682.xyz"),
    ("images.67c6c7a.com", "fan.cloudflare.182682.xyz"),
]

registry = {}      # guid -> 在线条目元数据
session = None     # aiohttp ClientSession
_douban = {"subs": None, "ts": 0}

GUID_RE = re.compile(r"on[0-9a-f]{30}")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ----------------------------------------------------------------- registry --
def load_registry():
    global registry
    try:
        registry = json.load(open(REG_FILE, encoding="utf-8"))
    except Exception:
        registry = {}
    # 重启后先把已落盘的红果剧集恢复进内存索引，库计数/列表无需等网络
    # （episode/season 记录也带 hgdj 标记，别把它们混进媒体库列表）
    for _g, _rec in registry.items():
        if (isinstance(_rec, dict) and _rec.get("hgdj")
                and not _rec.get("series")
                and not _rec.get("episode_number")):
            _HGDJ["order"].append(_g)
            _HGDJ["items"][_g] = _rec

watch = {}          # 观看记录；实际内容在 save_watch 定义之后载入


_SAVE_MIN_INTERVAL = 3.0     # 秒；去抖用
_save_last = [0.0]


def save_registry(force=False):
    """把 registry 落盘。

    后台批处理（resolve_all_pending / fill_durations）会按条目调用本函数，
    4.8MB 的同步序列化+写盘会把 asyncio 事件循环反复卡住——nginx 因此等
    上游等超时（error.log 里的 "upstream timed out ... item/list"）。
    这里做去抖：3 秒内只真正写一次；每个批次的收尾用 force=True 保证最终一致。
    """
    now = time.time()
    if not force and now - _save_last[0] < _SAVE_MIN_INTERVAL:
        return
    _save_last[0] = now
    try:
        clean = {}
        for g, rec in registry.items():
            if isinstance(rec, dict):
                clean[g] = {k: v for k, v in rec.items()
                            if not k.startswith("_") and not hasattr(v, "add_done_callback")}
        tmp = REG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False)
        os.replace(tmp, REG_FILE)     # 原子替换：读侧永远看不到半截文件
    except Exception as e:
        log("registry save err:", e)


# ── 观看记录（继续播放）─────────────────────────────────────────
# 本地影片的记录由官方 /play/record 写进 item_user_play；在线影片官方不认识，
# 这里按用户单独记一份。play/list 会把官方本地列表和在线列表合并返回。
WATCH_FILE = "/usr/local/trim_media_mitm/watch_history.json"
WATCH_MAX = 100


def load_watch():
    try:
        d = json.load(open(WATCH_FILE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_watch():
    try:
        json.dump(watch, open(WATCH_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception as e:
        log("watch save err:", e)


watch.update(load_watch())


def _watch_owner(rec):
    return str(rec.get("u") or "") if isinstance(rec, dict) else ""


def _watch_guid(key, rec):
    """兼容旧格式：旧版 key 就是 guid，没有 u / g 字段。"""
    g = rec.get("g") if isinstance(rec, dict) else ""
    if isinstance(g, str) and g:
        return g
    return key.split("|", 1)[1] if "|" in key else key


def _watch_key(user_guid, guid):
    return (user_guid or "") + "|" + guid


def note_watch(guid, pos, extra=None, user=None):
    """记录一条观看进度（按用户隔离）。pos<=0 表示已看完/退出。"""
    if not guid or not isinstance(registry.get(guid), dict):
        return
    try:
        pos = int(float(pos or 0))
    except Exception:
        pos = 0
    uid = str((user or {}).get("guid") or "")
    key = _watch_key(uid, guid)
    if pos <= 0:
        # 看完/退出：清掉这个用户自己的进度，别让「继续播放」一直挂着旧位置。
        # 管理员顺手清掉旧版没有 u 字段的遗留记录。
        changed = watch.pop(key, None) is not None
        if (user or {}).get("is_admin") and not _watch_owner(watch.get(guid, {})):
            changed = watch.pop(guid, None) is not None or changed
        if changed:
            save_watch()
        return
    rec = {"ts": pos, "at": int(time.time()), "g": guid, "u": uid}
    rec.update(extra or {})
    watch[key] = rec
    mine = [(k, v) for k, v in watch.items()
            if _watch_owner(v) == uid and isinstance(v, dict)]
    if len(mine) > WATCH_MAX:
        for k, _ in sorted(mine, key=lambda kv: kv[1].get("at", 0))[:len(mine) - WATCH_MAX]:
            watch.pop(k, None)
    save_watch()


def remove_watch(guid, user=None):
    """删除某个用户自己的在线观看记录；管理员顺手清掉旧版遗留记录。"""
    uid = str((user or {}).get("guid") or "")
    changed = False
    if watch.pop(_watch_key(uid, guid), None) is not None:
        changed = True
    if (user or {}).get("is_admin") and not _watch_owner(watch.get(guid, {})):
        if watch.pop(guid, None) is not None:
            changed = True
    if changed:
        save_watch()


def watch_list(user=None):
    """继续播放列表：按用户隔离，按最近观看倒序，附完整条目字段。"""
    uid = str((user or {}).get("guid") or "")
    is_admin = bool((user or {}).get("is_admin"))
    chosen = {}
    for key, rec in watch.items():
        if not isinstance(rec, dict):
            continue
        owner = _watch_owner(rec)
        if owner:
            if owner != uid:
                continue
        elif not is_admin:
            # 旧版记录没有 u 字段：只给管理员看，避免升级后把历史暴露给其他账号
            continue
        guid = _watch_guid(key, rec)
        if not guid:
            continue
        old = chosen.get(guid)
        if old is None or rec.get("at", 0) > old.get("at", 0):
            chosen[guid] = rec
    out = []
    for guid, rec in sorted(chosen.items(),
                            key=lambda kv: kv[1].get("at", 0), reverse=True):
        r = registry.get(guid)
        if not isinstance(r, dict) or not (r.get("eps") or []):
            continue                      # 片源已失效的不再展示
        item = build_item(guid, r)
        item["last_play"] = True
        item["visible"] = True
        item["watched_ts"] = rec.get("at", 0)
        item["play_position"] = rec.get("ts", 0)
        item["play_error"] = ""
        out.append(item)
    return out


def _play_body(body):
    try:
        pd = json.loads((body or b"").decode("utf-8", "replace") or "{}")
        return pd if isinstance(pd, dict) else {}
    except Exception:
        return {}


def _play_gid(pd, body_guid=None, path_guid=None):
    return str((pd or {}).get("item_guid")
               or (pd or {}).get("guid")
               or body_guid or path_guid or "")


def _play_main_guid(gid):
    """在线条目（含 onv/ona 派生 guid）→ 主 guid；本地 guid → None。"""
    if not gid:
        return None
    if gid in registry:
        return gid
    return resolve_guid(gid)


def _http_status(head):
    try:
        return int(head.split(b" ", 2)[1])
    except Exception:
        return 0


def _merge_play_items(local, online):
    """官方本地继续播放 + 在线继续播放，按 guid 去重（本地优先）。"""
    out, seen = [], set()
    for it in list(local or []) + list(online or []):
        if not isinstance(it, dict):
            continue
        g = it.get("guid")
        if g and g in seen:
            continue
        if g:
            seen.add(g)
        out.append(it)
    return out


def make_guid(site, vid):
    return "on" + hashlib.md5(f"{site}|{vid}".encode()).hexdigest()[:30]


def season_guid_of(guid):
    return "on" + hashlib.md5((guid + "|s1").encode()).hexdigest()[:30]


def episode_guid_of(guid, i):
    return "on" + hashlib.md5(f"{guid}|ep{i}".encode()).hexdigest()[:30]


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def resolve_guid(text):
    """路径/参数里找在线条目的主 guid（兼容 onv/ona 派生 guid 反查）"""
    m = GUID_RE.search(text)
    if m and m.group(0) in registry:
        return m.group(0)
    # 伪媒体库 guid 本身也是合法引用（前端进入库详情页会请求它）
    if m and m.group(0) in ALL_LIB_GUIDS:
        return None            # 库 guid 不作为条目，走库/条目各自分支
    m = re.search(r"on[a-z][0-9a-f]{29}", text)
    if m:
        cand = m.group(0)
        for g, rec in registry.items():
            if isinstance(rec, dict) and (rec.get("vg") == cand or rec.get("ag") == cand):
                return g
    return None


def lib_key_of_guid(g):
    """库 guid → 分类 key（不是库 guid则返回 None）"""
    for k, lg in LIB_GUIDS.items():
        if lg == g:
            return k
    return None


# ------------------------------------------------------- CMS 搜索 & 解析 ---

def _looks_m3u8(url):
    """粗判是否为可直接播放的 m3u8（排除 HTML 分享页/落地页）"""
    if not url:
        return False
    u = url.lower().split("?")[0]
    if u.endswith((".m3u8", ".m3u")):
        return True
    # 无扩展名时，看是否明显是网页
    if re.search(r"/share/|/detail|/vod|/play/|/index\.html|/\w+\.html?$", u):
        return False
    return True


def parse_episodes(play_url):
    """CMS vod_play_url: "集名$url#集名$url"，多线路以 $$$ 分隔。
    返回主线各集，其余线路的同集 url 收进 ep['urls']（线路兜底）。
    不可直放的线路（如 HTML 分享页）不作为主线路。

    有些站（如 22-白嫖资源）只返回裸 m3u8 地址，没有 "集名$" 前缀；
    这里也兼容，否则搜索能命中但会被当成“无播放地址”丢掉。
    """
    raw = (play_url or "").strip()
    if raw.startswith("http") and "$" not in raw:
        parts = [x.strip() for x in re.split(r"[#\n]+", raw) if x.strip()]
        if parts and all(x.startswith("http") and _looks_m3u8(x)
                         for x in parts[:3]):
            return [{"name": "正片" if len(parts) == 1 else "第%d集" % (i + 1),
                     "url": x, "urls": [x]} for i, x in enumerate(parts)]
    groups = []
    playable = []
    for line in (play_url or "").split("$$$"):
        eps = []
        for part in line.split("#"):
            if "$" in part:
                name, url = part.split("$", 1)
                url = url.strip()
                if url.startswith("http"):
                    eps.append({"name": name.strip(), "url": url})
        if eps:
            groups.append(eps)
            if all(_looks_m3u8(e["url"]) for e in eps[:3]):
                playable.append(eps)
    # 主线路优先取「可直放」的那组
    if playable and groups and groups[0] is not playable[0]:
        groups.remove(playable[0])
        groups.insert(0, playable[0])
    if not groups:
        return []
    main = groups[0]
    for i, ep in enumerate(main):
        urls = [ep["url"]]
        for g in groups[1:]:
            if i < len(g) and g[i]["url"] not in urls:
                urls.append(g[i]["url"])
        ep["urls"] = urls
    for g in groups[1:]:                      # 其余线路多出来的集也并入
        for j in range(len(main), len(g)):
            main.append({**g[j], "urls": [g[j]["url"]]})
    return main


def _parse_cms(raw):
    """采集站返回体 → dict。

    苹果CMS 有两种输出，站点靠地址区分（/at/json/ 与 /at/xml/）：
    只认 JSON 的话，XML 站点会报
        JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    XML 的归一化在 source_check.load_payload 里（与体检走同一份代码）。
    """
    if source_check is not None:
        return source_check.load_payload(raw)
    return json.loads(raw.decode("utf-8", "replace"))


def _site_timeout(site):
    try:
        t = float((site or {}).get("timeout") or 0)
    except (TypeError, ValueError):
        t = 0
    return t if 1 <= t <= 30 else ONLINE_TIMEOUT


async def cms_detail(site, vid):
    """拉取条目详情（搜索接口不返回 vod_play_url，必须走 ac=detail）"""
    if not vid:
        return None
    url = site["api"] + "?ac=detail&ids=" + urllib.parse.quote(str(vid))
    hdrs = {"User-Agent": UA, "Referer": site["referer"], "Accept": "*/*"}
    try:
        async with session.get(url, headers=hdrs, ssl=False,
                               timeout=aiohttp.ClientTimeout(
                                   total=_site_timeout(site))) as resp:
            raw = await resp.read()
        data = _parse_cms(raw)
    except Exception as e:
        log("cms detail err", site.get("name"), vid, repr(e)[:80])
        return None
    lst = data.get("list") or []
    return lst[0] if lst else None


async def cms_query(site, kw):
    url = site["api"] + "?ac=videolist&wd=" + urllib.parse.quote(kw)
    hdrs = {"User-Agent": UA, "Referer": site["referer"], "Accept": "*/*"}
    async with session.get(url, headers=hdrs, ssl=False,
                           timeout=aiohttp.ClientTimeout(total=_site_timeout(site))) as resp:
        raw = await resp.read()
    data = _parse_cms(raw)
    return data.get("list") or []


# ------------------------------------------------------- JS（drpy）源适配 ---
async def js_call(fn, *args):
    """drpy 是同步的（QuickJS + 阻塞 HTTP）→ 丢线程池，别卡事件循环"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(JS_EXEC, functools.partial(fn, *args))


def _swallow(fut):
    """取走异常，避免「exception was never retrieved」刷日志"""
    if fut.cancelled():
        return
    try:
        fut.exception()
    except Exception:
        pass


def _js_playable(u):
    """判断 drpy 给的地址是不是客户端能直接播的"""
    u = (u or "").strip()
    low = u.lower()
    if low.startswith(("rtmp://", "rtsp://")):
        return True
    if not low.startswith(("http://", "https://")):
        return False
    return any(k in low for k in (".m3u8", ".mp4", ".flv", ".mkv", ".mp3",
                                  ".m4a", ".aac", ".ts", "m3u8?"))


async def js_query(site, kw, limit=3):
    """一个 JS 源的搜索：search → detail →（必要时）play 免嗅"""
    if jsspider is None:
        return []
    try:
        lst = await js_call(jsspider.search, site, kw, 1)
    except Exception as e:
        log("js search err", site.get("name"), repr(e)[:100])
        return []
    out = []
    for it in lst[:limit]:
        vid = str(it.get("vod_id") or "")
        name = (it.get("vod_name") or "").strip()
        if not vid or not name:
            continue
        try:
            det = await js_call(jsspider.detail, site, vid)
        except Exception as e:
            log("js detail err", site.get("name"), vid[:24], repr(e)[:80])
            det = None
        if not det:
            continue
        eps = parse_episodes(det.get("vod_play_url") or "")
        if not eps:
            continue
        # 免嗅兜底：地址不是直链时问一下站点自己的 play 规则（最多 2 次，别拖死搜索）
        from_ = det.get("vod_play_from") or it.get("vod_play_from") or ""
        tried = 0
        ref_hint = ""
        for ep in eps:
            if tried >= 2:
                break
            if _js_playable(ep.get("url")):
                continue
            tried += 1
            try:
                p = await js_call(jsspider.play, site, from_, ep.get("url") or "")
            except Exception:
                continue
            nu = (p.get("url") or "").strip()
            if _js_playable(nu):
                ep["url"] = nu
                ep["urls"] = [nu]
            # drpy 可能顺带给出防盗链头，取 Referer 覆盖默认值
            hdr = p.get("header") or p.get("headers")
            if isinstance(hdr, str):
                try:
                    hdr = json.loads(hdr)
                except Exception:
                    hdr = None
            if isinstance(hdr, dict) and not ref_hint:
                ref_hint = str(hdr.get("Referer") or hdr.get("referer") or "")
        if not any(_js_playable(e.get("url")) for e in eps):
            continue
        guid = make_guid(site["name"], vid)
        rec = registry.get(guid) or {}
        rec.update({
            "title": name,
            "overview": strip_html(det.get("vod_content") or it.get("vod_content") or ""),
            "pic": (it.get("vod_pic") or det.get("vod_pic") or "").strip(),
            "site": site["name"],
            "referer": ref_hint or site.get("referer") or "",
            "remarks": (it.get("vod_remarks") or det.get("vod_remarks") or "").strip(),
            "eps": eps,
            "resolved_at": int(time.time()),
            "kind": "js",
        })
        registry[guid] = rec
        out.append(to_search_item(guid, rec))
    return out


async def js_search_all(kw, cap=4, timeout=12.0):
    """并行查所有 JS 源（限 cap 个；cap<=0 表示不限），整体超时保护。
    注意：超时只是「不等了」，被丢下的线程还会把这次请求跑完，属正常现象。"""
    sites = [s for s in JS_SITES if s.get("kind") == "js"]
    if cap and cap > 0:
        sites = sites[:cap]
    if not sites or jsspider is None:
        return []
    tasks = [asyncio.ensure_future(js_query(s, kw)) for s in sites]
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
            t.add_done_callback(_swallow)
        raise
    for t in pending:
        t.cancel()
        t.add_done_callback(_swallow)
    if pending:
        log("JS 源有 %d 个没在 %.0fs 内返回，本次先不等" % (len(pending), timeout))
    items = []
    for t in done:
        if t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            log("js search err:", repr(exc)[:140])
            continue
        items.extend(t.result() or [])
    return items


async def query_site(site, kw, sem):
    try:
        async with sem:
            data = await cms_query(site, kw)
        out = []
        # 搜索不限制每个源的条数；前端展示上限由 merge 的 limit 控制。
        for it in data:
            vid = str(it.get("vod_id") or "")
            if not vid:
                continue
            eps = parse_episodes(it.get("vod_play_url") or "")
            if not eps:
                # 搜索结果无播放地址 → 拉详情补全
                det = await cms_detail(site, vid)
                if det:
                    it = {**it, **det}
                    eps = parse_episodes(det.get("vod_play_url") or "")
            if not eps:
                continue
            guid = make_guid(site["name"], vid)
            pic = (it.get("vod_pic") or "").strip()
            rec = registry.get(guid) or {}
            rec.update({
                "title": (it.get("vod_name") or "").strip(),
                "overview": strip_html(it.get("vod_content") or ""),
                "pic": pic,
                "site": site["name"],
                "referer": site["referer"],
                "remarks": (it.get("vod_remarks") or "").strip(),
                "eps": eps,
                "resolved_at": int(time.time()),
            })
            registry[guid] = rec
            out.append(to_search_item(guid, rec))
        return out
    except Exception as e:
        log("online search err", site.get("name"), repr(e)[:120])
        return []

async def online_search(kw, _allow_fallback=True):
    if not kw:
        return []
    qkw = _search_query(kw)
    alt_kw = _fallback_query(qkw)
    h_alias_kw = _hgdj_alias_query(qkw)
    cms_kw = h_alias_kw or qkw
    sem = asyncio.Semaphore(4)
    mode, p_sites, f_sites, js_limit = cms_plan()
    levels = [p_sites + f_sites] if mode == "parallel" else [p_sites, f_sites]

    async def cms_all():
        got = []
        for level in levels:
            tasks = [asyncio.ensure_future(query_site(s, cms_kw, sem))
                     for s in level]
            if not tasks:
                continue
            # 先等快源；慢源（如光速）不阻塞整个搜索。
            # 快源已有结果就直接返回，慢源这次先不等。
            done, pending = await asyncio.wait(tasks, timeout=2.0)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    log("online search err:", repr(exc)[:160])
                    continue
                got.extend(t.result() or [])
            # 快源全空时，再给慢源最多 4 秒，避免“有结果但没等到”。
            # 但如果红果已经有结果，或还能用“去掉第二部”的兜底词再搜，
            # 就不为慢源额外等 4 秒。
            hgdj_ready = (hgdj_task.done() and not hgdj_task.cancelled()
                          and hgdj_task.exception() is None
                          and bool(hgdj_task.result()))
            if not got and pending and not hgdj_ready and not alt_kw:
                done2, pending = await asyncio.wait(pending, timeout=4.0)
                for t in done2:
                    if t.cancelled():
                        continue
                    exc = t.exception()
                    if exc is not None:
                        log("online search err:", repr(exc)[:160])
                        continue
                    got.extend(t.result() or [])
            for t in pending:
                t.cancel()
                t.add_done_callback(_swallow)
            if got and mode == "priority":
                break                      # 主用站有结果就不再等兜底站
        return got

    # 红果是本地缓存搜索，和采集站并发跑，别让本地结果排在 CMS 后面等。
    hkey = _norm_title(qkw)
    h_alias = bool(h_alias_kw) or hkey in ("红果", "红果短剧", "hongguo", "hgdj")
    hgdj_kw = h_alias_kw or qkw
    hlimit = 12 if h_alias else 3

    async def hgdj_search():
        if not hgdj_config()[0]:
            return []
        try:
            return await asyncio.wait_for(
                hgdj_search_items(hgdj_kw, limit=hlimit), timeout=2.0)
        except Exception as e:
            log("hgdj search skip:", repr(e)[:80])
            return []

    hgdj_task = asyncio.ensure_future(hgdj_search())
    cms_task = asyncio.ensure_future(cms_all())
    js_task = (asyncio.ensure_future(js_search_all(qkw, cap=js_limit))
               if JS_SITES else None)
    cms_items = await cms_task

    js_items = []
    if js_task is not None:
        # CMS 已经够用就只给 JS 源一点时间；否则多等一会儿
        wait_s = 2.5 if len(cms_items) >= 4 else 10.0
        done, _pending = await asyncio.wait({js_task}, timeout=wait_s)
        if js_task in done:
            try:
                js_items = js_task.result() or []
            except Exception as e:
                log("js search err:", repr(e)[:140])
        else:
            js_task.add_done_callback(_swallow)
            log("JS 源未在 %.1fs 内返回，本次先不等" % wait_s)

    # 红果短剧：只查本地已缓存的剧集标题，不额外打上游接口。
    # 普通搜索只给红果少量名额，避免把采集站/JS 源的结果挤掉；
    # 只有明确搜“红果/红果短剧”时才多返回红果库条目。
    hgdj_items = []
    try:
        hgdj_items = await hgdj_task
    except Exception as e:
        log("hgdj search skip:", repr(e)[:80])

    # 合并去重：普通搜索把原有采集源放在前面，红果只做补充；
    # 搜“红果”时红果库优先，方便直接进红果条目。
    if h_alias:
        items = _merge_online_items(hgdj_items + cms_items, js_items, 0)
    else:
        items = _merge_online_items(cms_items + hgdj_items, js_items, 0)
    if items:
        save_registry()
        log(f"online search '{kw}' -> {len(items)} 条"
            f"（{mode}，CMS {len(cms_items)} / JS {len(js_items)}"
            f" / HGDJ {len(hgdj_items)}）")
    elif _allow_fallback and alt_kw:
        log(f"online search '{kw}' 无结果，回退搜 '{alt_kw}'")
        return await online_search(alt_kw, _allow_fallback=False)
    return items


# --------------------------------------------------- 标题解析 / 播放源解析 ---
def _search_query(kw):
    """中文关键词去掉输入法带出的空格；英文保留空格。"""
    s = (kw or "").strip()
    if re.search(r"[\u4e00-\u9fff]", s):
        s = re.sub(r"\s+", "", s)
    return s


def _hgdj_alias_query(kw):
    """把“红果 xxx / 红果短剧 xxx”拆成 xxx；没有前缀返回空串。"""
    s = (kw or "").strip()
    low = s.lower()
    for prefix in ("红果短剧", "红果", "hongguo", "hgdj"):
        if low.startswith(prefix):
            return s[len(prefix):].strip(" -_·:：,，").strip()
    return ""


def _fallback_query(kw):
    """精确搜索落空时，去掉“第二部/2”这类后缀再试一次。
    例：“李卫当官 第二部” → “李卫当官”。"""
    t = (kw or "").strip()
    alt = re.sub(r"\s*第\s*[0-9零〇一二两三四五六七八九十百]+\s*[部季集]\s*$",
                 "", t).strip()
    if alt == t:
        alt = re.sub(r"(?<=[\u4e00-\u9fff])\s*[0-9]+\s*$", "", t).strip()
    return alt if alt and alt != t else ""


def _norm_title(s):
    return re.sub(r"[\s\-—:：·\.（）()\[\]【】!！?？'\"《》,，、0-9]", "", s or "").lower()


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2,
              "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_num_to_arabic(text):
    total = section = number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
    return str(total + section + number)


def _norm_hgdj(s):
    """红果标题匹配：中文数字与阿拉伯数字统一，保留数字。
    例：“我本四十岁” / “我本40岁” / “我本40sui” 都能匹配。"""
    t = (s or "").lower()
    t = re.sub(r"(?<=\d)\s*sui\b", "岁", t)
    t = re.sub(r"[零〇一二两三四五六七八九十百千万]+",
               lambda m: _cn_num_to_arabic(m.group(0)), t)
    return re.sub(r"[\s\-—:：·\.（）()\[\]【】!！?？'\"《》,，、]", "", t)


def _merge_online_items(cms_items, js_items, limit=8):
    """按来源轮流取结果；limit<=0 表示不限制条数，全部返回。"""
    groups, order = {}, []
    for it in list(cms_items or []) + list(js_items or []):
        if not isinstance(it, dict):
            continue
        site = str(it.get("ancestor_name") or "").split(" · ", 1)[0].strip()
        site = site or "?"
        if site not in groups:
            groups[site] = []
            order.append(site)
        groups[site].append(it)
    out, seen = [], set()
    while limit <= 0 or len(out) < limit:
        moved = False
        for site in order:
            q = groups.get(site) or []
            while q:
                it = q.pop(0)
                key = _norm_title(it.get("title") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.append(it)
                moved = True
                break
            if limit > 0 and len(out) >= limit:
                break
        if not moved:
            break
    return out


def title_score(a, b):
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.9 + 0.1 * min(len(na), len(nb)) / max(len(na), len(nb))
    return SequenceMatcher(None, na, nb).ratio()


async def _resolve_title(rec):
    """按标题搜索所有采集站，收集全部高相似候选；
    同一集在多个站的地址合并为多线路（供换源使用）。"""
    title = rec.get("title") or ""
    if not title:
        rec["resolved_at"] = int(time.time())
        return
    hits = []          # (score, site, cand)
    mode, p_sites, f_sites, _js_limit = cms_plan()
    levels = [p_sites + f_sites] if mode == "parallel" else [p_sites, f_sites]
    sem = asyncio.Semaphore(4)

    async def query_one(site):
        async with sem:
            try:
                data = await cms_query(site, title)
            except Exception as e:
                log("resolve query err:", site.get("name"), repr(e)[:80])
                return []
            got = []
            for cand in data or []:
                sc = title_score(title, cand.get("vod_name") or "")
                if sc < 0.6:
                    continue
                if not (cand.get("vod_play_url") or ""):
                    det = await cms_detail(site, cand.get("vod_id"))
                    if det:
                        cand = {**cand, **det}
                if (cand.get("vod_play_url") or ""):
                    got.append((sc, site, cand))
            return got

    for idx, level in enumerate(levels):
        if not level:
            continue
        res = await asyncio.gather(*(query_one(s) for s in level),
                                   return_exceptions=True)
        for r in res:
            if isinstance(r, Exception):
                log("resolve search err:", repr(r)[:160])
            else:
                hits.extend(r)
        if hits and mode == "priority":
            break                      # 主用站命中就不再等兜底站
        if mode == "priority" and idx == 0:
            log(f"resolve '{title}' 主用站无果，查兜底站 …")
    if not hits:
        rec["eps"] = rec.get("eps") or []
        rec.setdefault("pic_cms", "-")
        rec["resolved_at"] = int(time.time()) - 86400 + RESOLVE_NEG_TTL
        log(f"resolve '{title}' → 无匹配")
        return

    # 按标题匹配度排序；同分时按用户配置的主用/兜底顺序。
    # 不对任何具体站点做硬编码偏向，避免“没内置源、却内置偏好”的矛盾。
    order = {id(s): i for i, s in enumerate(CMS_SITES)}
    hits.sort(key=lambda x: (-x[0], order.get(id(x[1]), 999)))
    best_score, _bs, best = hits[0][0], None, hits[0][2]
    # 集数最多的候选作为主线路骨架
    main_eps = parse_episodes(best.get("vod_play_url") or "")
    if not main_eps:
        for _sc, _s, c in hits:
            main_eps = parse_episodes(c.get("vod_play_url") or "")
            if main_eps:
                best = c
                break
    if not main_eps:
        rec["eps"] = []
        rec.setdefault("pic_cms", "-")
        rec["resolved_at"] = int(time.time())
        return

    # 把其它候选中「同一集」的地址并入 urls（换源）
    def ep_key(ep):
        """按集名归一，识别「第01集」这类同集"""
        n = re.sub(r"[^0-9一-鿿A-Za-z]", "", ep.get("name") or "")
        m = re.search(r"(\d+)", ep.get("name") or "")
        return ("num", int(m.group(1))) if m else ("name", n)

    for _sc, site, cand in hits:
        if cand is best:
            continue
        eps2 = parse_episodes(cand.get("vod_play_url") or "")
        if not eps2:
            continue
        idx2 = {}
        for e2 in eps2:
            idx2.setdefault(ep_key(e2), e2)
        for i, e1 in enumerate(main_eps):
            e2 = idx2.get(ep_key(e1))
            if not e2:
                continue
            u2 = e2.get("url")
            if not u2:
                continue
            urls = e1.setdefault("urls", [e1["url"]])
            if u2 not in urls:
                urls.append(u2)
            if not e1.get("alt") :
                e1["alt"] = []
            e1["alt"].append({"site": site["name"], "url": u2})

    rec["eps"] = main_eps
    rec["source"] = rec.get("source") or best.get("site_name") or ""

    n_src = len({a["site"] for e in main_eps for a in (e.get("alt") or [])})
    pic = (best.get("vod_pic") or "").strip()
    rec["pic_cms"] = pic if pic.startswith("http") else "-"
    rec["resolved_at"] = int(time.time())
    nlines = sum(1 for e in main_eps if len(e.get("urls") or []) > 1)
    log(f"resolve '{title}' -> '{best.get('vod_name')}' score={best_score:.2f} "
        f"eps={len(main_eps)} 多线集={nlines} 源站={n_src + 1}")


async def ensure_resolved(guid, max_age=86400):
    rec = registry.get(guid)
    if not rec or not isinstance(rec, dict):
        return
    if (rec.get("hgdj") and not rec.get("series")
            and not rec.get("episode_number")):
        await ensure_hgdj_resolved(guid, max_age=HGDJ_DETAIL_TTL)
        return
    if rec.get("eps"):
        # 豆瓣条目若还缺采集站封面（老数据），补跑一次解析
        missing_pic = rec.get("site") == "豆瓣热播" and not rec.get("pic_cms")
        if not missing_pic:
            if not rec.get("resolved_at"):
                rec["resolved_at"] = int(time.time())
                return
            if time.time() - rec["resolved_at"] < max_age:
                return
    t = rec.get("_task")
    if t is not None:
        try:
            await t
        except Exception:
            pass
        return
    task = asyncio.create_task(_resolve_title(rec))
    rec["_task"] = task
    try:
        await task
    finally:
        try:
            rec["_task"] = None
        except Exception:
            pass


# ------------------------------------------------------ 豆瓣榜单(多分类) ---
DOUBAN_TAGS = {"热门": "热门", "豆瓣高分": "豆瓣高分"}


async def fetch_douban_cat(cat_key):
    """抓取单个分类的豆瓣榜单（带 TTL 缓存）"""
    spec = CAT_BY_KEY.get(cat_key)
    if not spec:
        return []
    _k, _name, dtype, tag, limit, _c = spec
    if _douban.get(cat_key) and time.time() - _douban.get(cat_key + "_ts", 0) < DOUBAN_TTL:
        return _douban[cat_key]
    url = ("https://movie.douban.com/j/search_subjects?type=%s&tag=%s"
           "&page_limit=%d&page_start=0"
           % (dtype, urllib.parse.quote(DOUBAN_TAGS.get(tag, tag)), limit))
    out = []
    try:
        async with session.get(
                url, headers={"Referer": "https://movie.douban.com/",
                              "User-Agent": UA}, ssl=False,
                timeout=aiohttp.ClientTimeout(total=12)) as r:
            j = await r.json(content_type=None)
        for s in (j.get("subjects") or [])[:limit]:
            s["_kind"] = dtype
            out.append(s)
    except Exception as e:
        log("douban err", cat_key, repr(e)[:110])
    if out:
        _douban[cat_key] = out
        _douban[cat_key + "_ts"] = time.time()
        log("douban fetched", cat_key, len(out))
    return out or _douban.get(cat_key) or []


async def fetch_douban_all():
    await asyncio.gather(*[fetch_douban_cat(c[0]) for c in DOUBAN_CATS],
                         return_exceptions=True)


async def douban_guids(cat_key=None):
    """把豆瓣条目登记进 registry 并触发后台片源解析"""
    cats = [c for c in DOUBAN_CATS if cat_key is None or c[0] == cat_key]
    allg = {}
    for key, name, _t, _tag, _n, _c in cats:
        items = await fetch_douban_cat(key)
        gl = []
        for s in items:
            g = item_guid_of(key, s.get("id"))
            rec = registry.get(g) or {}
            rate = str(s.get("rate") or "0")
            rec.update({
                "title": s.get("title") or "",
                "overview": s.get("sub") or "",
                "pic": s.get("cover") or "",
                "site": "豆瓣热播", "cat": key,
                "referer": "https://movie.douban.com/",
                "remarks": (("豆瓣 " + rate) if rate else ""),
                "vote": rate,
                "year": s.get("year") or "",
                "release_date": s.get("year") or "",
                "tv": s.get("_kind") == "tv",
                "eps": rec.get("eps") or [],
                "resolved_at": rec.get("resolved_at") or 0,
            })
            registry[g] = rec
            gl.append(g)
            if not rec.get("eps"):
                asyncio.create_task(ensure_resolved(g))
        allg[key] = gl
    save_registry()
    return allg


def douban_cat_guids(cat_key):
    out = []
    for g, r in registry.items():
        if isinstance(r, dict) and r.get("cat") == cat_key \
                and r.get("site") == "豆瓣热播":
            out.append(g)
    return out


def fake_mdb_info(cat_key):
    """单个伪媒体库的完整信息（mdb/:guid 与 /item/<库guid> 用）"""
    spec = CAT_BY_KEY.get(cat_key)
    if not spec:
        return None
    _k, name, _t, _tag, _n, cat = spec
    guids = douban_cat_guids(cat_key)
    covers = []
    for g in guids:
        c = (registry.get(g) or {}).get("pic_cms") or ""
        if c and c not in covers:
            covers.append(c)
        if len(covers) >= 6:
            break
    n = len(guids)
    return {
        "guid": LIB_GUIDS[cat_key], "name": name, "title": name,
        "category": cat, "dir_list": [], "lan": "zh-CN",
        "auto_scrap_subtitle": 0, "include_adult": False,
        "subtitle_lan": "zh-CN",
        "poster": covers[0] if covers else "", "poster_type": 0,
        "posters": covers, "view_type": 0, "refresh_disabled": True,
        "prefer_local_nfo": 0, "skip_filesize": 1, "auto_progress_thumb": 0,
        "last_update_time": int(time.time()), "sort_num": 0,
        # 以下为 /item/<库guid> 需要的字段（前端按条目模型渲染）
        "type": cat, "number_of_episodes": n, "number_of_seasons": 1,
        "season_number": 0, "episode_number": 0, "overview": "",
        "vote_average": "0", "genres": [], "content_ratings": "",
        "runtime": 0, "is_favorite": 0, "is_watched": 0, "watched_ts": 0,
        "can_play": 1, "play_error": "", "parent_guid": "",
        "ancestor_guid": "", "ancestor_name": "", "ancestor_category": cat,
        "trim_id": "", "original_title": "", "play_item_guid": "",
        "duration": 0, "logic_type": 0, "backdrops": covers[0] if covers else "",
        "markedForDeletion": False,
        "media_stream": {"resolutions": ["1080p"], "audio_type": ["Stereo"],
                         "color_range_type": ["SDR"]},
    }


def fake_mdb_list():
    """所有伪库的列表项（mediadb/list 注入）"""
    out = []
    for c in DOUBAN_CATS:
        info = fake_mdb_info(c[0])
        if info:
            out.append(info)
    if hgdj_config()[0]:
        out.append(hgdj_lib_info())
    return out


# --------------------------------------------------------- 红果短剧 (HGDJ) ---
def hgdj_config():
    """读取 HGDJ 开关/地址；2 秒缓存，配置页保存后无需重启。"""
    now = time.time()
    if now - _HGDJ_CFG["ts"] < 2.0:
        return _HGDJ_CFG["enabled"], _HGDJ_CFG["base"]
    cfg = load_config()
    enabled = bool(cfg.get("hgdj_enabled"))
    base = str(cfg.get("hgdj_url") or HGDJ_DEFAULT_URL).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = HGDJ_DEFAULT_URL
    _HGDJ_CFG.update({"ts": now, "enabled": enabled, "base": base})
    return enabled, base


def hgdj_guid_of(series_id):
    return "on" + hashlib.md5(
        ("fnv-hgdj-" + str(series_id)).encode()).hexdigest()[:30]


def hgdj_ep_num(rec):
    """当前条目对应的 HGDJ 集数（1 起）。"""
    try:
        n = int(rec.get("hgdj_ep") or rec.get("episode_number") or 0)
    except Exception:
        n = 0
    if n:
        return n
    eps = rec.get("eps") or []
    if eps and isinstance(eps[0], dict):
        try:
            return int(eps[0].get("hgdj_ep") or 1)
        except Exception:
            return 1
    return 1


def hgdj_is_rec(rec):
    if not isinstance(rec, dict):
        return False
    if rec.get("hgdj"):
        return True
    srec = registry.get(rec.get("series") or "") or {}
    return bool(isinstance(srec, dict) and srec.get("hgdj"))


def hgdj_raw_rec(raw):
    """把 HGDJ 的 series 元数据转成 registry 记录。"""
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("series_id") or raw.get("seriesId") or "").strip()
    if not sid:
        return None
    title = str(raw.get("series_name") or raw.get("name") or "").strip()
    cover = str(raw.get("series_cover") or raw.get("cover") or "").strip()
    intro = str(raw.get("series_intro") or raw.get("intro") or "").strip()
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    try:
        cnt = int(raw.get("episode_cnt") or 0)
    except Exception:
        cnt = 0
    cnt = max(0, min(cnt, 1000))
    eps = [{"name": f"第{i}集", "hgdj_ep": i} for i in range(1, cnt + 1)]
    if not eps:
        eps = [{"name": "第1集", "hgdj_ep": 1}]
    return {
        "title": title or "红果短剧",
        "overview": intro,
        "pic": cover, "pic_cms": cover,
        "site": HGDJ_LIB_NAME, "hgdj": True, "hgdj_sid": sid,
        "hgdj_resolved": bool(cnt), "hgdj_last_try": 0,
        "tv": True, "eps": eps, "resolved_at": int(time.time()),
        "tags": tags, "year": 2026,
    }


def _hgdj_merge(raw_items):
    added = 0
    for raw in raw_items or []:
        rec = hgdj_raw_rec(raw)
        if not rec:
            continue
        g = hgdj_guid_of(rec["hgdj_sid"])
        old = registry.get(g)
        if isinstance(old, dict) and old.get("hgdj"):
            # 已解析过的详情优先，别被首页/分类的短元数据覆盖
            if len(old.get("eps") or []) > len(rec.get("eps") or []):
                rec["eps"] = old["eps"]
                rec["hgdj_resolved"] = old.get("hgdj_resolved")
            for k in ("title", "overview", "pic", "pic_cms", "tags"):
                if not rec.get(k) and old.get(k):
                    rec[k] = old[k]
        else:
            added += 1
        registry[g] = rec
        if g not in _HGDJ["order"]:
            _HGDJ["order"].append(g)
        _HGDJ["items"][g] = rec
    return added


async def _hgdj_json(path, timeout=15):
    enabled, base = hgdj_config()
    if not enabled or session is None:
        return None
    try:
        async with session.get(
                base + path, ssl=False,
                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception as e:
        log("hgdj api err", path[:80], repr(e)[:100])
        return None


def _hgdj_total():
    """HGDJ 上游分页给出的总量；未拿到时退回已加载数量。"""
    n = len(hgdj_series_guids())
    try:
        total = sum(int(x or 0) for x in (_HGDJ.get("totals") or {}).values())
    except Exception:
        total = 0
    return max(n, total)


async def _hgdj_page(cat, page):
    j = await _hgdj_json(
        f"/api/browse?category={urllib.parse.quote(cat)}&page={int(page)}",
        20)
    return j if isinstance(j, dict) else None


def _hgdj_absorb_page(cat, j):
    if not isinstance(j, dict):
        return 0
    p = j.get("pagination") or {}
    try:
        total = int(p.get("total") or 0)
        pages = int(p.get("totalPages") or 0)
    except Exception:
        total, pages = 0, 0
    if total:
        _HGDJ["totals"][cat] = total
    if pages:
        _HGDJ["total_pages"][cat] = pages
    added = _hgdj_merge(j.get("items") or [])
    return added


async def _fetch_hgdj_all():
    # 第一屏：推荐 + 四个分类的第 1 页。后续页在用户滚动时按需加载，
    # 避免一次性打 100+ 个上游请求拖慢媒体库首屏。
    got = await asyncio.gather(
        _hgdj_json("/api/recommend", 20),
        *[_hgdj_page(c, 1) for c in HGDJ_CATS],
        return_exceptions=True)
    added = 0
    if isinstance(got[0], dict):
        added += _hgdj_merge(got[0].get("items") or [])
    for cat, j in zip(HGDJ_CATS, got[1:]):
        added += _hgdj_absorb_page(cat, j)
        if cat not in _HGDJ["next_page"]:
            _HGDJ["next_page"][cat] = 2
    _HGDJ["ts"] = time.time()
    _HGDJ["all_loaded"] = all(
        _HGDJ["next_page"].get(c, 2) > _HGDJ["total_pages"].get(c, 0)
        for c in HGDJ_CATS)
    if added:
        save_registry(force=True)
    log(f"hgdj library: {len(_HGDJ['order'])} 部 / 上游约 {_hgdj_total()} 部"
        f"（新增 {added}）")
    # 首页条目的 episode_cnt 常为 0，后台补一次详情，让库列表尽快显示真实集数
    pending = [g for g in hgdj_series_guids()
               if not registry[g].get("hgdj_resolved")
               and len(registry[g].get("eps") or []) <= 1]
    if pending:
        asyncio.create_task(_hgdj_prefetch(pending[:60]))
    # 首屏返回后再在后台把剩余分类页补齐，滚动/搜索时就有完整片库。
    if not _HGDJ.get("all_loaded"):
        asyncio.create_task(ensure_hgdj_loaded(_hgdj_total()))
    return hgdj_series_guids()


async def _hgdj_load_more():
    """每个分类各取下一页；返回新增剧集数。"""
    if _HGDJ.get("all_loaded"):
        return 0
    cats = [c for c in HGDJ_CATS
            if _HGDJ["next_page"].get(c, 2)
            <= _HGDJ["total_pages"].get(c, 0)]
    if not cats:
        _HGDJ["all_loaded"] = True
        return 0
    got = await asyncio.gather(
        *[_hgdj_page(c, _HGDJ["next_page"].get(c, 2)) for c in cats],
        return_exceptions=True)
    added = 0
    for cat, j in zip(cats, got):
        added += _hgdj_absorb_page(cat, j)
        _HGDJ["next_page"][cat] = _HGDJ["next_page"].get(cat, 2) + 1
    _HGDJ["all_loaded"] = all(
        _HGDJ["next_page"].get(c, 2) > _HGDJ["total_pages"].get(c, 0)
        for c in HGDJ_CATS)
    if added:
        save_registry(force=True)
    return added


async def ensure_hgdj_loaded(count, deadline=None):
    """确保已加载至少 count 部；滚动/搜索时再按页拉取。"""
    if count <= len(hgdj_series_guids()):
        return
    t = _HGDJ.get("load_task")
    if t is not None and not t.done():
        # 后台补全任务可能还要跑几十秒，不能把当前分页请求一起拖住；
        # 等一小会儿拿到最新进度即可，前端下一次滚动会再取。
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=1.2)
        except Exception:
            pass
        return
    async def run():
        rounds = 0
        while (len(hgdj_series_guids()) < count
               and not _HGDJ.get("all_loaded") and rounds < 40):
            if deadline is not None and time.monotonic() >= deadline:
                break
            n = await _hgdj_load_more()
            rounds += 1
            if n == 0 and _HGDJ.get("all_loaded"):
                break
    t = asyncio.create_task(run())
    _HGDJ["load_task"] = t
    try:
        await t
    except Exception as e:
        log("hgdj load more err:", repr(e)[:100])
    finally:
        if _HGDJ.get("load_task") is t:
            _HGDJ["load_task"] = None


async def _hgdj_prefetch(guids):
    sem = asyncio.Semaphore(3)

    async def one(g):
        async with sem:
            try:
                await ensure_hgdj_resolved(g)
            except Exception as e:
                log("hgdj prefetch err:", repr(e)[:90])

    await asyncio.gather(*[one(g) for g in guids], return_exceptions=True)


async def fetch_hgdj_all(force=False):
    enabled, _ = hgdj_config()
    if not enabled:
        return []
    if (not force and _HGDJ["order"]
            and time.time() - _HGDJ["ts"] < HGDJ_TTL):
        return list(_HGDJ["order"])
    t = _HGDJ.get("task")
    if t is not None and not t.done():
        try:
            return await t
        except Exception:
            return list(_HGDJ["order"])
    t = asyncio.create_task(_fetch_hgdj_all())
    _HGDJ["task"] = t
    try:
        return await t
    except Exception as e:
        log("hgdj fetch err:", repr(e)[:120])
        return list(_HGDJ["order"])
    finally:
        if _HGDJ.get("task") is t:
            _HGDJ["task"] = None


def hgdj_series_guids():
    out = []
    for g in _HGDJ["order"]:
        rec = registry.get(g)
        if (isinstance(rec, dict) and rec.get("hgdj")
                and not rec.get("series")
                and not rec.get("episode_number")):
            out.append(g)
    return out


def hgdj_lib_info():
    guids = hgdj_series_guids()
    covers = []
    for g in guids:
        c = (registry.get(g) or {}).get("pic_cms") or ""
        if c and c not in covers:
            covers.append(c)
        if len(covers) >= 6:
            break
    n = _hgdj_total() if guids else 0
    return {
        "guid": HGDJ_LIB_GUID, "name": HGDJ_LIB_NAME, "title": HGDJ_LIB_NAME,
        "category": "TV", "dir_list": [], "lan": "zh-CN",
        "auto_scrap_subtitle": 0, "include_adult": False,
        "subtitle_lan": "zh-CN",
        "poster": covers[0] if covers else "", "poster_type": 0,
        "posters": covers, "view_type": 0, "refresh_disabled": True,
        "prefer_local_nfo": 0, "skip_filesize": 1, "auto_progress_thumb": 0,
        "last_update_time": int(time.time()), "sort_num": 0,
        "type": "TV", "number_of_episodes": n, "number_of_seasons": 1,
        "season_number": 0, "episode_number": 0, "overview": "",
        "vote_average": "0", "genres": [], "content_ratings": "",
        "runtime": 0, "is_favorite": 0, "is_watched": 0, "watched_ts": 0,
        "can_play": 1, "play_error": "", "parent_guid": "",
        "ancestor_guid": "", "ancestor_name": "", "ancestor_category": "TV",
        "trim_id": "", "original_title": "", "play_item_guid": "",
        "duration": 0, "logic_type": 0,
        "backdrops": covers[0] if covers else "",
        "markedForDeletion": False,
        "media_stream": {"resolutions": ["1080p"], "audio_type": ["Stereo"],
                         "color_range_type": ["SDR"]},
        "isDirectLinkEnabled": 1,
    }


async def ensure_hgdj_resolved(guid, max_age=HGDJ_DETAIL_TTL):
    rec = registry.get(guid)
    if (not isinstance(rec, dict) or not rec.get("hgdj")
            or rec.get("series") or rec.get("episode_number")):
        return
    if (rec.get("hgdj_resolved")
            and time.time() - int(rec.get("resolved_at") or 0) < max_age):
        return
    if time.time() - int(rec.get("hgdj_last_try") or 0) < 30:
        return
    t = rec.get("_hgdj_task")
    if t is not None:
        try:
            await t
        except Exception:
            pass
        return
    task = asyncio.create_task(_resolve_hgdj(rec))
    rec["_hgdj_task"] = task
    try:
        await task
    finally:
        rec.pop("_hgdj_task", None)


async def _resolve_hgdj(rec):
    sid = str(rec.get("hgdj_sid") or "").strip()
    if not sid:
        return
    rec["hgdj_last_try"] = int(time.time())
    j = await _hgdj_json(f"/api/series/{urllib.parse.quote(sid)}", timeout=60)
    if not isinstance(j, dict):
        return
    d = j.get("detail") or {}
    vids = d.get("vid_list") or []
    eps = []
    for i, vid in enumerate(vids, 1):
        eps.append({"name": f"第{i}集", "hgdj_ep": i, "hgdj_vid": vid})
    if not eps:
        try:
            cnt = int(d.get("episode_cnt") or 0)
        except Exception:
            cnt = 0
        eps = [{"name": f"第{i}集", "hgdj_ep": i}
               for i in range(1, max(1, min(cnt, 1000)) + 1)]
    rec.update({
        "title": str(d.get("series_name") or rec.get("title") or ""),
        "overview": str(d.get("series_intro") or rec.get("overview") or ""),
        "pic": str(d.get("series_cover") or rec.get("pic") or ""),
        "pic_cms": str(d.get("series_cover") or rec.get("pic_cms") or ""),
        "tags": d.get("tags") or rec.get("tags") or [],
        "eps": eps, "tv": True,
        "hgdj_resolved": True, "resolved_at": int(time.time()),
    })
    g = hgdj_guid_of(sid)
    sg = season_guid_of(g)
    if sg in registry:
        registry[sg]["eps"] = eps
        registry[sg]["resolved_at"] = int(time.time())
    save_registry()
    log(f"hgdj resolved: {rec.get('title')} ({len(eps)} 集)")


async def serve_hgdj_media(guid, ep, headers, writer):
    """把 HGDJ 的 /api/play 解密流原样代理给播放器（支持 Range）。"""
    enabled, base = hgdj_config()
    if not enabled:
        return False
    rec = registry.get(guid) or {}
    srec = rec if rec.get("hgdj") else (registry.get(rec.get("series") or "") or {})
    sid = str(srec.get("hgdj_sid") or "").strip()
    if not sid:
        return False
    try:
        ep = int(ep or hgdj_ep_num(rec) or 1)
    except Exception:
        ep = 1
    url = f"{base}/api/play/{urllib.parse.quote(sid)}/{ep}"
    hdrs = {}
    if headers.get("range"):
        hdrs["Range"] = headers["range"]
    try:
        async with session.get(
                url, headers=hdrs, ssl=False,
                timeout=aiohttp.ClientTimeout(
                    total=240, sock_connect=10, sock_read=240)) as r:
            out = {}
            for k in ("Content-Type", "Content-Length", "Content-Range",
                      "Accept-Ranges", "Cache-Control", "ETag",
                      "Last-Modified"):
                if r.headers.get(k):
                    out[k] = r.headers[k]
            if not out.get("Content-Type"):
                out["Content-Type"] = "video/mp4"
            out["Connection"] = "close"
            if r.status >= 400:
                body = await r.read()
                out["Content-Type"] = "application/json"
                out["Content-Length"] = str(len(body))
                await write_head(writer, r.status, out)
                writer.write(body)
                await writer.drain()
                return True
            await write_head(writer, r.status, out)
            async for chunk in r.content.iter_chunked(65536):
                writer.write(chunk)
                await writer.drain()
        return True
    except (BrokenPipeError, ConnectionResetError):
        return True
    except Exception as e:
        log("hgdj media err:", repr(e)[:140])
        return False


def hgdj_abs_url(path, headers=None):
    """原生播放器（trim_player）不一定会解析相对直链，这里按请求 Host 补全。"""
    h = headers or {}
    host = str(h.get("x-forwarded-host") or h.get("host") or "").strip()
    if not host:
        return path
    proto = str(h.get("x-forwarded-proto") or "").strip().lower()
    try:
        parsed = urllib.parse.urlsplit("//" + host)
        port = parsed.port
    except Exception:
        parsed = None
        port = None
    if proto not in ("http", "https"):
        # nginx 的 /v location 没传 X-Forwarded-Proto；按端口推断。
        proto = "https" if port in (443, 5667) else "http"
    # 关键：nginx 配置里是 `proxy_set_header Host $host;`，$host 会丢掉端口。
    # iOS 客户端拿到 http://[IPv6]/... 后会去连 80 端口，直接报 STRM 直链异常。
    # 因此 Host 没带端口时，按飞牛默认 Web 端口补齐。
    if port is None:
        xport = str(h.get("x-forwarded-port") or "").strip()
        if xport.isdigit():
            port = int(xport)
        else:
            port = 5667 if proto == "https" else 5666
    if parsed is not None and parsed.port is None:
        host = f"{host}:{port}"
    return f"{proto}://{host}{path}"


def hgdj_synth_stream(guid, rec, headers=None):
    now = int(time.time() * 1000)
    ep = hgdj_ep_num(rec)
    direct = hgdj_abs_url(f"/v/media/hgdj/{guid}.mp4?ep={ep}", headers)
    log("HGDJ-STREAM", direct[:160],
        "host=" + str((headers or {}).get("host") or "-"),
        "proto=" + str((headers or {}).get("x-forwarded-proto") or "-"),
        "ua=" + str((headers or {}).get("user-agent") or "-")[:40],
        "ref=" + str((headers or {}).get("referer") or "-")[:60])
    slug = re.sub(r"[^\w一-鿿.-]+", "_", rec.get("title", "hgdj"))[:60]
    dur = int(rec.get("duration") or 0)
    file_stream = {
        "guid": guid, "path": f"/hgdj/{slug}.mp4",
        "file_name": f"{slug}.mp4", "size": 0, "timestamp": now,
        "type": 1, "sort_num": 0, "can_play": 1, "play_error": "",
        "create_time": now, "update_time": now, "parent_fv_guid": "",
        "file_birth_time": now, "progress_thumb_hash_dir": "",
    }
    video_stream = {
        "media_guid": guid, "title": "",
        "guid": "onv" + guid[2:][:29],
        "resolution_type": "1080p", "color_range_type": "SDR",
        "codec_name": "hevc", "codec_type": "video",
        "color_range": "", "profile": "Main", "index": 0,
        "width": 1080, "height": 1920,
        "coded_width": 1080, "coded_height": 1920,
        "display_aspect_ratio": "9:16", "pix_fmt": "yuv420p",
        "level": "120", "color_space": "bt709",
        "color_transfer": "bt709", "color_primaries": "bt709",
        "duration": dur, "dv_profile": "", "refs": 1,
        "r_frame_rate": "25/1", "avg_frame_rate": "25/1",
        "bits_per_raw_sample": "8", "bps": 4000000, "progressive": 1,
        "bit_depth": 8, "wrapper": "mp4", "create_time": now,
        "update_time": now, "rotation": 0, "ext1": "", "is_bluray": 0,
    }
    audio_streams = [{
        "media_guid": guid, "title": "",
        "guid": "ona" + guid[2:][:29],
        "audio_type": "Stereo", "codec_name": "aac",
        "codec_type": "audio", "language": "und", "channels": 2,
        "profile": "LC", "sample_rate": "44100", "is_default": 1,
        "channel_layout": "stereo", "duration": dur, "index": 1,
        "bits_per_raw_sample": "16", "bps": 128000,
        "create_time": now, "update_time": now, "is_fake": 0,
    }]
    qualities = [{"bitrate": 0, "resolution": "1080p",
                  "progressive": True, "is_m3u8": False, "url": direct}]
    return {
        "msg": "", "code": 0,
        "data": {
            "file_stream": file_stream, "video_stream": video_stream,
            "audio_streams": audio_streams, "qualities": qualities,
            "direct_link_qualities": qualities,
            # 不要用 9001(STRM)：飞牛影视会走 STRM 权限/直链校验，原生播放器
            # 对相对地址或非网盘直链容易直接报 -99985。用阿里云盘直连类型，
            # 前端仍会按 RawFile 播放，但不再触发 STRM 专用错误。
            "cloud_storage_info": {"cloud_storage_type": 2},
            "isDirectLinkEnabled": 1,
        },
    }


def hgdj_synth_play(guid, rec, headers=None):
    ep = hgdj_ep_num(rec)
    direct = hgdj_abs_url(f"/v/media/hgdj/{guid}.mp4?ep={ep}", headers)
    log("HGDJ-PLAY", direct[:160])
    return {
        "msg": "", "code": 0,
        "data": {
            "audio_guid": "ona" + guid[2:][:29], "audio_index": 1,
            "hls_time": 0, "is_subtitle_external": 0,
            "media_guid": guid, "play_link": direct, "line_count": 1,
            "subtitle_guid": "", "subtitle_index": -1, "subtitle_link": "",
            "video_guid": "onv" + guid[2:][:29], "video_index": 0,
            "non_fatal_errno": None, "video_encoder": "",
            "not_supported_resolution": "",
            "supported_highest_resolution": "",
            "isDirectLinkEnabled": 1,
            "quality": [{"bitrate": 0, "resolution": "1080p",
                         "progressive": True, "is_m3u8": False,
                         "url": direct}],
        },
    }


# --------------------------------------------------------- 伪造响应构造 ---
def _item_poster(rec):
    """封面选择：采集站封面优先（无防盗链）；豆瓣封面有 Referer 防盗链且
    前端只会原样使用 http 开头地址，未解析出采集站封面时留空走前端占位图。"""
    pic = (rec.get("pic_cms") or "").strip()
    if pic.startswith("http"):
        return pic
    if rec.get("site") == "豆瓣热播":
        return ""
    pic = (rec.get("pic") or "").strip()
    return pic if pic.startswith("http") else (rec.get("pic_proxy") or "")


def build_item(guid, rec):
    eps = rec.get("eps") or []
    n = len(eps)
    series = rec.get("series")
    counts_tv = n > 1 or bool(rec.get("tv"))      # 是否按“剧集结构”给计数
    is_ep = bool(rec.get("episode_number"))
    is_season = bool(rec.get("season_marker"))
    if is_ep:
        typ = "Episode"
    elif is_season:
        typ = "Season"
    else:
        typ = "TV" if counts_tv else "Movie"
    poster = _item_poster(rec)
    return {
        "guid": guid, "trim_id": "", "tv_title": "", "parent_title": "",
        "title": rec.get("title", ""),
        "original_title": rec.get("title", ""),
        "backdrops": poster, "posters": poster, "poster": poster,
        "vote_average": str(rec.get("vote") or "0"),
        "vote_count": 0,
        "year": rec.get("year") or 0,
        "release_date": rec.get("release_date") or "",
        "first_air_date": rec.get("release_date") or "",
        "genres": [],
        "tags": [],
        "content_ratings": "",
        # 时长兜底：前端靠它判断条目可播放，为 0 会卡在「视频加载中」
        "runtime": int(rec.get("runtime") or rec.get("duration")
                       or (2700 if is_ep else 5400)),
        "adult": 0,
        "logos": "",
        "still_path": "",
        "overview": (rec.get("overview", "")
                     or ("" if eps else "该影片在全部片源中暂无可播放资源。")),
        "is_favorite": 0, "is_watched": 0, "watched_ts": 0,
        "season_number": 1 if (counts_tv or is_ep or is_season) else 0,
        "episode_number": int(rec.get("episode_number") or 0),
        "number_of_episodes": n if counts_tv else 0,
        "local_number_of_episodes": n if counts_tv else 0,
        "local_number_of_seasons": 1 if counts_tv else 0,
        "number_of_seasons": 1 if counts_tv else 0,
        # 无片源条目明确标记，避免前端给出播放入口后永远「视频加载中」
        "can_play": 0 if not eps else 1, "type": typ,
        "play_error": "" if eps else "暂无片源",
        "parent_guid": (season_guid_of(series) if (is_ep and series)
                        else (series if is_season else "")),
        "ancestor_guid": "", "ancestor_name": rec.get("site", ""),
        "ancestor_category": "Movie" if typ == "Movie" else "TV",
        "play_item_guid": "", "logic_type": 0,
        # 红果短剧走直链 MP4，让前端按 RawFile 播放而不是拼 m3u8
        "isDirectLinkEnabled": 1 if hgdj_is_rec(rec) else 0,
        # 以下字段搜索结果里有、媒体库条目原本没有；前端靠它们决定
        # 是否先走 play/info + stream 再 play/play，缺失会直接卡在「视频加载中」
        "lan": "zh-CN", "imdb_id": "", "file_name": "", "ts": 0,
        "watched": 0, "status": "Returning" if counts_tv else "Ended",
        "single_child_guid": "" if is_season else guid,
        # media_guid / 时长：前端详情页与播放入口依赖，缺失会导致「点开不播」
        "media_guid": rec.get("mg") or ("onm" + guid[2:][:29]),
        "duration": int(rec.get("duration") or rec.get("runtime")
                        or (2700 if is_ep else 5400)),
        "media_stream": {"resolutions": ["1080p"],
                         "audio_type": ["Stereo"],
                         "color_range_type": ["SDR"]},
    }


def to_search_item(guid, rec):
    eps = rec.get("eps") or []
    n = len(eps)
    is_tv = n > 1 or bool(rec.get("tv"))
    site_remarks = rec.get("site", "") + (
        (" · " + rec["remarks"]) if rec.get("remarks") else "")
    return {
        "guid": guid, "lan": "zh-CN", "imdb_id": "", "tv_title": "",
        "parent_guid": "", "parent_title": "",
        "title": rec.get("title", ""),
        "type": "TV" if is_tv else "Movie",
        "genres": [],
        "poster": _item_poster(rec),
        "runtime": 0,
        "is_favorite": 0, "watched": 0, "watched_ts": 0,
        "vote_average": str(rec.get("vote") or "0"),
        "media_stream": {"resolutions": ["1080p"], "audio_type": None,
                         "color_range_type": None},
        "season_number": 1 if is_tv else 0,
        "episode_number": 1 if is_tv else 0,
        "number_of_seasons": 1 if is_tv else 0,
        "number_of_episodes": n if is_tv else 0,
        "local_number_of_seasons": 1 if is_tv else 0,
        "local_number_of_episodes": n if is_tv else 0,
        "status": "1",
        "overview": rec.get("overview", ""),
        "ancestor_guid": "",
        "ancestor_name": site_remarks,
        "ancestor_category": "Movie" if not is_tv else "TV",
        "ts": 0, "duration": 0, "single_child_guid": "",
        "file_name": "",
    }


async def hgdj_search_items(kw, limit=12):
    """在本地红果库里按标题找；简介不再参与，避免无关结果挤占搜索。"""
    enabled, _ = hgdj_config()
    if not enabled or not kw:
        return []
    if not _HGDJ["order"]:
        try:
            await asyncio.wait_for(fetch_hgdj_all(), timeout=3.0)
        except Exception:
            pass
    key = _norm_hgdj(kw)
    if not key:
        return []

    # 搜“红果/红果短剧”时直接把红果库条目作为结果，否则用户会以为
    # 这个媒体库不能被搜索。普通查询只按标题匹配，避免简介里的词
    # 把一堆不相干的红果剧集塞进搜索结果。
    if key in ("红果", "红果短剧", "hongguo", "hgdj"):
        return [to_search_item(g, registry.get(g) or {})
                for g in hgdj_series_guids()[:limit]]
    out = []
    for g in hgdj_series_guids():
        rec = registry.get(g) or {}
        hay = _norm_hgdj(rec.get("title") or "")
        if not hay or key not in hay:
            continue
        out.append(to_search_item(g, rec))
        if len(out) >= limit:
            break
    return out


def synth_play_info(guid, rec):
    vg = "onv" + guid[2:][:29]
    ag = "ona" + guid[2:][:29]
    rec["vg"], rec["ag"] = vg, ag          # 供路径反查
    # parent_guid：剧集/单集要指回上级，否则前端详情页跳转会断链
    _series = rec.get("series") or ""
    _parent = ""
    if rec.get("episode_number"):
        _parent = season_guid_of(_series) if _series else ""
    elif rec.get("season_marker"):
        _parent = _series
    _w = watch.get(guid) or {}
    return {
        "msg": "", "code": 0,
        "data": {
            "grand_guid": _parent, "guid": guid, "parent_guid": _parent,
            "play_config": None,
            "ts": int(_w.get("ts") or 0),      # 续播：上次播放位置
            "type": "Movie",
            "video_guid": vg, "audio_guid": ag, "subtitle_guid": "",
            "media_guid": guid,
            "item": build_item(guid, rec),
            "direct_link_audio_index": -1,
            "isDirectLinkEnabled": 1 if hgdj_is_rec(rec) else 0,
        },
    }


def synth_stream(guid, headers=None):
    now = int(time.time() * 1000)
    rec = registry.get(guid, {})
    if hgdj_is_rec(rec):
        return hgdj_synth_stream(guid, rec, headers)
    slug = re.sub(r"[^\w一-鿿.-]+", "_", rec.get("title", "online"))[:60]
    # 实测流参数（ffprobe 探分片得到，见 probe_props）：拿不到就用兜底值
    pr = props_of_rec(rec) or {}
    try:
        w = int(pr.get("width") or 1920)
        h = int(pr.get("height") or 1080)
    except Exception:
        w, h = 1920, 1080
    # 时长（秒）：_probe_duration 已按清单 EXTINF 合计缓存进 registry。
    # 报 0 时播放器不显示总时长、进度条也不动（实测观看记录里位置一直是 0~6s）。
    try:
        dur = int(rec.get("duration") or 0)
    except Exception:
        dur = 0
    try:
        ch = int(pr.get("channels") or 2)
    except Exception:
        ch = 2
    a_type = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch, "Stereo")
    a_layout = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(ch, "stereo")
    aspect = aspect_label(w, h)
    file_stream = {
        "guid": guid, "path": f"/online/{slug}.m3u8",
        "file_name": f"{slug}.m3u8", "size": 0, "timestamp": now,
        "type": 1, "sort_num": 0, "can_play": 1, "play_error": "",
        "create_time": now, "update_time": now, "parent_fv_guid": "",
        "file_birth_time": now, "progress_thumb_hash_dir": "",
    }
    video_stream = {
        "media_guid": guid, "title": "",
        "guid": "onv" + guid[2:][:29],
        "resolution_type": res_label(w, h), "color_range_type": "SDR",
        "codec_name": pr.get("codec") or "h264", "codec_type": "video",
        "color_range": "",
        "profile": pr.get("profile") or "High", "index": 0,
        "width": w, "height": h,
        "coded_width": w, "coded_height": h,
        "display_aspect_ratio": aspect, "pix_fmt": "yuv420p",
        "level": str(pr.get("level") or 40), "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709", "duration": dur, "dv_profile": "",
        "refs": 1, "r_frame_rate": "24/1", "avg_frame_rate": "24/1",
        "bits_per_raw_sample": "8", "bps": 4000000, "progressive": 1,
        "bit_depth": 8, "wrapper": "hls", "create_time": now,
        "update_time": now, "rotation": 0, "ext1": "", "is_bluray": 0,
    }
    audio_streams = [{
        "media_guid": guid, "title": "",
        "guid": "ona" + guid[2:][:29],
        "audio_type": a_type, "codec_name": pr.get("acodec") or "aac",
        "codec_type": "audio",
        "language": "und", "channels": ch, "profile": "LC",
        "sample_rate": "44100", "is_default": 1, "channel_layout": a_layout,
        "duration": dur, "index": 1, "bits_per_raw_sample": "16",
        "bps": 128000, "create_time": now, "update_time": now,
        "is_fake": 0,
    }]
    qualities = [{"bitrate": 2000000, "resolution": str(h),
                  "progressive": False, "is_m3u8": True}]
    return {"msg": "", "code": 0,
            "data": {"file_stream": file_stream, "video_stream": video_stream,
                     "audio_streams": audio_streams, "qualities": qualities,
                     "cloud_storage_info": None}}


def synth_play(guid, ep_index=0, headers=None):
    _rec = registry.get(guid) or {}
    if hgdj_is_rec(_rec):
        return hgdj_synth_play(guid, _rec, headers)
    try:
        _lines = len(build_line_index(guid, _rec, ep_index))
    except Exception:
        _lines = 1
    return {
        "msg": "", "code": 0,
        "data": {
            "audio_guid": "ona" + guid[2:][:29], "audio_index": 1,
            "hls_time": 4, "is_subtitle_external": 0,
            "media_guid": guid,
            "play_link": f"/v/media/online/{guid}.m3u8?ep={ep_index}",
            "line_count": _lines,
            "subtitle_guid": "", "subtitle_index": -1, "subtitle_link": "",
            "video_guid": "onv" + guid[2:][:29], "video_index": 0,
            "non_fatal_errno": None, "video_encoder": "",
            "not_supported_resolution": "",
            "supported_highest_resolution": "",
        },
    }


def synth_seasons(guid, rec):
    eps = rec.get("eps") or []
    n = len(eps)
    if not (n > 1 or rec.get("tv")):
        return []
    sg = season_guid_of(guid)
    if sg not in registry:
        registry[sg] = {
            "title": rec.get("title", ""), "series": guid, "eps": eps,
            "pic": rec.get("pic", ""), "site": rec.get("site", ""),
            "referer": rec.get("referer", ""),
            "overview": rec.get("overview", ""),
            "season_marker": True, "resolved_at": int(time.time()),
        }
        save_registry()
    s = build_item(sg, registry[sg])
    s.update({
        "type": "Season", "title": "第一季", "original_title": "第一季",
        "season_number": 1, "number_of_episodes": n,
        "number_of_seasons": 1, "parent_guid": guid, "episode_number": 0,
    })
    return [s]


def synth_episodes(g, rec):
    series = rec.get("series") or g
    srec = registry.get(series) or rec
    eps = srec.get("eps") or []
    sg = season_guid_of(series)
    out = []
    new_eps = False
    for i, ep in enumerate(eps):
        eg = episode_guid_of(series, i)
        if eg not in registry:
            new_eps = True
            registry[eg] = {
                "title": ep.get("name") or f"第{i+1}集",
                "eps": [ep], "resolved_at": int(time.time()),
                "series": series, "episode_number": i + 1,
                "pic": srec.get("pic", ""), "site": srec.get("site", ""),
                "referer": srec.get("referer", ""),
                "overview": srec.get("overview", ""),
                "hgdj": srec.get("hgdj"), "hgdj_sid": srec.get("hgdj_sid"),
                "hgdj_ep": ep.get("hgdj_ep"),
            }
        elif srec.get("hgdj"):
            # 老数据可能被误当成剧集解析过，这里强制恢复成单集记录
            registry[eg].update({
                "hgdj": True, "hgdj_sid": srec.get("hgdj_sid"),
                "hgdj_ep": ep.get("hgdj_ep"), "eps": [ep],
                "series": series, "episode_number": i + 1,
                "resolved_at": int(time.time()),
            })
        it = build_item(eg, registry[eg])
        it.update({
            "type": "Episode",
            "title": ep.get("name") or f"第{i+1}集",
            "parent_guid": sg,
            "season_number": 1,
            "episode_number": i + 1,
            "number_of_episodes": 0,
            "media_guid": eg,
            "markedForDeletion": False,
            "ancestor_guid": series,
            "ancestor_name": srec.get("title", ""),
            "ancestor_category": "TV",
        })
        out.append(it)
    if new_eps:
        # 剧集 guid 必须落盘：否则进程重启后前端缓存的集 guid 全部失效
        # （症状：点开提示“视频不存在”）
        save_registry()
    return out


# ------------------------------------------------------------ HTTP 小工具 ---
def parse_query(path):
    if "?" in path:
        p, qs = path.split("?", 1)
    else:
        p, qs = path, ""
    return p, urllib.parse.parse_qs(qs, keep_blank_values=True)


def dechunk(data):
    out, i = b"", 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            sz = int(data[i:j].split(b";")[0].strip(), 16)
        except Exception:
            break
        if sz == 0:
            break
        out += data[j + 2:j + 2 + sz]
        i = j + 2 + sz + 2
    return out


def split_response(raw):
    idx = raw.find(b"\r\n\r\n")
    if idx < 0:
        return raw, b""
    head, body = raw[:idx], raw[idx + 4:]
    hl = head.decode("latin-1", "replace").lower()
    if "transfer-encoding: chunked" in hl:
        return head, dechunk(body)
    m = re.search(r"content-length:\s*(\d+)", hl)
    if m:
        return head, body[:int(m.group(1))]
    return head, body


async def read_request(reader):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(65536)
        if not chunk:
            raise ConnectionError("client closed")
        buf += chunk
        if len(buf) > 1 << 20:
            raise ConnectionError("head too large")
    hraw, rest = buf.split(b"\r\n\r\n", 1)
    lines = hraw.decode("latin-1").split("\r\n")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    te = headers.get("transfer-encoding", "").lower()
    if "chunked" in te:
        while b"\r\n0\r\n\r\n" not in rest:
            chunk = await reader.read(65536)
            if not chunk:
                break
            rest += chunk
        body = dechunk(rest)
        new_head = [lines[0]]
        for ln in lines[1:]:
            if ln.lower().startswith("transfer-encoding:"):
                continue
            new_head.append(ln)
        new_head.append(f"Content-Length: {len(body)}")
        hraw = "\r\n".join(new_head).encode("latin-1")
    elif "content-length" in headers:
        cl = int(headers["content-length"])
        body = rest
        while len(body) < cl:
            chunk = await reader.read(65536)
            if not chunk:
                break
            body += chunk
        body = body[:cl]
    else:
        body = rest
    return hraw + b"\r\n\r\n", headers, body


def json_response(obj, status=200, extra=None):
    b = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
              500: "Internal Server Error"}.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(b)}\r\n"
            f"Connection: keep-alive\r\n")
    for k, v in (extra or []):
        head += f"{k}: {v}\r\n"
    return (head + "\r\n").encode() + b


def text_response(text, ctype="text/html; charset=utf-8", status=200, extra=None):
    b = text.encode("utf-8") if isinstance(text, str) else text
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
              500: "Internal Server Error"}.get(status, "OK")
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(b)}\r\n"
            f"Cache-Control: no-store\r\n"
            f"Connection: keep-alive\r\n")
    for k, v in (extra or []):
        head += f"{k}: {v}\r\n"
    return (head + "\r\n").encode() + b


# ------------------------------------------- 配置页后端（爱迷茫 · 源设置）---
def _json_body(body):
    """把请求体当 JSON 解析；坏数据一律当空对象，不抛给上层。"""
    try:
        got = json.loads((body or b"").decode("utf-8", "replace") or "{}")
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


# 配置页的写操作只放行内网/本机，避免把 5666 映射到公网后被人乱改源
#
# 目的：别让「把 5666 映射到公网」的人被陌生人乱改源。
#
# 但现实里很多人是这样打开配置页的，来源地址并不落在传统 192.168/10 网段：
#   · IPv6（现在三大运营商默认给公网 IPv6）
#   · Tailscale / ZeroTier / frp 之类的叠加网（100.64/10 等）
#   · 飞牛自带的远程访问（中转节点回源的地址）
# 于是会被自己的插件挡在门外。三层兜底：
#   1) 默认网段尽量全（私网 + 回环 + 链路本地 + CGNAT + IPv6 ULA/链路本地 + IPv4-mapped）
#   2) conf/config.json 的显式白名单 write_allow_ip（支持单个 IP 和 CIDR）
#   3) conf/config.json 的 write_allow_any，或环境变量 TRIM_MITM_WRITE_ANY=1
# 另外：直接走 unix socket（没有 X-Real-IP）永远放行 —— 那等于已经在 NAS 上了，
# 所以「本机自救」永远有路（见 README 里 curl --unix-socket 那条）。
_PRIV_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10", "198.18.0.0/15", "0.0.0.0/32",
    "::1/128", "fc00::/7", "fe80::/10",
))

_CONFIG_NOTE = [
    "爱迷茫 · 配置页「保存 / 导入」这类写操作的来源策略。改完立即生效，不用重启。",
    "write_allow_any : true = 不限制来源，任何地址都能改配置（慎用；5666 别对公网裸奔）",
    "write_allow_ip  : 额外放行的 IP 或网段，例如 \"192.168.1.7\" / \"10.0.0.0/8\" / \"2001:db8::/32\"",
    "auto_trust_first: true = 本机第一次写操作时，信任一次并给浏览器发一张凭证（只发生一次）",
    "write_token     : 上面发的浏览器凭证。留着它，你这台浏览器换 IP / 换网络也照样能改；",
    "                  删掉（置空）＝把它作废，下一次写操作会重新信任一次。别外传。",
    "first_trusted   : 上面那次自动信任用掉没有（留档，可改）",
    "默认就放行的：内网私网段、IPv6 内网段(ULA/链路本地)、叠加网(100.64/10)、本机 unix socket",
    "",
    "online_visibility: 在线源（搜索结果 / 豆瓣伪库 / 在线继续播放）对谁可见：",
    "                   all=所有用户，admin=仅管理员（默认），users=仅 online_users 里的用户",
    "online_users     : online_visibility=users 时生效，填飞牛影视用户名，大小写不敏感",
    "",
    "source_mode      : 采集源查询方式：priority=主用优先（旧行为，主用落空才查兜底），",
    "                   parallel=全部同时（主用+兜底一起查，所有启用的源都生效）",
    "source_limit     : 最多启用几个采集源（0=不限；按主用→兜底顺序取前 N 个）",
    "js_source_limit  : 最多同时查几个 JS 源（0=不限；默认 4，避免一次启动太多引擎）",
]

DEFAULT_CONFIG = {
    "_note": _CONFIG_NOTE,
    "write_allow_any": False,
    "write_allow_ip": [],
    "auto_trust_first": True,
    "first_trusted": False,
    "write_token": "",
    # 默认只给管理员看，避免在线源（以及在线观看记录）默认暴露给全家所有账号。
    "online_visibility": "admin",
    "online_users": [],
    # 旧配置没有这三个字段时按旧行为（主用优先）走；新装模板默认「全部同时」。
    "source_mode": "priority",
    "source_limit": 0,
    "js_source_limit": 4,
    # 红果短剧：由本机 HGDJ 服务提供元数据与在线播放流。
    "hgdj_enabled": False,
    "hgdj_url": HGDJ_DEFAULT_URL,
}

# 下发到浏览器的写操作凭证（cookie 名）。它不是给人看的，浏览器自动带。
COOKIE_NAME = "trim_write"


def _is_parseable(s):
    try:
        ipaddress.ip_address(str(s).strip().strip("[]"))
        return True
    except ValueError:
        return False


def _is_private_addr(s):
    """能解析就按网段判；解析不了当作不匹配（不因为脏数据放行）。"""
    s = (s or "").strip().strip("[]")
    if not s:
        return False
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped                # ::ffff:192.168.1.5 这种
    return any(a in n for n in _PRIV_NETS if n.version == a.version)


def load_config():
    """读 conf/config.json；文件坏了/不存在都给默认值，不影响主链路。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            cfg.update({k: v for k, v in got.items() if not k.startswith("_")})
    except FileNotFoundError:
        pass
    except Exception as e:
        log("config.json 读不了，用默认策略:", repr(e)[:120])
    if not isinstance(cfg.get("write_allow_ip"), list):
        cfg["write_allow_ip"] = []
    return cfg


def save_config(patch):
    """把 patch 合并进 conf/config.json（保留 _note）。"""
    cfg = load_config()
    for k, v in (patch or {}).items():
        if k.startswith("_"):
            continue
        cfg[k] = v
    out = dict(DEFAULT_CONFIG)
    out.update(cfg)
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        return False, "写不了 %s：%s" % (CONFIG_FILE, repr(e)[:120])
    _POLICY_CACHE["ts"] = 0.0
    _SOURCE_CFG_CACHE["ts"] = 0.0
    return True, "已保存"


# ---------------------------------------------------------- 用户 / 可见范围 ---
# 飞牛影视把「谁登录了」记在 trimactivity.db 的 user_token 表（token → user_guid），
# 用户资料在 trimmedia.db 的 user 表。只读查询，不改官方数据。
_POLICY_CACHE = {"ts": 0.0, "vis": "admin", "users": set()}
_USER_CACHE = {}                 # token -> (expire_at, user|None)
_USER_CACHE_TTL = 60.0
_USER_CACHE_MAX = 256
_DB_ERR = {}                     # path -> 上次报错时间，避免刷屏


def _db_paths():
    """返回 (trimmedia.db, trimactivity.db)；兼容数据目录不在 /usr/local 的安装。"""
    pairs = [(TRIM_MEDIA_DB, TRIM_ACTIVITY_DB)] + _TRIM_DB_FALLBACKS
    for media, activity in pairs:
        if os.path.exists(media) and os.path.exists(activity):
            return media, activity
    return TRIM_MEDIA_DB, TRIM_ACTIVITY_DB


def _ro_connect(path):
    uri = "file:" + urllib.parse.quote(path, safe="/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=0.5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=500")
    return con


def _db_rows(path, sql, args=()):
    con = None
    try:
        con = _ro_connect(path)
        return con.execute(sql, args).fetchall()
    except Exception as e:
        now = time.time()
        if now - _DB_ERR.get(path, 0) > 300:
            _DB_ERR[path] = now
            log("读取官方数据库失败（在线源权限会按不可识别处理）:",
                os.path.basename(path), repr(e)[:100])
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _db_one(path, sql, args=()):
    rows = _db_rows(path, sql, args)
    return rows[0] if rows else None


def _token_from_headers(headers):
    """从 Cookie / Authorization 里取飞牛影视登录 token。"""
    raw = headers.get("cookie") or ""
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == "Trim-MC-token":
            return urllib.parse.unquote(v.strip().strip('"'))
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", auth):
        return auth
    return ""


def request_user(headers):
    """当前请求对应的飞牛影视用户；未登录 / 认不出来返回 None。"""
    tok = _token_from_headers(headers)
    if not tok:
        return None
    now = time.time()
    hit = _USER_CACHE.get(tok)
    if hit and hit[0] > now:
        return hit[1]
    media_db, activity_db = _db_paths()
    row = _db_one(activity_db,
                  "select user_guid from user_token "
                  "where token=? and status=1 limit 1", (tok,))
    user = None
    if row:
        urow = _db_one(media_db,
                       "select guid,username,is_admin,status from user "
                       "where guid=? and status=1 limit 1",
                       (row["user_guid"],))
        if urow:
            user = {
                "guid": str(urow["guid"] or ""),
                "username": str(urow["username"] or ""),
                "is_admin": bool(int(urow["is_admin"] or 0)),
            }
    if len(_USER_CACHE) >= _USER_CACHE_MAX:
        # 简单淘汰：先清掉过期的，再清最早写入的
        for k in [k for k, v in _USER_CACHE.items() if v[0] <= now]:
            _USER_CACHE.pop(k, None)
        while len(_USER_CACHE) >= _USER_CACHE_MAX:
            _USER_CACHE.pop(next(iter(_USER_CACHE)), None)
    _USER_CACHE[tok] = (now + _USER_CACHE_TTL, user)
    return user


def media_users():
    """给配置页列出的飞牛影视用户（只列启用中的）。"""
    media_db, _ = _db_paths()
    rows = _db_rows(media_db,
                    "select guid,username,is_admin from user "
                    "where status=1 order by is_admin desc, username")
    return [{"guid": str(r["guid"] or ""),
             "username": str(r["username"] or ""),
             "is_admin": bool(int(r["is_admin"] or 0))} for r in rows]


def online_policy():
    """返回 (可见范围, 允许的用户名/guid 集合)。文件改动后最多 2 秒生效。"""
    now = time.time()
    if now - _POLICY_CACHE["ts"] < 2.0:
        return _POLICY_CACHE["vis"], _POLICY_CACHE["users"]
    cfg = load_config()
    vis = str(cfg.get("online_visibility") or "admin").strip().lower()
    if vis not in ("all", "admin", "users"):
        vis = "admin"
    raw = cfg.get("online_users") or []
    if not isinstance(raw, list):
        raw = []
    users = {str(x).strip().lower() for x in raw if str(x).strip()}
    _POLICY_CACHE.update({"ts": now, "vis": vis, "users": users})
    return vis, users


def online_allowed(user):
    """这个用户能不能看到在线源。没有登录信息时，只有 all 模式放行。"""
    vis, allowed = online_policy()
    if vis == "all":
        return True
    if not user:
        return False
    if vis == "admin":
        return bool(user.get("is_admin"))
    if user.get("is_admin"):
        return True
    name = str(user.get("username") or "").strip().lower()
    guid = str(user.get("guid") or "").strip().lower()
    return bool(name and name in allowed) or bool(guid and guid in allowed)


_SOURCE_CFG_CACHE = {"ts": 0.0, "mode": "priority",
                     "limit": 0, "js_limit": 4}


def _limit_int(v, default, maximum):
    try:
        n = int(v)
    except Exception:
        n = default
    return max(0, min(maximum, n))


def source_policy():
    """返回 (查询方式, 采集源上限, JS 源上限)；0 表示不限。"""
    now = time.time()
    if now - _SOURCE_CFG_CACHE["ts"] < 2.0:
        return (_SOURCE_CFG_CACHE["mode"], _SOURCE_CFG_CACHE["limit"],
                _SOURCE_CFG_CACHE["js_limit"])
    cfg = load_config()
    mode = str(cfg.get("source_mode") or "priority").strip().lower()
    if mode not in ("priority", "parallel"):
        mode = "priority"
    limit = _limit_int(cfg.get("source_limit"), 0, 100)
    js_limit = _limit_int(cfg.get("js_source_limit"), 4, 50)
    _SOURCE_CFG_CACHE.update({"ts": now, "mode": mode, "limit": limit,
                              "js_limit": js_limit})
    return mode, limit, js_limit


def cms_plan():
    """按配置把主用/兜底整理成本次真正启用的两级源。
    返回 (mode, primary, fallback, js_limit)。
    source_limit 按「主用在前、兜底在后」截断；0 表示不限。"""
    mode, limit, js_limit = source_policy()
    p = list(CMS_PRIMARY)
    f = list(CMS_FALLBACK)
    if limit > 0:
        p = p[:limit]
        f = f[:max(0, limit - len(p))]
    return mode, p, f, js_limit


def source_policy_payload():
    mode, limit, js_limit = source_policy()
    return {"source_mode": mode, "source_limit": limit,
            "js_source_limit": js_limit}


def public_config():
    """给配置页的配置副本：绝不把 write_token 发回浏览器。"""
    cfg = load_config()
    cfg.pop("write_token", None)
    return cfg


def visibility_payload():
    """配置页需要的可见范围 + 用户列表。"""
    vis, _ = online_policy()
    cfg = load_config()
    selected = cfg.get("online_users") or []
    if not isinstance(selected, list):
        selected = []
    return {"online_visibility": vis,
            "online_users": [str(x) for x in selected if str(x).strip()],
            "users": media_users()}


def _env_write_any():
    return (os.environ.get("TRIM_MITM_WRITE_ANY") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _cookie_token(headers):
    """从请求的 Cookie 头里取出写操作凭证。"""
    raw = headers.get("cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == COOKIE_NAME:
            return v.strip()
    return ""


def ensure_cookie(headers):
    """本次请求若已被信任，就把写凭证下发/刷新到这台浏览器。

    这样「第一次是怎么进来的」不再重要：认过一次之后，你这台浏览器
    换 IP（宽带重播号、换到 4G、飞牛远程访问走另一条线路）都还能改配置。
    陌生人即使打开了配置页，只要不在白名单也拿不到凭证。
    """
    ok, _ip, _why = write_gate(headers)
    if not ok:
        return []
    tok = str(load_config().get("write_token") or "")
    if not tok:
        return []
    return [("Set-Cookie", "%s=%s; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly"
                         % (COOKIE_NAME, tok))]


def write_state(headers):
    """给配置页看的「写操作来源」快照。"""
    allowed, ip, why = write_gate(headers)
    cfg = load_config()
    return {
        "allowed": allowed, "ip": ip, "why": why,
        "allow_any": bool(cfg.get("write_allow_any") or _env_write_any()),
        "auto_trust_first": bool(cfg.get("auto_trust_first", True)),
        "first_trusted": bool(cfg.get("first_trusted")),
        "browser_bound": bool(str(cfg.get("write_token") or "")
                              and _cookie_token(headers) == str(cfg.get("write_token"))),
        "token_set": bool(str(cfg.get("write_token") or "")),
        "config_file": CONFIG_FILE,
    }


def write_gate(headers):
    """→ (是否放行, 来源 IP, 说明)。说明用于配置页展示与 403 提示。"""
    cfg = load_config()
    ip = (headers.get("x-real-ip") or "").strip()
    if not ip:
        ip = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if not ip:
        return True, "本机(unix socket)", "没经 nginx，等于已经在 NAS 上"
    if cfg.get("write_allow_any") or _env_write_any():
        return True, ip, "已开启「不限制来源」"
    if _is_private_addr(ip):
        return True, ip, "内网 / 叠加网地址"
    tok = str(cfg.get("write_token") or "")
    if tok and _cookie_token(headers) == tok:
        return True, ip, "这台浏览器已经认过（凭证在 cookie 里，与 IP 无关）"
    for rule in cfg.get("write_allow_ip") or []:
        rule = str(rule).strip()
        if not rule:
            continue
        if not _is_parseable(ip):
            break
        a = ipaddress.ip_address(ip.strip().strip("[]"))
        if a.version == 6 and a.ipv4_mapped is not None:
            a = a.ipv4_mapped
        try:
            net = ipaddress.ip_network(rule, strict=False)
        except ValueError:
            if rule == ip:
                return True, ip, "在你的白名单里"
            continue
        if a.version == net.version and a in net:
            return True, ip, "在你的白名单里（%s）" % rule
    return False, ip, "来源不在内网，也不在白名单里"


def _write_allowed(headers):
    return write_gate(headers)[0]


def post_allowed(headers):
    """POST 的闸门。在 write_gate 之上加一次性的「首次配置自动信任」：

    全新安装（或者从老版本升上来）时，用户可能根本不在私网里（IPv6、飞牛远程
    访问、反代……），如果一上来就 403，他就被自己的插件锁在门外，还得 SSH 进去
    改文件 —— 体验太差。所以：**本机还没有发过浏览器凭证**时，允许一次写操作，
    顺手把来源 IP 记进白名单、把凭证发给这台浏览器。只发生一次，
    结果写进 conf/config.json，随时可以删掉重来。
    不想用这个行为：把 auto_trust_first 设为 false。
    """
    ok, ip, _why = write_gate(headers)
    if ok:
        return True
    cfg = load_config()
    if not cfg.get("auto_trust_first", True):
        return False
    if str(cfg.get("write_token") or ""):
        return False          # 已经发过凭证了，不再自动信任新的来源
    lst = list(cfg.get("write_allow_ip") or [])
    if ip and ip not in lst and ip != "本机(unix socket)":
        lst.append(ip)
    tok = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    ok2, msg = save_config({"write_allow_ip": lst, "first_trusted": True,
                            "write_token": tok})
    if ok2:
        log("首次配置：已信任来源 %s 并下发浏览器凭证（%s）；"
            "不想保留就把 write_allow_ip 清掉、或把 write_token 置空。"
            % (ip, CONFIG_FILE))
        return True
    log("首次配置自动信任失败：", msg)
    return False


# 从微信 / 论坛复制粘贴过来的地址常带全角符号、零宽字符、全角空格 —— 先规整
_URL_TRANS = str.maketrans({"：": ":", "／": "/", "．": ".", "。": ".",
                            "\u3000": " ", "\u200b": "", "\ufeff": ""})


def _do_import(url):
    """配置页：抓一份 TVBox 配置并筛出「能直接用」的接口（阻塞，跑在线程里）。"""
    # 从微信/论坛粘过来的地址常带全角符号、零宽字符、首尾空白，先规整
    url = (url or "").strip().translate(_URL_TRANS).strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False,
                "msg": "请填一个 http(s) 开头的配置地址（比如 http://xxx.top/tv）"}

    # 中文域名：记一笔，抓取时转 punycode，回报给用户看一眼
    note = ""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host and not host.isascii():
        try:
            note = "域名里的中文已转成 punycode 再请求：%s → %s" % (
                host, host.encode("idna").decode())
        except Exception:
            note = ""

    try:
        text = webui.fetch_config(url)
    except ValueError as e:                     # fetch_config 自己给的说明
        return {"ok": False, "msg": "抓取失败：%s" % str(e)[:400]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "msg": "抓取失败：服务器返回 %s %s（地址不对，"
                                    "或这个入口要浏览器/客户端 UA）" % (e.code, e.reason)}
    except urllib.error.URLError as e:
        return {"ok": False, "msg": "抓取失败：连不上 %s（%s）—— 域名解析不了或网络不通"
                                    % (host or url, e.reason)}
    except Exception as e:
        return {"ok": False, "msg": "抓取失败：%s" % repr(e)[:160]}
    try:
        kept, skipped, jskept, meta = webui.parse_tvbox(text)
    except Exception as e:
        return {"ok": False, "msg": "解析失败：%s" % repr(e)[:200]}
    return {"ok": True, "kept": kept, "skipped": skipped, "js": jskept,
            "meta": meta, "note": note}


def _test_js_sites(sites, keyword, timeout):
    """配置页：测 JS（drpy）源 —— home / 分类 / 搜索 三项，带耗时。"""
    out = []
    if jsspider is None:
        for s in sites:
            out.append({"name": s["name"], "js": s["js"], "referer": s.get("referer", ""),
                        "ms": 0, "hits": 0, "playable": 0, "routes": 0,
                        "title": "", "err": "JS 引擎不可用：" + _JS_ERR,
                        "lines": s["lines"], "level": s.get("level") or "primary",
                        "kind": "js", "classes": 0, "cate": 0, "search": 0})
        return out

    def one(s):
        try:
            r = jsspider.probe(s, keyword or "你好")
        except Exception as e:
            r = {"ok": False, "ms": 0, "classes": 0, "cate": 0, "search": 0,
                 "err": str(e)[:120], "first": ""}
        return {
            "name": s["name"], "js": s["js"], "referer": s.get("referer", ""),
            "ms": r.get("ms") or 0,
            "hits": int(r.get("search") or 0),
            "playable": 1 if r.get("ok") else 0,
            "routes": 1,
            "title": r.get("first") or "",
            "err": r.get("err") or "",
            "lines": s["lines"], "level": s.get("level") or "primary",
            "kind": "js", "classes": r.get("classes") or 0,
            "cate": r.get("cate") or 0, "search": r.get("search") or 0,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        for res in ex.map(one, sites):
            out.append(res)
    return out


def _do_test(sites, keyword, timeout, source_limit=None,
             js_source_limit=None, source_mode=None):
    """配置页：逐个测响应时间/命中/可放线路，并给出推荐方案（阻塞，跑在线程里）。"""
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 25.0
    timeout = min(max(timeout, 5.0), 60.0)
    cur_mode, cur_limit, cur_js = source_policy()
    if source_mode not in ("priority", "parallel"):
        source_mode = cur_mode
    if source_limit is None:
        source_limit = cur_limit
    if js_source_limit is None:
        js_source_limit = cur_js
    source_limit = _limit_int(source_limit, 0, 100)
    js_source_limit = _limit_int(js_source_limit, 4, 50)
    clean, jsclean = [], []
    for s in (sites if isinstance(sites, list) else []):
        if not isinstance(s, dict):
            continue
        lines = str(s.get("lines") or "auto").strip().lower()
        if lines not in ("single", "multi"):
            lines = "auto"
        level = str(s.get("level") or "primary")
        # JS（drpy）源
        js = str(s.get("js") or "").strip()
        if js and (s.get("kind") == "js" or not str(s.get("api") or "").strip()):
            jsclean.append({"name": str(s.get("name") or js.rsplit("/", 1)[-1])[:40],
                            "js": js, "referer": str(s.get("referer") or ""),
                            "lines": lines, "level": level, "kind": "js"})
            continue
        api = str(s.get("api") or "").strip()
        if not api.startswith(("http://", "https://")):
            continue
        clean.append({"name": str(s.get("name") or "")[:40], "api": api,
                      "referer": str(s.get("referer") or ""),
                      "lines": lines, "level": level})
    if not clean and not jsclean:
        return {"ok": False, "msg": "没有可测的源（接口必须是 http(s) 开头）",
                "results": [], "recommend": {"text": "", "primary": [],
                                             "fallback": []}}
    results = webui.test_sites(clean, keyword or "凡人", timeout) if clean else []
    if jsclean:
        results += _test_js_sites(jsclean, keyword or "你好", timeout)
    return {"ok": True, "results": results,
            "recommend": webui.recommend(results, source_limit,
                                         js_source_limit, source_mode)}


async def _cap_and_replay(head, body, raw):
    """DIAG 自证：把本地点播请求原样重放给官方 socket，对比经 MITM 的响应。

    「经 MITM 前端不取流、直连官方就正常」只可能是响应被改了。这里直接
    抓同一请求的两种响应做字节级比对，避免再靠读前端压缩 JS 猜。
    """
    if _DIAG != "2" or _CAPN[0] >= 8:
        return
    try:
        lines = [ln for ln in head.split(b"\r\n")
                 if ln and not ln.lower().startswith(b"connection:")]
        lines.append(b"Connection: close")
        rhead = b"\r\n".join(lines) + b"\r\n\r\n"
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(REAL), 4)
        w.write(rhead + body)
        await w.drain()
        buf = b""
        while True:
            try:
                c = await asyncio.wait_for(r.read(65536), 15)
            except asyncio.TimeoutError:
                break
            if not c:
                break
            buf += c
        w.close()
        _CAPN[0] += 1
        rl = head.split(b"\r\n", 1)[0].decode("latin-1")[:58]
        log(f"CAP#{_CAPN[0]} {rl} same={buf == raw} "
            f"mitm={len(raw)}B direct={len(buf)}B")
        log("CAP mitm-hd:", raw.split(b"\r\n\r\n", 1)[0][:170])
        log("CAP dir -hd:", buf.split(b"\r\n\r\n", 1)[0][:170])
        if buf != raw:
            _bm = raw.split(b"\r\n\r\n", 1)[-1]
            _bd = buf.split(b"\r\n\r\n", 1)[-1]
            log("CAP mitm-bd:", _bm[:200])
            log("CAP dir -bd:", _bd[:200])
    except Exception as e:
        log("CAP err", repr(e)[:90])


async def connect_upstream():
    """连官方上游：优先它自己的 unix socket（nginx 原本的路径），TCP 回退。"""
    if os.path.exists(REAL):
        try:
            return await asyncio.wait_for(
                asyncio.open_unix_connection(REAL), 4)
        except Exception:
            pass
    return await asyncio.wait_for(asyncio.open_connection(*UPSTREAM_TCP), 8)


async def forward_stream(head, body, writer, tee=None, tee_max=6000):
    """透明流式转发：响应边收边发，不整体缓冲。

    本地原盘动辄数 GB，若先在内存里攒完再吐，播放器会长时间拿不到数据
    （表现为一直“加载中”）。这里逐块转发，语义等价于“nginx 直连官方”。
    tee 用于 DIAG：把前 tee_max 字节留一份给日志/比对。
    """
    ur, uw = await connect_upstream()
    try:
        uw.write(head + body)
        await uw.drain()
        total = 0
        while True:
            try:
                d = await asyncio.wait_for(ur.read(65536), UPSTREAM_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not d:
                break
            total += len(d)
            if tee is not None and sum(map(len, tee)) < tee_max:
                tee.append(d[:tee_max])
            writer.write(d)
            await writer.drain()
        return total
    finally:
        try:
            uw.close()
        except Exception:
            pass


async def forward(head, headers, body):
    lines = head.split(b"\r\n")
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.lower().startswith(b"connection:"):
            continue
        out.append(ln)
    while out and out[-1] == b"":        # 去掉尾部空行，防 Connection 掉进 body
        out.pop()
    out.append(b"Connection: close")
    fhead = b"\r\n".join(out) + b"\r\n\r\n"
    # 必须优先走官方自己的 unix socket：nginx 原本就是这么连官方的，
    # 官方对「本地媒体库的播放授权」判定与传输方式相关——改走 TCP 会让
    # 本地视频 play/info 直接 AuthFailed（在线条目不受影响，故长期被掩盖）。
    # unix socket 文件若已失效，connect 会立刻 ECONNREFUSED，再回退 TCP。
    reader, writer = await connect_upstream()
    try:
        writer.write(fhead + body)
        await writer.drain()
        raw = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536),
                                               UPSTREAM_TIMEOUT)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            raw += chunk
        return raw
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def write_head(writer, status, hdrs):
    reason = {200: "OK", 206: "Partial Content", 302: "Found",
              404: "Not Found", 502: "Bad Gateway"}.get(status, "OK")
    lines = [f"HTTP/1.1 {status} {reason}"]
    for k, v in hdrs.items():
        lines.append(f"{k}: {v}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()


# ------------------------------------------------------------- HLS 代理 ---
def proxied(url, referer=""):
    q = "u=" + urllib.parse.quote(url, safe="")
    if referer:
        q += "&r=" + urllib.parse.quote(referer, safe="")
    return "/v/media/online/seg?" + q



def _is_ts(head):
    """MPEG-TS 分片以 0x47 同步字节开头"""
    return head[:1] == b"\x47"


# --------------------------------------------------------------- 真实流参数
# 片源方在 master 清单里声明的分辨率经常和真实流不符。实测某站声明
# RESOLUTION=720x406 / BANDWIDTH=3000000，实际发的是 1920x1080（分片目录
# 写着 9622kb）。播放器照着声明配好硬件解码器、收到 1080p 帧后参数不符，
# 就提示「不支持硬解码」转软解，时长也拿不到正确参数。
# 这里用 ffprobe 探一个中段分片拿真值，按 master URL 缓存并落盘（重启不丢）。
_PROPS_FILE = os.path.join(os.path.dirname(REG_FILE), "stream_props.json")
_PROPS_TTL = 7 * 86400
_PROPS_MAX = 2000
_URL_PROPS = {}


def _load_props():
    global _URL_PROPS
    try:
        with open(_PROPS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _URL_PROPS = d if isinstance(d, dict) else {}
    except Exception:
        _URL_PROPS = {}


def _save_props():
    try:
        tmp = _PROPS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_URL_PROPS, f, ensure_ascii=False)
        os.replace(tmp, _PROPS_FILE)
    except Exception as e:
        log("props save err:", repr(e)[:70])


def props_of_rec(rec):
    """条目首个线路的实测流参数（没探过返回 None）"""
    now = time.time()
    for ep in (rec.get("eps") or []):
        u = ep.get("url")
        if not u:
            continue
        p = _URL_PROPS.get(u)
        if p and now - (p.get("ts") or 0) < _PROPS_TTL:
            return p
    return None


# 少数站的 ts 分片前面塞了一坨假数据（实测伪装成 BMP 图片：54 字节文件头
# + 全零，合计 1036 字节），真正的 TS 包从中间才开始。播放器的硬件解码器
# 读到这串垃圾就报「不支持硬解码」转软解（软解器宽容，会自己找同步字节，
# 所以画面还能出来，只是走路软解）。这里把前缀切掉，只把真 TS 交出去。
_WRAP_HOSTS = {}          # host → 前缀字节数（认出过就记住，省得每次重扫）


def _ts_strip(buf, max_scan=2048):
    """找 TS 包边界；返回 (前缀长度, 是否 TS)。认不出返回 (0, False)。

    依据 TS 包固定 188 字节、且以 0x47 开头。加密后的随机数据凑不出
    连续多个 188 周期同步字节，所以不会把加密分片误判成带前缀。
    """
    if not buf:
        return 0, False
    if buf[:1] == b"\x47":
        return 0, True
    lim = min(len(buf), max_scan)
    best, best_n = 0, 0
    for ph in range(188):
        n = 0
        for i in range(ph, lim, 188):
            if buf[i] == 0x47:
                n += 1
        if n > best_n:
            best, best_n = ph, n
    if best_n < 5:
        return 0, False
    i = best
    while i + 188 <= len(buf) and buf[i] != 0x47:      # 跳过全零的包槽
        i += 188
    j, end = i, min(len(buf) - 188, i + 188 * 40)
    while j <= end:                                    # 优先从 PAT 包开始
        if buf[j] == 0x47 and (buf[j + 1] & 0x40) \
                and (((buf[j + 1] & 0x1F) << 8) | buf[j + 2]) == 0:
            return j, True
        j += 188
    return i, True


async def _read_rest(resp, cap=24_000_000):
    """把上游响应剩下的字节收齐（上限 cap，防跑飞）"""
    out = b""
    while len(out) < cap:
        chunk = await resp.content.read(65536)
        if not chunk:
            break
        out += chunk
    return out


def _aes_decrypt(data, key, iv_hex=None):
    """AES-128-CBC 解密 HLS 分片（IV 缺省全 0，HLS 里最常见）"""
    try:
        from Crypto.Cipher import AES
    except Exception as e:
        log("无 pycryptodome，跳过分片解密:", repr(e)[:50])
        return None
    try:
        iv = bytes.fromhex(iv_hex or "")
        if len(iv) != 16:
            iv = b"\x00" * 16
        n = len(data) - (len(data) % 16)     # 尾部不足一块的丢弃
        if n <= 0:
            return None
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data[:n])
    except Exception as e:
        log("AES 解密失败:", repr(e)[:60])
        return None


def _parse_key_line(line, base_url):
    """#EXT-X-KEY → {"key_url","iv"}；非 AES-128 返回 {}"""
    if "METHOD=AES-128" not in line.upper():
        return {}
    m = re.search(r'URI="([^"]+)"', line)
    if not m:
        return {}
    ivm = re.search(r"IV=0x([0-9A-Fa-f]+)", line)
    iv = (ivm.group(1).lower() if ivm else "")
    iv = iv.rjust(32, "0")[:32]
    return {"key_url": urllib.parse.urljoin(base_url, m.group(1)), "iv": iv}


def _pick_main_segment(base_url, text):
    """挑「正片」里的一个分片 + 该分片生效的 AES 密钥信息。

    不能取整条清单的中点：实测某站片头片尾插了另一个分辨率/码率的贴片
    广告（还写在 DISCONTINUITY 分组里），照整条清单取中点会探到广告段，
    参数就全错了。正片 = 时长最长的那个分组（和广告剔除的判断一致）。
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    gdur, gsegs = {}, {}
    gi = 0
    for s in lines:
        if s.startswith("#EXT-X-DISCONTINUITY"):
            gi += 1
            continue
        if s.startswith("#EXTINF:"):
            try:
                gdur[gi] = gdur.get(gi, 0.0) + float(s[8:].rstrip(","))
            except Exception:
                pass
        elif not s.startswith("#"):
            gsegs.setdefault(gi, []).append(s)
    if not gsegs:
        return None, {}
    main = max(gsegs, key=lambda k: gdur.get(k, 0.0))
    # 逐行跟踪 KEY 状态（KEY 只对其后的分片生效），取正片中间那一片
    cur_key, gi, cands = {}, 0, []
    for s in lines:
        if s.startswith("#EXT-X-DISCONTINUITY"):
            gi += 1
        elif s.startswith("#EXT-X-KEY:"):
            cur_key = _parse_key_line(s, base_url)
        elif not s.startswith("#") and gi == main:
            cands.append((s, dict(cur_key)))
    if not cands:
        return None, {}
    seg, key = cands[len(cands) // 2]
    return urllib.parse.urljoin(base_url, seg), key


async def _fetch_bytes(url, referer):
    try:
        async with session.get(url, ssl=False, allow_redirects=True,
                               headers={"User-Agent": UA, "Referer": referer or ""},
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                return None
            return await r.read()
    except Exception as e:
        log("fetch err:", repr(e)[:60])
        return None


async def _ffprobe_props(seg_url, referer, key_url=None, iv=None):
    """探单个分片拿真实编码/分辨率/声道。

    分片可能是 AES-128 加密的（实测某站正片全部加密，直接探会
    "Invalid data found"），有密钥就先解密再探。
    """
    raw = await _fetch_bytes(seg_url, referer)
    if not raw:
        return None
    blob = raw
    if blob[:1] != b"\x47":                      # 0x47 = TS 同步字节
        off, is_ts = _ts_strip(raw, len(raw))    # 有的站分片前面带假前缀
        if is_ts and off:
            blob = raw[off:]
        elif key_url:
            kb = await _fetch_bytes(key_url, referer)
            if not kb or len(kb) < 16:
                return None
            blob = _aes_decrypt(raw, kb[:16], iv)
            if not blob or blob[:1] != b"\x47":
                log("probe: 解密后仍不是 TS，跳过")
                return None
        else:
            log("probe: 分片非明文 TS 且无密钥，跳过")
            return None
    fd, path = tempfile.mkstemp(suffix=".ts")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        args = ["ffprobe", "-v", "error", "-probesize", "1500000",
                "-analyzeduration", "800000", "-of", "json",
                "-show_entries",
                "stream=codec_type,codec_name,profile,level,width,height,channels",
                path]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except Exception as e:
        log("ffprobe err:", repr(e)[:60])
        return None
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    try:
        js = json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return None
    props = {"ts": int(time.time())}
    for st in (js.get("streams") or []):
        if st.get("codec_type") == "video" and not props.get("width"):
            props.update(width=st.get("width"), height=st.get("height"),
                         codec=st.get("codec_name"), profile=st.get("profile"),
                         level=st.get("level"))
        elif st.get("codec_type") == "audio" and not props.get("acodec"):
            props["acodec"] = st.get("codec_name")
            props["channels"] = st.get("channels")
    return props if props.get("width") else None


async def _probe_props_impl(master_url, referer, master_text=None):
    ref = referer or ""
    hdrs = {"User-Agent": UA, "Referer": ref}
    try:
        txt = master_text
        if txt is None:
            async with session.get(master_url, headers=hdrs, ssl=False,
                                   allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                txt = await r.text()
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        var = [l for l in lines if not l.startswith("#")]
        if not var:
            return None
        child = urllib.parse.urljoin(master_url, var[0])
        async with session.get(child, headers=hdrs, ssl=False,
                               allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=10)) as r2:
            if r2.status != 200:
                return None
            ctxt = await r2.text()
        seg, key = _pick_main_segment(child, ctxt)
        if not seg:
            return None
        props = await _ffprobe_props(seg, ref, key.get("key_url"), key.get("iv"))
    except Exception as e:
        log("props probe err:", repr(e)[:70])
        return None
    if not props:
        return None
    _URL_PROPS[master_url] = props
    if len(_URL_PROPS) > _PROPS_MAX:                 # 只留最新的一批
        for k, _v in sorted(_URL_PROPS.items(),
                            key=lambda kv: kv[1].get("ts") or 0
                            )[:len(_URL_PROPS) - _PROPS_MAX]:
            _URL_PROPS.pop(k, None)
    _save_props()
    log("stream props:", (master_url.split("/")[2] if "//" in master_url else ""),
        "%sx%s %s %s ch=%s" % (props.get("width"), props.get("height"),
                               props.get("codec") or "?",
                               props.get("profile") or "",
                               props.get("channels") or "-"))
    return props


async def probe_props(master_url, referer, master_text=None):
    """取 master 的真实流参数（有缓存就直接返回）"""
    hit = _URL_PROPS.get(master_url)
    if hit and time.time() - (hit.get("ts") or 0) < _PROPS_TTL:
        return hit
    return await _probe_props_impl(master_url, referer, master_text)


def kick_props_probe(guid):
    """播放接口被调用时先探一把，等播放器来取主清单时缓存已就绪"""
    rec = registry.get(guid)
    if not isinstance(rec, dict) or props_of_rec(rec):
        return
    eps = rec.get("eps") or []
    u0 = eps[0].get("url") if eps else None
    if u0 and _looks_m3u8(u0):
        asyncio.create_task(probe_props(u0, rec.get("referer") or ""))


def _log_play_req(path, body):
    """播放接口的请求体里带着客户端能力（支持的分辨率/编码等）。

    记一条：万一播放器还提示「不支持硬解码」，可以据此判断是客户端
    报的能力不够，还是我们给的参数不对。
    """
    try:
        t = (body or b"").decode("utf-8", "replace").strip()
    except Exception:
        t = ""
    if t and t not in ("{}", "null"):
        log("PLAY-REQ", path.rsplit("/", 1)[-1], t[:400])


def aspect_label(w, h):
    """归一到 16:9 / 4:3 / 9:16 这类写法（别给前端 360:203 这种）"""
    try:
        w, h = float(w or 0), float(h or 0)
    except Exception:
        return "16:9"
    if w <= 0 or h <= 0:
        return "16:9"
    r = w / h
    ratios = (("16:9", 16 / 9), ("4:3", 4 / 3), ("3:2", 3 / 2),
              ("21:9", 21 / 9), ("1:1", 1.0))
    for name, val in ratios:
        if abs(r - val) / val < 0.05:
            return name
    for name, val in ratios:
        if abs(r - 1 / val) / (1 / val) < 0.05:
            num, den = name.split(":")
            return "%s:%s" % (den, num)
    return "16:9"


def res_label(w, h):
    """resolution_type 用短边命名（1080p / 720p …）"""
    try:
        short = min(int(w or 0), int(h or 0))
    except Exception:
        short = 0
    for std in (2160, 1440, 1080, 720, 576, 480, 360, 240):
        if short >= std:
            return "%dp" % std
    return "1080p"


def _fix_stream_inf(line, props):
    """把 master 里与真实流不符的 RESOLUTION 改成实测值。

    只改分辨率：BANDWIDTH 用单个分片估不准（VBR 波动大），估低了会让
    播放器缓冲不足反而卡顿，宁可不改。
    """
    if not line.startswith("#EXT-X-STREAM-INF"):
        return line
    try:
        w, h = int(props.get("width") or 0), int(props.get("height") or 0)
    except Exception:
        return line
    if not (w and h):
        return line
    attrs = line.split(":", 1)[1].strip() if ":" in line else ""
    keep = [p.strip() for p in attrs.split(",")
            if p.strip() and not p.strip().upper().startswith("RESOLUTION=")]
    keep.append("RESOLUTION=%dx%d" % (w, h))
    return "#EXT-X-STREAM-INF:" + ",".join(keep)


# 清单级标签（对整个播放列表生效，不属于某个分片）：剔除广告分组时
# 绝不能跟着一起丢。实测某站把 #EXT-X-ENDLIST 放在片尾广告组里，一丢
# 播放器就把它当直播流：不显示总时长、进度条不动，还可能走软解路径。
PL_LEVEL_TAGS = ("#EXT-X-ENDLIST", "#EXT-X-INDEPENDENT-SEGMENTS",
                 "#EXT-X-START", "#EXT-X-PLAYLIST-TYPE", "#EXT-X-VERSION",
                 "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE",
                 "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-ALLOW-CACHE",
                 "#EXT-X-I-FRAMES-ONLY", "#EXTM3U")


def rewrite_playlist(text, base_url, referer, props=None):
    """改写清单 URI 为同源代理路径，并剔除广告段。

    正确做法：
    - 清单头（#EXTM3U 与 #EXT-X-* 全局标签）必须始终保留在输出最前，
      否则播放器判定为非法清单直接失败。
    - 广告判定按「DISCONTINUITY 分组」进行；组内分片时长中位数
      若为片源方标注的固定广告时长，则整组剔除。
    - 剔除广告分组时，清单级标签（尤其 #EXT-X-ENDLIST）必须补回，
      否则播放器把点播当直播：不显示总时长、进度条不动。
    - props（实测流参数）不为空时，纠正主清单里片源方声明的分辨率。
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return text

    # ---- 1) 分离：全局头 / 分组 ----
    header = []
    idx = 0
    while idx < len(lines):
        s = lines[idx]
        # 第一个非标签行(分片)出现前，所有标签都算全局头
        if not s.startswith("#"):
            break
        if s.startswith("#EXTINF:"):
            break
        header.append(s)
        idx += 1
    # KEY/MAP 是按分片生效的，不算全局头
    header = [h for h in header
              if not h.startswith("#EXT-X-KEY")
              and not h.startswith("#EXT-X-MAP")]
    if props and props.get("width"):
        header = [_fix_stream_inf(h, props) for h in header]

    # ---- 2) 按 DISCONTINUITY 切分剩余部分 ----
    body = lines[idx:]
    groups = [[]]
    for s in body:
        if s.startswith("#EXT-X-DISCONTINUITY"):
            groups.append([])
            continue
        groups[-1].append(s)

    def durs_of(g):
        out = []
        for s in g:
            if s.startswith("#EXTINF:"):
                try:
                    out.append(float(s[8:].rstrip(",")))
                except Exception:
                    pass
        return out

    def gdur(g):
        return sum(durs_of(g))

    ds = [gdur(g) for g in groups]
    has_media = [len([x for x in g if not x.startswith("#")]) > 0 for g in groups]
    total = sum(d for d, ok in zip(ds, has_media) if ok)
    media_groups = [i for i, ok in enumerate(has_media) if ok and ds[i] > 0]
    # 最长的媒体分组时长（正片基准）
    base = max((ds[i] for i in media_groups), default=0.0)

    keep = []
    for i, g in enumerate(groups):
        if not has_media[i]:
            keep.append(True)            # 空组（只有 DISCONTINUITY）保留占位
            continue
        d = ds[i]
        seg_d = durs_of(g)
        # 组内中位时长
        med = sorted(seg_d)[len(seg_d) // 2] if seg_d else 0.0
        # 规则1：整组总时长命中片源方标注的固定广告时长
        #       注意不能用「组内分片时长」——正片分片也常是 4.0s
        if base >= MIN_MAIN_DUR and any(abs(d - v) < 0.05 for v in KNOWN_AD_DURS):
            keep.append(False)
            continue
        # 规则2：贴片广告——整组时长远小于正片
        #        真实场景：正片 20~60min，片头/片尾广告 5~90s（占 3%~8%）
        if base >= MIN_MAIN_DUR and d < base * 0.12:
            keep.append(False)
            continue
        keep.append(True)

    dropped = sum(1 for k in keep if not k and has_media[keep.index(k)]
                  if False)  # 占位，实际用下面统计

    # ---- 3) 组装输出：先清单头，再保留的分组 ----
    # 被剔除分组里的清单级标签先挑出来，不能随分组一起丢
    head_tags, tail_tags = [], []
    for i, g in enumerate(groups):
        if keep[i]:
            continue
        for s in g:
            if not s.startswith(PL_LEVEL_TAGS):
                continue
            (tail_tags if s.startswith("#EXT-X-ENDLIST")
             else head_tags).append(s)
    out = list(header)
    if not any(s == "#EXTM3U" for s in out):
        out.insert(0, "#EXTM3U")
    for t in head_tags:
        if t not in out:
            out.append(t)
    prev_media = False
    ndrop = 0
    for i, g in enumerate(groups):
        if not keep[i]:
            if has_media[i]:
                ndrop += 1
            continue
        if not g:
            continue
        if not has_media[i]:
            continue
        if out and prev_media:
            out.append("#EXT-X-DISCONTINUITY")
        for s in g:
            if s.startswith("#"):
                if 'URI="' in s:
                    def _rep(m, _b=base_url, _r=referer):
                        absu = urllib.parse.urljoin(_b, m.group(1))
                        return 'URI="%s"' % proxied(absu, _r)
                    s = re.sub(r'URI="([^"]+)"', _rep, s)
                out.append(s)
            else:
                out.append(proxied(urllib.parse.urljoin(base_url, s), referer))
        prev_media = True

    if ndrop:
        log(f"ad-filter: 剔除 {ndrop} 个广告分组")
    # ENDLIST 必须落在最后一行
    for t in tail_tags:
        if not any(x.startswith("#EXT-X-ENDLIST") for x in out):
            out.append(t)
    return "\r\n".join(out) + "\r\n"


def _already_wrote(wrote):
    return bool(wrote) if wrote is not None else False


async def proxy_media(target, referer, client_headers, writer, owner=None,
                      retries=2, wrote=None, timeout=30):
    """拉取在线资源并写给客户端。m3u8 清单做 URI 改写+广告剔除。
    返回 True=已写出响应；False=失败（未写任何响应，可换源重试）。

    上游 CDN 偶发 5xx/超时（实测某集变体清单瞬时 500），单次失败会让
    播放器永远等不到响应 → “加载中…播放失败”。这里对瞬时错误做重试。
    """
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        hdrs["Referer"] = referer
    if client_headers.get("range"):
        hdrs["Range"] = client_headers["range"]
    for attempt in range(retries + 1):
        if wrote is not None:
            wrote.clear()          # 每次尝试前清空
        try:
            return await _proxy_once(target, hdrs, referer,
                                     client_headers, writer,
                                     wrote, timeout)
        except _TransientUpstream:
            if _already_wrote(wrote):
                log("upstream err after response started (no retry):",
                    target[:90])
                return False
            if attempt < retries:
                log(f"upstream retry {attempt+1}/{retries}:", target[:90])
                await asyncio.sleep(0.6 * (attempt + 1))
        except (BrokenPipeError, ConnectionResetError) as e:
            # 客户端已断开，重试无意义且会刷日志
            log("client gone:", target[:80], repr(e)[:60])
            return False
        except Exception as e:
            if _already_wrote(wrote):
                log("upstream err after response started (no retry):",
                    target[:90], repr(e)[:60])
                return False
            if attempt < retries:
                log(f"upstream retry {attempt+1}/{retries}:", target[:90],
                    repr(e)[:80])
                await asyncio.sleep(0.6 * (attempt + 1))
            else:
                log("proxy err:", repr(e)[:160])
                # 带上调用栈：媒体代理出错时光看一句 repr 定位不到
                log("TRACE:", traceback.format_exc())
    return False


class _TransientUpstream(Exception):
    """上游 5xx/429/空响应等瞬时错误，可重试"""


async def _proxy_once(target, hdrs, referer, client_headers, writer,
                    wrote=None, timeout=30):
    async with session.get(target, headers=hdrs, ssl=False,
                           allow_redirects=True,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status >= 500 or resp.status == 429:
                log("proxy fail", resp.status, target[:100])
                raise _TransientUpstream(resp.status)
            if resp.status >= 400:
                log("proxy fail", resp.status, target[:100])
                return False
            first = await resp.content.read(2048)
            if first.lstrip()[:7] == b"#EXTM3U":
                rest = b""
                while len(rest) < 4_000_000:
                    chunk = await resp.content.read(65536)
                    if not chunk:
                        break
                    rest += chunk
                text = (first + rest).decode("utf-8", "replace")
                props = None
                if "#EXT-X-STREAM-INF" in text:
                    # 主清单：片源方声明的分辨率常和真实流不符，
                    # 取实测值纠正（没探过就现探一次，上限 5s）
                    props = _URL_PROPS.get(str(resp.url))
                    if not (props and props.get("width")):
                        try:
                            props = await asyncio.wait_for(
                                probe_props(str(resp.url), referer, text),
                                timeout=5)
                        except Exception:
                            props = None
                new = rewrite_playlist(text, str(resp.url), referer,
                                       props).encode()

                if wrote is not None:
                    wrote.add(1)
                await write_head(writer, 200, {
                    "Content-Type": "application/vnd.apple.mpegurl",
                    "Content-Length": len(new),
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                })
                writer.write(new)
                await writer.drain()
                return True
            # AES-128 key: HLS 规定 #EXT-X-KEY 的密钥内容必须是 base64
            # 字符串，但该源返回 16 字节原始二进制。播放器按 base64 解码会
            # 失败 → AES 解密失败 → "播放失败"。这里转成 base64 再返回。
            clen = resp.headers.get("Content-Length")
            if (not _is_ts(first)
                    and str(resp.headers.get("Content-Type", "")).startswith(
                        "application/octet-stream")
                    and clen in ("16", "32")):
                n = int(clen)
                key_b = first
                while len(key_b) < n:
                    chunk = await resp.content.read(65536)
                    if not chunk:
                        break
                    key_b += chunk
                key_b = key_b[:n]
                if len(key_b) == n:
                    enc = base64.b64encode(key_b)
                    await write_head(writer, 200, {
                        "Content-Type": "text/plain",
                        "Content-Length": len(enc),
                        "Connection": "keep-alive",
                    })
                    writer.write(enc)
                    await writer.drain()
                    log("key b64", n, "B ->", enc[:24].decode())
                    return True

            # ---- 带假前缀的 ts 分片：切掉前缀再把真 TS 交给播放器 ----
            host = urllib.parse.urlsplit(str(resp.url)).netloc
            crng = resp.headers.get("Content-Range") or ""
            from0 = (resp.status == 200) or crng.startswith("bytes 0-")
            plen = _WRAP_HOSTS.get(host, 0)
            if from0 and not _is_ts(first) and host not in _WRAP_HOSTS:
                o, ok = _ts_strip(first)
                if ok and o:
                    plen = o
                    _WRAP_HOSTS[host] = o
                    log("ts 分片带假前缀 %dB，已切掉（%s）" % (o, host))
            if plen:
                if from0 and resp.status == 200:
                    body = first + await _read_rest(resp)
                else:
                    # 上游只给了区间（或首包不够），自己整段重拉一次
                    body = await _fetch_bytes(target, referer) or b""
                if body[:1] != b"\x47":
                    o2, ok2 = _ts_strip(body, len(body))
                    body = body[o2:] if (ok2 and o2) else b""
                if not body:
                    return False
                total = len(body)
                want = client_headers.get("range")
                if want:
                    mm = re.match(r"bytes=(\d*)-(\d*)", want.strip())
                    if mm:
                        s0 = int(mm.group(1) or 0)
                        s1 = int(mm.group(2)) if mm.group(2) else total - 1
                        s1 = min(s1, total - 1)
                        if s0 >= total or s0 > s1:
                            await write_head(writer, 416, {
                                "Content-Length": 0, "Connection": "close"})
                            return True
                        blob = body[s0:s1 + 1]
                        if wrote is not None:
                            wrote.add(1)
                        await write_head(writer, 206, {
                            "Content-Type": "video/mp2t",
                            "Content-Length": len(blob),
                            "Content-Range": "bytes %d-%d/%d" % (s0, s1, total),
                            "Accept-Ranges": "bytes",
                            "Connection": "keep-alive",
                        })
                        writer.write(blob)
                        await writer.drain()
                        return True
                if wrote is not None:
                    wrote.add(1)
                await write_head(writer, 200, {
                    "Content-Type": "video/mp2t",
                    "Content-Length": total,
                    "Accept-Ranges": "bytes",
                    "Connection": "keep-alive",
                })
                writer.write(body)
                await writer.drain()
                return True

            st = first.lstrip()[:16].lower()
            if st.startswith(b"<html") or st.startswith(b"<!doc") \
                    or st[:1] in (b"{", b"["):
                log("proxy non-media:", target[:100], first[:40])
                return False
            rh = {}
            ct = (resp.headers.get("Content-Type") or "").lower()
            # TS 分片：无论上游给什么类型，对播放器一律声明 video/mp2t。
            # 上游普遍返回 application/octet-stream，Apple AVPlayer 会因
            # 类型不符而拒绝拉流（表现为反复重拉清单、0 个分片）。
            if _is_ts(first):
                rh["Content-Type"] = "video/mp2t"
            elif ct.startswith("video/"):
                rh["Content-Type"] = ct
            elif ct:
                rh["Content-Type"] = ct
            else:
                rh["Content-Type"] = "video/mp2t"
            cl = resp.headers.get("Content-Length")
            if cl:
                rh["Content-Length"] = cl
                rh["Connection"] = "keep-alive"
            else:
                rh["Connection"] = "close"
            rh["Accept-Ranges"] = "bytes"
            if resp.headers.get("Accept-Ranges"):
                rh["Accept-Ranges"] = resp.headers["Accept-Ranges"]
            if resp.status == 206 and resp.headers.get("Content-Range"):
                rh["Content-Range"] = resp.headers["Content-Range"]
            if resp.status == 200 and client_headers.get("range"):
                # 上游忽略 Range 而返回整文件：截断到请求区间，
                # 否则播放器会因长度不符而中断。
                want = client_headers["range"]
                mm = re.match(r"bytes=(\d*)-(\d*)", want.strip())
                if mm:
                    s0, s1 = mm.group(1), mm.group(2)
                    if s0:
                        st_i = int(s0)
                        en_i = int(s1) if s1 else st_i + len(first) - 1
                        if len(first) == 2048 and cl:
                            total = int(cl)
                            en_i = min(en_i, total - 1)
                        rest_all = b""
                        while len(first) + len(rest_all) <= en_i - st_i + 1:
                            chunk = await resp.content.read(65536)
                            if not chunk:
                                break
                            rest_all += chunk
                        full = first + rest_all
                        blob = full[st_i:en_i + 1]
                        rh = {
                            "Content-Type": rh["Content-Type"],
                            "Content-Length": len(blob),
                            "Content-Range": f"bytes {st_i}-{en_i}/{cl or '*'}",
                            "Accept-Ranges": "bytes",
                            "Connection": "keep-alive",
                        }
                        if wrote is not None:
                            wrote.add(1)
                        await write_head(writer, 206, rh)
                        writer.write(blob)
                        await writer.drain()
                        return True
            if wrote is not None:
                wrote.add(1)
            await write_head(writer, resp.status, rh)
            writer.write(first)
            await writer.drain()
            async for chunk in resp.content.iter_chunked(65536):
                writer.write(chunk)
                await writer.drain()
            if not cl:
                writer.close()
            return True


async def _probe_duration(guid, rec):
    """播放成功后异步抓取清单，统计总时长缓存下来（供详情页显示）"""
    try:
        eps = rec.get("eps") or []
        if not eps:
            return
        u0 = eps[0].get("url")
        if not u0 or not _looks_m3u8(u0):
            return
        ref = rec.get("referer") or ""
        hdrs = {"User-Agent": UA}
        if ref:
            hdrs["Referer"] = ref
        async with session.get(u0, headers=hdrs,
                               ssl=False,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                return
            txt = await r.text()
        # 主清单：只有一行说明是变体清单入口，需要再取一层
        base = u0
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        var = [l for l in lines if not l.startswith("#")]
        if len(var) == 1 and txt.lstrip().startswith("#EXTM3U") \
                and "#EXTINF" not in txt:
            vurl = urllib.parse.urljoin(u0, var[0])
            async with session.get(vurl, headers=hdrs,
                                   ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=12)) as r2:
                if r2.status == 200:
                    txt = await r2.text()
                    base = vurl
        # 与广告剔除保持同一套判断：只统计真正会播的分组，否则详情页
        # 显示的时长会把被剔掉的片头/片尾广告也算进去（实测差 50 秒）
        try:
            kept = rewrite_playlist(txt, base, ref)
        except Exception:
            kept = txt
        total = 0.0
        nseg = 0
        for l in kept.splitlines():
            ls = l.strip()
            if ls.startswith("#EXTINF:"):
                try:
                    total += float(ls[8:].rstrip(","))
                    nseg += 1
                except Exception:
                    pass
        if total > 0:
            rec["duration"] = int(total)
            rec["dur_v"] = DUR_V          # 标记算法版本，避免重复回填
            save_registry()
            log("duration set:", (rec.get("title") or "")[:18],
                f"{int(total)//60}min/{nseg}seg")
        else:
            log("duration probe: 未解析到 EXTINF",
                (rec.get("title") or "")[:18], f"len={len(txt)}")
    except Exception as e:
        log("duration probe err:", (rec.get("title") or "")[:16], repr(e)[:100])


async def fill_durations(force=False):
    """后台并发补齐所有在线条目的时长。

    必须做：前端靠 item 的 duration/runtime 判断「这是可播放条目」，
    为 0 时播放器永远停在「视频加载中」；而时长原本只在播放成功后回填，
    于是形成死循环（没时长不播 → 不播没时长）。这里在预热阶段并发补齐。
    """
    sem = asyncio.Semaphore(8)

    async def one(guid, rec):
        if not force and rec.get("duration"):
            return
        eps = rec.get("eps") or []
        if not eps:
            return
        async with sem:
            await _probe_duration(guid, rec)

    todo = [(g, r) for g, r in registry.items()
            if isinstance(r, dict) and (r.get("eps") or [])]
    if not todo:
        return
    n0 = sum(1 for g, r in todo if not r.get("duration"))
    log(f"fill_durations: 待补 {n0}/{len(todo)} 条")
    for i in range(0, len(todo), 40):
        await asyncio.gather(*[one(g, r) for g, r in todo[i:i + 40]],
                             return_exceptions=True)
        done = sum(1 for g, r in todo if r.get("duration"))
        log(f"fill_durations: {done}/{len(todo)} 已有时长")
        save_registry(force=True)


async def resolve_all_pending(limit=None):
    """后台把豆瓣库里所有「无片源」的条目解析一遍。

    原本解析完全靠用户点开时按需触发，库里大量条目处于未解析状态，
    表现就是点开卡「视频加载中」。这里开机后台先跑一轮。
    """
    sem = asyncio.Semaphore(4)

    async def one(g, rec):
        async with sem:
            try:
                await _resolve_title(rec)
            except Exception as e:
                log("resolve err:", (rec.get("title") or "")[:14], repr(e)[:80])
            save_registry()

    todo = [(g, r) for g, r in registry.items()
            if isinstance(r, dict) and r.get("cat") and not (r.get("eps") or [])]
    if limit:
        todo = todo[:limit]
    if not todo:
        return
    log(f"resolve_all_pending: {len(todo)} 条待解析")
    for i in range(0, len(todo), 20):
        await asyncio.gather(*[one(g, r) for g, r in todo[i:i + 20]],
                             return_exceptions=True)
        done = sum(1 for g, r in registry.items()
                   if isinstance(r, dict) and r.get("cat") and (r.get("eps") or []))
        log(f"resolve_all_pending: 已解析 {done} 条")
    save_registry(force=True)


_maint_task = None


async def maintenance(reason="timer"):
    """每日维护：刷豆瓣榜单 → 解析新片/掉片 → 补时长。

    由 systemd timer（每天 00:20）在进程内触发，保证 registry 只有
    本进程一个写入者，不会和前台请求抢文件。
    """
    global _maint_task
    if _maint_task and not _maint_task.done():
        return "already running"
    _maint_task = asyncio.create_task(_run_maintenance(reason))
    return "started"


async def _run_maintenance(reason):
    t0 = time.time()
    log(f"=== 每日维护开始 ({reason}) ===")
    try:
        # 1) 榜单强制刷新（清 TTL）
        for ck, *_ in DOUBAN_CATS:
            _douban.pop(ck, None)
            _douban.pop(ck + "_ts", None)
        allg = await douban_guids()
        log("榜单已刷新:", sum(len(v) for v in allg.values()), "条")

        # 2) 解析：新增条目 + 掉片条目
        await resolve_all_pending()

        # 3) 时长：只补缺失的
        await fill_durations()

        cat = [r for g, r in registry.items()
               if isinstance(r, dict) and r.get("cat")]
        ok = [r for r in cat if r.get("eps")]
        log(f"维护完成: 可播 {len(ok)}/{len(cat)}，"
            f"耗时 {time.time() - t0:.0f}s")
    except Exception as e:
        log("维护出错:", repr(e)[:200])
    finally:
        save_registry(force=True)


def _alt_sources(guid, rec):
    """同片的其它候选源：同分类、同标题、不同 guid 的条目
    （采集站同一部剧常有多个片源条目，互为备份）。
    返回 {guid: [该条目每集的url...]}，按集数优先匹配。"""
    out = []
    title = (rec.get("title") or "").strip()
    if not title:
        return out
    n_here = len(rec.get("eps") or [])
    for g, r in registry.items():
        if g == guid or not isinstance(r, dict):
            continue
        if (r.get("title") or "").strip() != title:
            continue
        eps = r.get("eps") or []
        if not eps:
            continue
        out.append((abs(len(eps) - n_here), g, eps))
    out.sort(key=lambda x: x[0])          # 集数最接近的优先
    return [(g, e) for _d, g, e in out]


def build_line_index(guid, rec, ep_index):
    """构造该集的候选源列表（多源合并），供换源使用。
    返回 [(来源说明, 该集url), ...]"""
    eps = rec.get("eps") or []
    cands = []
    if not eps:
        return cands
    ep_index = max(0, min(ep_index, len(eps) - 1))
    good = (rec.get("good") or {}).get(str(ep_index))
    if good:
        cands.append(("上次可用", good))
    e = eps[ep_index]
    for i, u in enumerate(e.get("urls") or []):
        if u not in [c[1] for c in cands]:
            cands.append((f"线路{i+1}" if i else "主线路", u))
    if e.get("url") and e["url"] not in [c[1] for c in cands]:
        cands.append(("主线路", e["url"]))
    # 同片其它源
    for g2, eps2 in _alt_sources(guid, rec):
        if ep_index < len(eps2):
            u2 = eps2[ep_index].get("url")
            if u2 and u2 not in [c[1] for c in cands]:
                r2 = registry.get(g2) or {}
                cands.append((("备源·" + (r2.get("source") or r2.get("site") or "同片"))[:16], u2))
    return cands


def play_candidates(rec, ep_index, line=None, guid=None):
    """某集的候选播放地址：缓存的可用地址优先，然后各线路同集地址，
    最后是同片的其它源（换源）。line=N 可指定用第 N 个候选。"""
    cands = build_line_index(guid, rec, ep_index)
    urls = [u for _lbl, u in cands]
    if line is not None and 0 <= line < len(urls):
        return [urls[line]] + [u for i, u in enumerate(urls) if i != line]
    return urls


async def serve_manifest(guid, ep_index, headers, writer, line=None):
    """在线影片的主清单入口：解析片源 → 逐个候选尝试 → 写响应"""
    rec = registry.get(guid)
    if not rec:
        await write_head(writer, 404, {"Content-Length": 0, "Connection": "close"})
        writer.close()
        return
    if hgdj_is_rec(rec):
        if not await serve_hgdj_media(guid, ep_index, headers, writer):
            await write_head(writer, 502, {"Content-Length": 0,
                                           "Connection": "close"})
            writer.close()
        return
    await ensure_resolved(guid)
    rec = registry.get(guid) or {}
    cands = play_candidates(rec, ep_index, line, guid)
    if not cands:
        log("no candidates for", guid, rec.get("title"))
        await write_head(writer, 404, {"Content-Length": 0, "Connection": "close"})
        writer.close()
        return
    referer = rec.get("referer", "")
    if line is not None and 0 <= line < len(cands):
        rec.setdefault("good", {})[str(ep_index)] = cands[line]
        save_registry()
    for i, u in enumerate(cands):
        log(f"manifest[{ep_index}] try#{i}:", u[:110])
        ok = await proxy_media(u, referer, headers, writer)
        if ok and rec.get("dur_v") != DUR_V:
            # 清单已成功送达，顺带算出总时长缓存下来（供详情页显示）。
            # dur_v 标记算法版本：旧值把被剔掉的片头/片尾广告也算进去了
            asyncio.create_task(_probe_duration(guid, rec))
        if ok:
            if i > 0:      # 备用线路命中，记住下次直选
                rec.setdefault("good", {})[str(ep_index)] = u
                save_registry()
            return
    log("all candidates failed:", guid)
    await write_head(writer, 502, {"Content-Length": 0, "Connection": "close"})
    writer.close()


# -------------------------------------------------------------- 分发 ---
async def handle_request(reader, writer, head, headers, body):
    reqline = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = reqline.split(" ")
    if len(parts) < 2:
        return
    method, target = parts[0], parts[1]
    path, query = parse_query(target)

    # 看门狗健康探测（systemd watchdog 用，判断 socket 是否仍被 MITM 接管）
    if path == "/v/__mitm__/refresh":
        # 供 systemd timer 触发每日维护（走 unix socket，不经过 nginx）
        r = await maintenance("api")
        rel = reload_sources()          # 顺带热加载源清单
        writer.write(json_response({"mitm": 1, "refresh": r, "sources": rel,
                                    "registry": len(registry)}))
        await writer.drain()
        return

    if path == "/v/__mitm__/sources":
        # 查看当前生效的源清单；?reload=1 先热加载 conf/sources.json
        rel = reload_sources() if "reload" in query else None
        writer.write(json_response({
            "mitm": 1,
            "origin": SOURCES_ORIGIN,
            "reload": rel,
            "file": SOURCES_FILE,
            "primary": CMS_PRIMARY,
            "fallback": CMS_FALLBACK,
            "js": JS_SITES,
            **source_policy_payload(),
        }))
        await writer.drain()
        return

    if path == "/v/__mitm__/health":
        writer.write(json_response({"mitm": 1, "pid": os.getpid(),
                                    "registry": len(registry)}))
        await writer.drain()
        return

    # ---- 配置页（爱迷茫 · 在线源设置）-------------------------------------
    # 经 nginx 走 http://<NAS>:5666/v/__mitm__/config 就能打开，不占新端口。
    if path == "/v/__mitm__/config" or path.startswith("/v/__mitm__/api/"):
        if webui is None:
            writer.write(text_response("配置页不可用（webui.py 没装上）：" + _WEBUI_ERR,
                                       status=500))
        elif method == "POST" and not post_allowed(headers):
            ok, src_ip, why = write_gate(headers)
            writer.write(json_response({
                "ok": False, "allowed": False, "ip": src_ip, "why": why,
                "msg": "不允许从当前来源改配置：%s（来源 IP %s）。三条路任选一条 —— "
                       "① 用「之前已经改过配置的那台浏览器」打开本页，它带着写凭证、"
                       "跟 IP 无关，在那上面点『把当前来源 IP 加进白名单』或『不限制来源』就行；"
                       "② 在 NAS 上执行这条（走 unix socket 永远放行）："
                       "curl -s --unix-socket /var/run/trim_media.sock -X POST "
                       "-H 'Content-Type: application/json' "
                       "-d '{\"write_allow_any\":true}' http://x/v/__mitm__/api/config"
                       " ③ 编辑 %s，把 %s 加进 write_allow_ip（支持单个 IP 或 1.2.3.0/24 这种网段）。"
                       % (why, src_ip, CONFIG_FILE, src_ip),
            }, status=403))
        elif path == "/v/__mitm__/config":
            writer.write(text_response(webui.PAGE, extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/sources" and method == "POST":
            req = _json_body(body)
            ok, msg = save_sources(req.get("primary") or [],
                                   req.get("fallback") or [],
                                   req.get("js") or [])
            patch = {}
            if "online_visibility" in req:
                vis = str(req.get("online_visibility") or "").strip().lower()
                if vis in ("all", "admin", "users"):
                    patch["online_visibility"] = vis
                else:
                    ok = False
                    msg += "；可见范围不合法"
            if "online_users" in req:
                v = req.get("online_users") or []
                if not isinstance(v, list):
                    v = [v]
                patch["online_users"] = [str(x).strip() for x in v
                                         if str(x).strip()]
            if "source_mode" in req:
                sm = str(req.get("source_mode") or "").strip().lower()
                if sm in ("priority", "parallel"):
                    patch["source_mode"] = sm
                else:
                    ok = False
                    msg += "；查询方式不合法"
            if "source_limit" in req:
                patch["source_limit"] = _limit_int(req.get("source_limit"), 0, 100)
            if "js_source_limit" in req:
                patch["js_source_limit"] = _limit_int(
                    req.get("js_source_limit"), 4, 50)
            if patch:
                ok2, msg2 = save_config(patch)
                if not ok2:
                    ok = False
                    msg += "；" + msg2
                elif ok:
                    msg += "；配置已保存"
            writer.write(json_response({"ok": ok, "msg": msg,
                                        "file": SOURCES_FILE,
                                        "origin": SOURCES_ORIGIN,
                                        "primary": CMS_PRIMARY,
                                        "fallback": CMS_FALLBACK,
                                        "js": JS_SITES,
                                        **visibility_payload(),
                                        **source_policy_payload()},
                                       extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/sources":
            writer.write(json_response({"ok": True, "msg": _SOURCES_MSG,
                                        "file": SOURCES_FILE,
                                        "origin": SOURCES_ORIGIN,
                                        "primary": CMS_PRIMARY,
                                        "fallback": CMS_FALLBACK,
                                        "js": JS_SITES,
                                        "write": write_state(headers),
                                        "engine": jsspider.info() if jsspider else
                                                  {"available": False, "why": _JS_ERR},
                                        **visibility_payload(),
                                        **source_policy_payload()},
                                       extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/users":
            writer.write(json_response({"ok": True, **visibility_payload()},
                                       extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/config" and method == "POST":
            req = _json_body(body)
            patch = {}
            if "write_allow_any" in req:
                patch["write_allow_any"] = bool(req.get("write_allow_any"))
            if "write_allow_ip" in req:
                v = req.get("write_allow_ip") or []
                patch["write_allow_ip"] = v if isinstance(v, list) else [v]
            if "online_visibility" in req:
                vis = str(req.get("online_visibility") or "").strip().lower()
                if vis in ("all", "admin", "users"):
                    patch["online_visibility"] = vis
            if "online_users" in req:
                v = req.get("online_users") or []
                patch["online_users"] = ([str(x).strip() for x in v
                                          if str(x).strip()]
                                         if isinstance(v, list) else [str(v).strip()])
            if "source_mode" in req:
                sm = str(req.get("source_mode") or "").strip().lower()
                if sm in ("priority", "parallel"):
                    patch["source_mode"] = sm
            if "source_limit" in req:
                patch["source_limit"] = _limit_int(req.get("source_limit"), 0, 100)
            if "js_source_limit" in req:
                patch["js_source_limit"] = _limit_int(
                    req.get("js_source_limit"), 4, 50)
            # 便捷写法：把「当前请求的来源 IP」直接加进白名单
            if req.get("add_ip"):
                cfg = load_config()
                cur = write_gate(headers)[1]
                lst = list(cfg.get("write_allow_ip") or [])
                if cur and cur not in lst and cur != "本机(unix socket)":
                    lst.append(cur)
                patch["write_allow_ip"] = lst
            ok, msg = save_config(patch) if patch else (True, "没给要改的字段")
            writer.write(json_response({"ok": ok, "msg": msg,
                                        "config": public_config(),
                                        **visibility_payload(),
                                        **source_policy_payload(),
                                        **write_state(headers)},
                                       extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/config":
            writer.write(json_response({"ok": True, "config": public_config(),
                                        **visibility_payload(),
                                        **source_policy_payload(),
                                        **write_state(headers)},
                                       extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/import" and method == "POST":
            req = _json_body(body)
            writer.write(json_response(
                await asyncio.to_thread(_do_import, str(req.get("url") or "").strip()),
                extra=ensure_cookie(headers)))
        elif path == "/v/__mitm__/api/test" and method == "POST":
            req = _json_body(body)
            writer.write(json_response(
                await asyncio.to_thread(_do_test, req.get("sites") or [],
                                        str(req.get("keyword") or "凡人"),
                                        req.get("timeout"),
                                        req.get("source_limit"),
                                        req.get("js_source_limit"),
                                        req.get("source_mode")),
                extra=ensure_cookie(headers)))
        else:
            writer.write(text_response("未知接口", status=404))
        await writer.drain()
        return

    g = resolve_guid(path)
    body_guid = None
    if body:
        m = re.search(rb"on[0-9a-f]{30}", body)
        if m:
            cand = m.group(0).decode()
            if cand in registry or cand in ALL_LIB_GUIDS:
                body_guid = cand
    if not path.startswith("/v/media/"):
        log(method, path, f"[on:{g or body_guid}]" if (g or body_guid) else "")

    # 当前请求是谁 + 在线源是否对他可见。用户/策略都有短缓存，本地分片请求
    # 也只是两次内存命中，不会每次都读数据库或配置文件。
    need_ctx = (path.startswith("/v/api/v1/") or path.startswith("/v/media/"))
    req_user = request_user(headers) if need_ctx else None
    allow_online = online_allowed(req_user) if need_ctx else True

    # ---- 1) 搜索合并 ----
    if path == "/v/api/v1/search/list":
        if not allow_online:
            raw = await forward(head, headers, body)
            writer.write(raw)
            await writer.drain()
            return
        kw = (query.get("q") or [""])[0]
        online_t = asyncio.create_task(online_search(kw)) if kw else None
        raw = await forward(head, headers, body)
        _, body_b = split_response(raw)
        local = None
        try:
            local = json.loads(body_b.decode("utf-8", "replace"))
        except Exception:
            pass
        online = await online_t if online_t else []
        if _DIAG == "2" and local is not None:
            log("DIAG-SEARCH local_code=", local.get("code"),
                "local_n=", len(local.get("data") or []) if isinstance(
                    local.get("data"), list) else "-")
        if local is not None:
            if local.get("code") == 0:
                local["data"] = online + list(local.get("data") or [])
                writer.write(json_response(local))
            elif online:
                writer.write(json_response({"msg": "", "code": 0, "data": online}))
            else:
                writer.write(raw)
        elif online:
            writer.write(json_response({"msg": "", "code": 0, "data": online}))
        else:
            writer.write(raw)
        await writer.drain()
        return

    # ---- 2) 媒体代理（在线影片 m3u8/分片/海报） ----
    # 2a) 官方图片接口拼出的在线海报：
    #     前端把海报地址直接拼在 /v/api/v1/sys/img 之后（无分隔符），
    #     如 /v/api/v1/sys/imghttps://img.x.com/a.jpg —— 官方按本地文件
    #     查找必然失败。这里识别并改为远程代理，海报才能显示。
    if path.startswith(SYS_IMG_PREFIX):
        tail = path[len(SYS_IMG_PREFIX):]
        if tail.startswith("http://") or tail.startswith("https://"):
            tail = urllib.parse.unquote(tail)
            ref = ""
            if "doubanio" in tail or "douban" in tail:
                ref = "https://movie.douban.com/"     # 豆瓣图防盗链
            for dom, cdn in HOSTS_MIRROR:             # 图片域名镜像
                if dom in tail:
                    tail = tail.replace(dom, cdn)
                    break
            log("img(sys):", tail[:100])
            if not await proxy_media(tail, ref, headers, writer,
                                     retries=0, timeout=8):
                await write_head(writer, 404, {"Content-Length": 0,
                                                "Connection": "close"})
                writer.close()
            return

    # 前端可能带 /v/api/v1 前缀（如 /v/api/v1/media/range/<guid>），统一剥离
    if path.startswith("/v/api/v1/media/"):
        path = "/v/media/" + path[len("/v/api/v1/media/"):]
    if path.startswith("/v/media/"):
        if path.startswith("/v/media/online/") and not allow_online:
            await write_head(writer, 404, {"Content-Length": 0,
                                           "Connection": "close"})
            writer.close()
            return
        if not path.startswith("/v/media/online/"):
            # 本地库影片流的取流请求（原先完全不记日志，导致排查时“看不见”）
            log("MEDIA-REQ", method, path[:60],
                "pl=" + str((query.get("playlink") or [""])[0])[:120],
                "range=" + str(headers.get("range") or "-")[:32])
        if path.startswith("/v/media/hgdj/"):
            gid = g
            if not gid:
                m = GUID_RE.search(path)
                gid = m.group(0) if m else None
            if not gid or not allow_online or gid not in registry:
                await write_head(writer, 404, {"Content-Length": 0,
                                               "Connection": "close"})
                writer.close()
                return
            try:
                ep = int((query.get("ep") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                ep = 0
            if not ep:
                ep = hgdj_ep_num(registry.get(gid) or {})
            log("HGDJ-MEDIA", gid[:18], "ep=", ep,
                "range=" + str(headers.get("range") or "-")[:32])
            if not await serve_hgdj_media(gid, ep, headers, writer):
                await write_head(writer, 502, {"Content-Length": 0,
                                               "Connection": "close"})
                writer.close()
            return
        if path == "/v/media/online/img":
            u = urllib.parse.unquote((query.get("u") or [""])[0])
            if u.startswith("http"):
                r = urllib.parse.unquote((query.get("r") or [""])[0])
                # 防盗链：豆瓣图必须带 Referer 才返回图片；
                # 源站域名给出可用 CDN 镜像。
                if not r:
                    if "doubanio" in u or "douban" in u:
                        r = "https://movie.douban.com/"
                for dom, cdn in HOSTS_MIRROR:
                    if dom in u:
                        u = u.replace(dom, cdn)
                        break
                log("img:", u[:110])
                if not await proxy_media(u, r, headers, writer):
                    await write_head(writer, 502, {"Content-Length": 0,
                                                   "Connection": "close"})
                    writer.close()
                return
        # 分片/外部资源直连代理（u= 目标地址）
        if path == "/v/media/online/seg":
            u = urllib.parse.unquote((query.get("u") or [""])[0])
            r = urllib.parse.unquote((query.get("r") or [""])[0])
            if u.startswith("http"):
                log("seg:", u[:110])
                if not await proxy_media(u, r, headers, writer):
                    # 必须给客户端一个明确失败响应，否则播放器永远等 → “加载中”
                    await write_head(writer, 502,
                                     {"Content-Length": 0, "Connection": "close"})
                    writer.close()
                return
        # 直链 playlink（前端 Kpe 拼接：/v/media/range/<guid>?playlink=...）
        pl = (query.get("playlink") or [""])[0]
        g2 = g
        try:
            ep = int((query.get("ep") or ["0"])[0] or 0)
        except (TypeError, ValueError):
            ep = 0          # 畸形 ep 参数容错，避免整请求 502
        line = None
        if (query.get("line") or [""])[0] not in ("", None):
            try:
                line = int((query.get("line") or ["0"])[0])
            except (TypeError, ValueError):
                line = None
        if pl.startswith("http"):
            # 只有「在线条目」的直链才由 MITM 代下载。
            # 官方本地库的 playlink 也是绝对地址，但它指向本机
            # 的 /v/... 接口——必须原样转发：Range 续传、鉴权、实时转码都在
            # 官方侧。用 proxy_media（为在线 CDN 分片写、30s 整体超时）代理
            # 本地大文件会一直“加载中”。
            _pp = urllib.parse.urlparse(pl)
            _local_pl = ((_pp.path or "").startswith("/v/")
                         or (_pp.hostname or "") in ("127.0.0.1", "localhost",
                                                     "::1", "0.0.0.0"))
            if resolve_guid(pl) and not allow_online:
                await write_head(writer, 404, {"Content-Length": 0,
                                               "Connection": "close"})
                writer.close()
                return
            if resolve_guid(pl) or not _local_pl:
                if not await proxy_media(pl, urllib.parse.unquote(
                        (query.get("r") or [""])[0]), headers, writer):
                    await write_head(writer, 502,
                                     {"Content-Length": 0,
                                      "Connection": "close"})
                    writer.close()
                return
            log("MEDIA-LOCAL", method, path[:56], "pl=" + pl[:110],
                "range=" + str(headers.get("range") or "-")[:32])
            await forward_stream(head, body, writer)
            return
        if pl and not g2:
            pl_path, pl_q = parse_query(pl)
            g2 = resolve_guid(pl_path)
            if not ep:
                ep = int((pl_q.get("ep") or ["0"])[0] or 0)
        if g2 and g2 in registry:
            if not allow_online:
                await write_head(writer, 404, {"Content-Length": 0,
                                               "Connection": "close"})
                writer.close()
                return
            await serve_manifest(g2, ep, headers, writer, line)
            return
        # 其余（本地影片流）原样转发
        await forward_stream(head, body, writer)
        return

    # ---- 3) 豆瓣热播 伪媒体库 ----
    if allow_online and method == "GET" and path.startswith("/v/api/v1/mdb/"):
        lg = path[len("/v/api/v1/mdb/"):]
        if lg == HGDJ_LIB_GUID:
            await fetch_hgdj_all()
            writer.write(json_response({"msg": "", "code": 0,
                                        "data": hgdj_lib_info()}))
            await writer.drain()
            return
        for ck, g in LIB_GUIDS.items():
            if lg == g:
                await fetch_douban_cat(ck)
                await douban_guids(ck)
                writer.write(json_response({"msg": "", "code": 0,
                                            "data": fake_mdb_info(ck)}))
                await writer.drain()
                return
    if allow_online and path == "/v/api/v1/item/list" and body:
        try:
            bj = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            bj = {}
        if _DIAG and _DIAG == "2":
            log("DIAG-ITEMLIST body=", repr(bj)[:220])
        # 前端不同页面用不同字段名（parent_guid / ancestor_guid / folder_guid
        # / library_guid / guid），且可能是字符串或数组，统一摊平成 set
        parents = set()
        for k in ("parent_guid", "ancestor_guid", "folder_guid",
                  "library_guid", "guid", "ancestor_guids", "parent_guids"):
            v = bj.get(k)
            if isinstance(v, (list, tuple, set)):
                parents.update(x for x in v if isinstance(x, str))
            elif isinstance(v, str):
                parents.add(v)
        cks = [ck for ck, g in LIB_GUIDS.items() if g in parents]
        if FAKE_LIB_GUID in parents and not cks:
            cks = ["hot_movie", "hot_tv"]      # 旧单库 → 兼容映射
        want_hgdj = HGDJ_LIB_GUID in parents
        if cks or want_hgdj:
            try:
                page = max(1, int(bj.get("page") or 1))
            except Exception:
                page = 1
            try:
                page_size = max(0, int(bj.get("page_size") or 0))
            except Exception:
                page_size = 0
            items = []
            for ck in cks:
                await douban_guids(ck)
                for g in douban_cat_guids(ck):
                    items.append(build_item(g, registry[g]))
            total_hgdj = 0
            if want_hgdj:
                await fetch_hgdj_all()
                total_hgdj = _hgdj_total()
                # 飞牛前端是按 page/page_size 拉取的。红果上游有约 2400 部，
                # 首屏只加载 108 部；滚动到后面时再按需补对应页，避免一次
                # 打 100 多个请求，也避免“划到底就没了”。
                if page_size:
                    await ensure_hgdj_loaded(
                        min(total_hgdj, page * page_size))
                for g in hgdj_series_guids():
                    rec = registry.get(g)
                    if isinstance(rec, dict):
                        items.append(build_item(g, rec))
            total = len(items)
            if want_hgdj:
                total = max(total, total_hgdj + len(items) - len(
                    hgdj_series_guids()))
            if page_size:
                start = (page - 1) * page_size
                items = items[start:start + page_size]
            log("fake library list:", len(items), "total", total,
                "page", page, "size", page_size, cks,
                "hgdj" if want_hgdj else "")
            writer.write(json_response({"msg": "", "code": 0,
                                        "data": {"total": total,
                                                 "list": items,
                                                 "page": page,
                                                 "page_size": page_size or len(items)}}))
            await writer.drain()
            return
    if path == "/v/api/v1/mediadb/list":
        if not allow_online:
            raw = await forward(head, headers, body)
            writer.write(raw)
            await writer.drain()
            return
        raw = await forward(head, headers, body)
        _, body_b = split_response(raw)
        try:
            j = json.loads(body_b.decode("utf-8", "replace"))
            code = j.get("code")
            libs = list(j.get("data") or [])
            if code != 0:
                # 官方本身报错（如 invalid sign / 未授权）时绝不能注入伪库：
                # code 非 0 前端会丢弃整个 data，结果本地库也跟着消失。
                log(f"mediadb/list 官方 code={code} msg={str(j.get('msg'))[:30]}"
                    f"，原样透传（本地库 {len(libs)} 个）")
                writer.write(raw)
            else:
                asyncio.create_task(fetch_douban_all())   # 预热各分类
                if hgdj_config()[0]:
                    asyncio.create_task(fetch_hgdj_all())
                fake = fake_mdb_list()
                j["data"] = libs + fake
                log(f"mediadb/list: 官方 {len(libs)} 个 + 伪库 {len(fake)} "
                    f"= {len(j['data'])}")
                writer.write(json_response(j))
        except Exception as e:
            log("mediadb/list err:", repr(e)[:100])
            writer.write(raw)
        await writer.drain()
        return
    if path == "/v/api/v1/mediadb/sum":
        if not allow_online:
            raw = await forward(head, headers, body)
            writer.write(raw)
            await writer.drain()
            return
        raw = await forward(head, headers, body)
        _, body_b = split_response(raw)
        try:
            j = json.loads(body_b.decode("utf-8", "replace"))
            if j.get("code") != 0:
                log(f"mediadb/sum 官方 code={j.get('code')}，原样透传")
                writer.write(raw)
            else:
                d = j.get("data")
                if not isinstance(d, dict):
                    d = j["data"] = {}
                for ck in LIB_GUIDS:
                    n = len(douban_cat_guids(ck))
                    if n:
                        d[LIB_GUIDS[ck]] = n
                if hgdj_config()[0] and hgdj_series_guids():
                    d[HGDJ_LIB_GUID] = len(hgdj_series_guids())
                log(f"mediadb/sum: {len(d)} 个库计数")
                writer.write(json_response(j))
        except Exception:
            writer.write(raw)
        await writer.drain()
        return

    # ---- 4) 在线条目相关接口 ----
    # 4a) 伪媒体库 guid：前端进入豆瓣分类库详情页时会请求它
    raw_g = GUID_RE.search(path)
    if allow_online and raw_g and raw_g.group(0) in ALL_LIB_GUIDS:
        lg = raw_g.group(0)
        if lg == HGDJ_LIB_GUID:
            if (path.startswith("/v/api/v1/item/")
                    or path.startswith("/v/api/v1/mdb/")):
                await fetch_hgdj_all()
                writer.write(json_response({"msg": "", "code": 0,
                                            "data": hgdj_lib_info()}))
                await writer.drain()
                return
        else:
            ck = lib_key_of_guid(lg)
            if ck is None:                  # 旧单库 → 默认取热门电影
                ck = "hot_movie"
            if (path.startswith("/v/api/v1/item/")
                    or path.startswith("/v/api/v1/mdb/")):
                await fetch_douban_cat(ck)
                await douban_guids(ck)
                writer.write(json_response({"msg": "", "code": 0,
                                            "data": fake_mdb_info(ck)}))
                await writer.drain()
                return

    # ---- 观看记录 / 继续播放 ----
    # 关键：本地影片的 play/record、play/quit、play/list 必须原样交给官方。
    # 旧版把这三个接口全拦了，本地进度写不进 item_user_play，症状就是
    # 「本地 NAS 电影的播放记录 / 继续播放不显示了」。
    if path.startswith("/v/api/v1/play/list"):
        raw = await forward(head, headers, body)
        rhead, rbody = split_response(raw)
        status = _http_status(rhead)
        try:
            j = json.loads(rbody.decode("utf-8", "replace"))
        except Exception:
            j = None
        online = watch_list(req_user) if allow_online else []
        # 官方明确返回 501（接口未实现）或根本不是 JSON 时，本地列表拿不到，
        # 至少把在线列表还给前端；但 401/403 等登录/鉴权错误必须原样透传，
        # 不能用一个「看起来成功」的在线列表把登录问题盖掉。
        if status == 501:
            writer.write(json_response({"msg": "", "code": 0, "data": online}))
            await writer.drain()
            log("play/list: 官方 501，退化为在线 %d 条" % len(online))
            return
        if status in (401, 403):
            writer.write(raw)
            await writer.drain()
            log("play/list: 官方 HTTP %s，原样透传" % status)
            return
        if not isinstance(j, dict):
            writer.write(json_response({"msg": "", "code": 0, "data": online}))
            await writer.drain()
            log("play/list: 官方无 JSON（status=%s），在线 %d 条"
                % (status, len(online)))
            return
        if j.get("code") != 0:
            # 官方明确报错（未登录 / 签名错 / 其它业务错误）时原样透传
            writer.write(raw)
            await writer.drain()
            log("play/list: 官方 code=%s，原样透传" % j.get("code"))
            return
        if status >= 400:
            writer.write(raw)
            await writer.drain()
            log("play/list: 官方 HTTP %s，原样透传" % status)
            return
        data = j.get("data")
        local_n = 0
        if isinstance(data, list):
            local_n = len(data)
            j["data"] = _merge_play_items(data, online)
        elif isinstance(data, dict) and isinstance(data.get("list"), list):
            local_n = len(data["list"])
            data["list"] = _merge_play_items(data["list"], online)
            if "total" in data:
                data["total"] = len(data["list"])
            j["data"] = data
        else:
            j["data"] = online if allow_online else data
        writer.write(json_response(j))
        await writer.drain()
        log("play/list: 官方本地 %d + 在线 %d -> %d 条"
            % (local_n, len(online),
               len(j["data"]) if isinstance(j.get("data"), list)
               else len(j["data"].get("list", []))
               if isinstance(j.get("data"), dict) else 0))
        return
    if path.startswith("/v/api/v1/play/record"):
        pd = _play_body(body)
        gid = _play_gid(pd, body_guid, g)
        main_gid = _play_main_guid(gid)
        if main_gid:
            if not allow_online:
                writer.write(json_response(
                    {"msg": "在线源未对该用户开放", "code": 403, "data": None},
                    status=403))
                await writer.drain()
                return
            if method == "DELETE":
                remove_watch(main_gid, req_user)
                log("play/record 删除:", main_gid)
            else:
                pos = pd.get("ts")
                if pos is None:
                    pos = pd.get("position") or 0
                note_watch(main_gid, pos,
                           {"title": registry[main_gid].get("title", "")},
                           user=req_user)
                try:
                    pos_s = int(float(pos or 0))
                except Exception:
                    pos_s = 0
                log(f"play/record: {(registry[main_gid].get('title') or '')[:16]} "
                    f"{pos_s}s")
            writer.write(json_response({"msg": "", "code": 0, "data": None}))
            await writer.drain()
            return
        # 本地 / 未知 guid：原样转发，让官方写 item_user_play / 删除记录
        raw = await forward(head, headers, body)
        writer.write(raw)
        await writer.drain()
        log("play/record 本地转发:", method, (gid or "-")[:18])
        return
    if path == "/v/api/v1/play/quit":
        pd = _play_body(body)
        gid = _play_gid(pd, body_guid, g)
        main_gid = _play_main_guid(gid)
        if main_gid:
            if not allow_online:
                writer.write(json_response(
                    {"msg": "在线源未对该用户开放", "code": 403, "data": None},
                    status=403))
                await writer.drain()
                return
            # 在线：退出播放不再续记，但保留已记录的进度供「继续播放」使用
            writer.write(json_response({"msg": "", "code": 0, "data": None}))
            await writer.drain()
            return
        # 本地：必须交给官方，否则播放会话/进度收尾不完整
        raw = await forward(head, headers, body)
        writer.write(raw)
        await writer.drain()
        log("play/quit 本地转发:", method, (gid or "-")[:18])
        return

    gg = g or (body_guid if body_guid in registry else None)
    if gg and isinstance(registry.get(gg), dict):
        if not allow_online:
            writer.write(json_response(
                {"msg": "在线源未对该用户开放", "code": 403, "data": None},
                status=403))
            await writer.drain()
            return
        rec = registry[gg]
        if path.startswith("/v/api/v1/item/") and not path.endswith("/list"):
            if method == "DELETE":
                writer.write(json_response({"msg": "", "code": 0, "data": None}))
            else:
                await ensure_resolved(gg)
                writer.write(json_response(
                    {"msg": "", "code": 0, "data": build_item(gg, registry[gg])}))
            await writer.drain()
            return
        if path.startswith(("/v/api/v1/tv/", "/v/api/v1/movie/")):
            await ensure_resolved(gg)
            writer.write(json_response(
                {"msg": "", "code": 0, "data": build_item(gg, registry[gg])}))
            await writer.drain()
            return
        if path.startswith("/v/api/v1/season/list/"):
            await ensure_resolved(gg)
            writer.write(json_response(
                {"msg": "", "code": 0, "data": synth_seasons(gg, registry[gg])}))
            await writer.drain()
            return
        if path.startswith("/v/api/v1/episode/list/"):
            await ensure_resolved(gg)
            writer.write(json_response(
                {"msg": "", "code": 0, "data": synth_episodes(gg, registry[gg])}))
            await writer.drain()
            return
        if path == "/v/api/v1/play/info":
            await ensure_resolved(gg)
            kick_props_probe(gg)
            _log_play_req(path, body)
            writer.write(json_response(synth_play_info(gg, registry[gg])))
            await writer.drain()
            return
        if path == "/v/api/v1/stream":
            kick_props_probe(gg)
            _log_play_req(path, body)
            writer.write(json_response(synth_stream(gg, headers=headers)))
            await writer.drain()
            return
        if path in ("/v/api/v1/play/play", "/v/api/v1/play/quality"):
            kick_props_probe(gg)
            _log_play_req(path, body)
            writer.write(json_response(synth_play(gg, headers=headers)))
            await writer.drain()
            return
        if path.startswith("/v/api/v1/"):
            # 未识别的在线接口（record/quit/setConfig/checkPlayLink...）：
            # 官方后端必然查无此 guid，统一回成功空响应并告警
            log("UNHANDLED-ON", method, path)
            dummy = [] if ("/list/" in path or path.endswith("/list")) else None
            writer.write(json_response({"msg": "", "code": 0, "data": dummy}))
            await writer.drain()
            return

    # ---- 5) 其余原样转发（透明流式，不整体缓冲） ----
    _tee = [] if (_DIAG == "2" and path.startswith("/v/api/")) else None
    await forward_stream(head, body, writer, tee=_tee)
    # 立刻把「响应到此结束」传给 nginx。官方是 HTTP/1.0 服务端，用它自己的
    # 连接关闭来界定响应结尾：/v/api/v1/stream 的成功响应既没有 Content-Length
    # 也没有 Transfer-Encoding。若不在这一步关闭，nginx 会一直等 EOF 直到前端
    # 超时（日志里表现为 "POST /v/api/v1/stream" 499 0，前端卡“视频加载中”）。
    # 注意要放在下面 CAP/DIAG 之前，否则诊断重放会把关闭拖后十几秒。
    try:
        writer.close()
    except Exception:
        pass
    if _tee:
        raw = b"".join(_tee)
        # 本地点播链路：把同一请求原样重放给官方，做响应字节比对
        if (_CAPN[0] < 8 and len(raw) < 6000
                and not (g and g in registry) and (
                    path.startswith("/v/api/v1/play/")
                    or path.startswith("/v/api/v1/stream"))):
            await _cap_and_replay(head, body, raw)
        # 本地视频点播链路（play/info / stream / media/range）即使 code=0
        # 也要打出响应体：本地播放卡在 /stream 之后不再往下走。
        _watch = (path.startswith("/v/api/v1/play/")
                  or path.startswith("/v/api/v1/stream")
                  or path.startswith("/v/api/v1/media/")
                  or ("/v/api/v1/item/" in path and method == "GET"))
        _b = b""
        try:
            _h, _b = split_response(raw)
            _j = json.loads(_b.decode("utf-8", "replace"))
            if _j.get("code") != 0:
                log("DIAG-FWD", method, path, "code=" + str(_j.get("code")),
                    "msg=" + str(_j.get("msg"))[:40])
            elif _watch:
                log("DIAG-OK", method, path, "data=",
                    repr(_j.get("data"))[:600])
        except Exception as _e:
            if _watch:
                log("DIAG-RAW", method, path[:60], "body=", _b[:220])
            else:
                log("DIAG-FWD", method, path, "parse-fail", repr(_e)[:60])


async def handle_client(reader, writer):
    # 官方后端是 HTTP/1.0 服务端：用「关闭连接」界定响应结束，很多响应
    # （例如 /v/api/v1/stream 的成功响应）没有 Content-Length。nginx 也是用
    # HTTP/1.0 连上游的，所以这里必须「一请求一连接、响应后立即关闭」，
    # 否则 nginx 永远等不到响应结束，前端就会一直卡在“视频加载中”。
    try:
        try:
            head, headers, body = await asyncio.wait_for(
                read_request(reader), IDLE_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError,
                asyncio.IncompleteReadError):
            return            # 空闲超时/对端断开：直接收工
        except Exception as e:
            log("read req err:", repr(e)[:160])
            return
        try:
            await handle_request(reader, writer, head, headers, body)
        except Exception as e:
            log("handle err:", repr(e)[:200])
            try:
                await write_head(writer, 502,
                                 {"Content-Length": 0, "Connection": "close"})
            except Exception:
                pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    global session
    load_registry()
    _load_props()
    # 连接池：播放器会并发拉多个分片，复用连接避免每片重建 TLS
    conn = aiohttp.TCPConnector(limit=64, limit_per_host=32,
                                ttl_dns_cache=600, force_close=False,
                                enable_cleanup_closed=True)
    session = aiohttp.ClientSession(
        connector=conn,
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=ONLINE_TIMEOUT))
    if not os.path.exists(REAL):
        log("提示: 官方 unix socket 不在（forward 走 TCP 8005）", REAL)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    server = await asyncio.start_unix_server(handle_client, path=SOCK)
    os.chmod(SOCK, 0o777)
    log("MITM 已接管", SOCK, f"→ 转发 {UPSTREAM_TCP[0]}:{UPSTREAM_TCP[1]}"
        f"/unix:{REAL}",
        f"(注册表 {len(registry)} 条在线条目, 豆瓣伪库 {FAKE_LIB_GUID[:12]}...)")
    if _SOURCES_MSG:
        log("源清单:", _SOURCES_MSG,
            "→ 打开 http://<NAS 的 IP>:5666/v/__mitm__/config 填写")
    log("源清单:", SOURCES_ORIGIN,
        "主用=" + (",".join(s["name"] for s in CMS_PRIMARY) or "(空)"),
        "兜底=" + (",".join(s["name"] for s in CMS_FALLBACK) or "(空)"))
    # 预热：豆瓣榜单 + 片源解析（后台，不阻塞启动）
    async def _warm():
        await asyncio.sleep(1)
        try:
            allg = await douban_guids()
            log("warmup: 豆瓣各库",
                sum(len(v) for v in allg.values()), "条，后台解析片源…")
            # 榜单先抓一遍（让用户马上能看到库），再等一会儿做完整维护；
            # 开机瞬间官方后端/app-center 还在启动，错开避免互相抢资源
            await asyncio.sleep(90)
            await maintenance("boot")
        except Exception as e:
            log("warmup err:", repr(e)[:160])
    asyncio.create_task(_warm())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
