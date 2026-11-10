#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芒果TV Playwright worker: 搜索抓取 + 播放 m3u8 抓取。

用法:
  python3 mgtv_worker.py search <keyword> <out_json> [storage_state]
  python3 mgtv_worker.py play   <page_url> <out_json> [storage_state]

search: 打开 so.mgtv.com/so?k=<kw>, 拦截 pc/search/v2 JSON, 原样写文件。
play:   打开视频页 <page_url>, 点击播放, 捕获真实 m3u8 URL 写文件。
"""
import asyncio, json, os, sys, time

from playwright.async_api import async_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DEFAULT_STATE = "/root/mgtv_login/storage_state.json"
CHROMIUM = os.environ.get("AIMIMANG_CHROMIUM") or (
    "/usr/bin/chromium" if os.path.exists("/usr/bin/chromium") else "")


async def _new_page(p, storage_state):
    kw = {} if not CHROMIUM else {"executable_path": CHROMIUM}
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
              "--autoplay-policy=no-user-gesture-required"], **kw)
    ctx = await browser.new_context(
        storage_state=storage_state if os.path.exists(storage_state) else None,
        user_agent=UA, viewport={"width": 1366, "height": 900}, locale="zh-CN")
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = await ctx.new_page()
    return browser, ctx, page


async def do_search(kw, out, storage_state):
    result = {"ok": 0, "keyword": kw, "json": None, "ts": time.time()}
    async with async_playwright() as p:
        browser, ctx, page = await _new_page(p, storage_state)
        store = {}

        async def on_resp(r):
            if "pc/search/v2" in r.url and "json" not in store:
                try:
                    store["json"] = await r.text()
                except Exception:
                    pass

        page.on("response", on_resp)
        try:
            import urllib.parse
            await page.goto("https://so.mgtv.com/so?k=" + urllib.parse.quote(kw),
                            timeout=45000, wait_until="domcontentloaded")
        except Exception:
            pass
        for _ in range(30):
            if "json" in store:
                break
            await page.wait_for_timeout(500)
        if "json" in store:
            result["ok"] = 1
            result["json"] = store["json"]
        try:
            await browser.close()
        except Exception:
            pass
    _write(out, result)


async def do_play(page_url, out, storage_state):
    result = {"ok": 0, "page_url": page_url, "m3u8": "", "ts": time.time()}
    async with async_playwright() as p:
        browser, ctx, page = await _new_page(p, storage_state)
        found = {"m3u8": ""}

        async def on_resp(r):
            u = r.url
            if not found["m3u8"] and ".m3u8" in u and "titan.mgtv.com" in u:
                found["m3u8"] = u

        page.on("response", on_resp)
        try:
            await page.goto(page_url, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)
            # 关闭隐私协议弹窗
            try:
                await page.get_by_text("我同意", exact=True).first.click(timeout=2500)
            except Exception:
                pass
            await page.wait_for_timeout(800)
            # 点击播放区域 + 主动触发 video.play()
            try:
                await page.mouse.click(680, 380)
            except Exception:
                pass
            try:
                await page.evaluate(
                    """()=>{const v=document.querySelector('video');if(v){v.muted=true;
                       try{v.play().catch(()=>{})}catch(e){}}}""")
            except Exception:
                pass
            for _ in range(36):  # ~18s
                if found["m3u8"]:
                    break
                # 每 3s 再触发一次播放
                if _ % 6 == 5:
                    try:
                        await page.evaluate(
                            """()=>{const v=document.querySelector('video');if(v){v.muted=true;v.play().catch(()=>{})}}""")
                    except Exception:
                        pass
                await page.wait_for_timeout(500)
        except Exception:
            pass
        if found["m3u8"]:
            result["ok"] = 1
            result["m3u8"] = found["m3u8"]
        try:
            await browser.close()
        except Exception:
            pass
    _write(out, result)


def _write(out, result):
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: mgtv_worker.py <search|play> <arg> <out> [storage_state]")
        sys.exit(1)
    mode, arg, out = sys.argv[1], sys.argv[2], sys.argv[3]
    storage = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_STATE
    if mode == "search":
        asyncio.run(do_search(arg, out, storage))
    elif mode == "play":
        asyncio.run(do_play(arg, out, storage))
    else:
        print("unknown mode", mode)
        sys.exit(1)
