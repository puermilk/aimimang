#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱迷茫 2.0 · VIP 登录态保活。

定期用无头 Chromium 带上已保存的 storage_state 访问平台，
触发服务端续期并把最新 cookie 写回 conf/<平台>_state.json。

由 trim-media-vip-keepalive.timer 每 6 小时调用；配置页也能「立即保活」。
"""
import json
import os
import sys
import time

try:
    from playwright.sync_api import sync_playwright
    _PW_ERR = ""
except Exception as e:
    sync_playwright = None
    _PW_ERR = repr(e)[:160]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CONF_DIR = os.environ.get("AIMIMANG_CONF", "/usr/local/trim_media_mitm/conf")
DATA_DIR = os.environ.get("AIMIMANG_DATA", "/usr/local/trim_media_mitm/vip_data")
STATUS = os.path.join(DATA_DIR, "keepalive.json")
from chromium_path import find_chromium

CHROMIUM = find_chromium()

PLATFORMS = {
    "iqiyi": {
        "name": "AI",
        "state": "vip_iqiyi_state",
        "file": "iqiyi_state.json",
        "auth": ["P00001"],
        "urls": ["https://www.iqiyi.com/", "https://passport.iqiyi.com/"],
    },
    "migu": {
        "name": "MI",
        "state": "vip_migu_state",
        "file": "migu_state.json",
        "auth": ["MIGU_UID", "mgnd_session_id", "userInfo",
                 "UserInfo", "checked_token"],
        "urls": ["https://www.miguvideo.com/", "https://passport.migu.cn/"],
    },
    "mgtv": {
        "name": "MANG",
        "state": "vip_mgtv_state",
        "file": "mgtv_state.json",
        "auth": ["UID", "loginAccount", "id"],
        "urls": ["https://www.mgtv.com/", "https://i.mgtv.com/account"],
    },
}


def _cfg():
    try:
        return json.load(open(os.path.join(CONF_DIR, "config.json"),
                              encoding="utf-8"))
    except Exception:
        return {}


def _state_path(platform, cfg):
    spec = PLATFORMS[platform]
    val = str(cfg.get(spec["state"]) or "")
    builtin = "/usr/local/trim_media_mitm/conf/" + spec["file"]
    default = os.path.join(CONF_DIR, spec["file"])
    return default if (not val or val == builtin) else val


def _write_status(platform, result):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        allst = json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        allst = {}
    allst[platform] = result
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(allst, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATUS)


def _run_one(platform, cfg):
    spec = PLATFORMS[platform]
    path = _state_path(platform, cfg)
    result = {"ts": time.time(), "ok": 0, "name": spec["name"],
              "error": "", "cookies": 0}
    if not os.path.exists(path):
        result["error"] = "未登录（没有登录态文件）"
        return result
    if sync_playwright is None:
        result["error"] = "缺少 Playwright：" + _PW_ERR
        return result
    try:
        with sync_playwright() as p:
            kw = {"headless": True,
                  "args": ["--no-sandbox",
                           "--disable-blink-features=AutomationControlled"]}
            if CHROMIUM:
                kw["executable_path"] = CHROMIUM
            browser = p.chromium.launch(**kw)
            try:
                ctx = browser.new_context(
                    storage_state=path, user_agent=UA,
                    viewport={"width": 1280, "height": 800}, locale="zh-CN")
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = ctx.new_page()
                for url in spec["urls"]:
                    try:
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=45000)
                        time.sleep(2.5)
                    except Exception:
                        pass
                state = ctx.storage_state()
                names = {c.get("name") for c in state.get("cookies", [])}
                ok = any(a in names for a in spec["auth"])
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                result["ok"] = 1 if ok else 0
                result["cookies"] = len(state.get("cookies", []))
                if not ok:
                    result["error"] = "cookie 已刷新，但未识别到登录态（可能需要重新登录）"
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = repr(e)[:200]
    return result


def main():
    args = sys.argv[1:]
    only = ""
    if "--platform" in args:
        i = args.index("--platform")
        if i + 1 < len(args):
            only = args[i + 1]
    cfg = _cfg()
    targets = [only] if only in PLATFORMS else list(PLATFORMS)
    out = {}
    for platform in targets:
        res = _run_one(platform, cfg)
        _write_status(platform, res)
        out[platform] = res
        print(json.dumps({"platform": platform, **res}, ensure_ascii=False),
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
