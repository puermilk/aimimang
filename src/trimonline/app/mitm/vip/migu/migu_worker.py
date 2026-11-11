#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""咪咕 m3u8 URL 抓取 worker (Playwright 无头浏览器)。

用法: python3 migu_worker.py <episode_cid> <out_json> [storage_state]

原理:
  咪咕 PC Web 端使用 /playurl/secret/v2/play/playurl 加密接口,
  普通 HTTP 请求无法直接构造, 但真实浏览器可以正常播放。
  本 worker 用无头 Chromium 打开单集详情页 /p/detail/<cid>,
  捕获播放器实际请求的 m3u8 URL (gslbmgspvod.miguvideo.com 域)。
  之后由 bridge 用 requests 代理 m3u8 和 ts (已验证可从 NAS 拉取)。
"""
import asyncio, json, os, re, sys, time

from playwright.async_api import async_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 咪咕下架页会带这个 meta。先判断，避免为一个已下架内容等 30 秒。
OFFLINE_MARKERS = ("内容已下线", "baidu-deadlink")


def page_is_offline(html):
    return any(m in (html or "") for m in OFFLINE_MARKERS)


from chromium_path import find_chromium

CHROMIUM = find_chromium()


async def run(cid, out, storage_state):
    result = {"ok": 0, "m3u8": "", "cid": str(cid), "ts": time.time()}
    async with async_playwright() as p:
        kw = {} if not CHROMIUM else {"executable_path": CHROMIUM}
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required"], **kw)
        ctx = await browser.new_context(
            storage_state=storage_state if os.path.exists(storage_state) else None,
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()

        found = {"m3u8": ""}

        async def on_resp(r):
            u = r.url
            if not found["m3u8"] and ".m3u8" in u and "mgsp" in u:
                found["m3u8"] = u

        page.on("response", on_resp)
        try:
            await page.goto(f"https://www.miguvideo.com/p/detail/{cid}",
                            timeout=20000, wait_until="domcontentloaded")
        except Exception:
            pass

        offline = False
        try:
            offline = page_is_offline(await page.content())
        except Exception:
            pass

        if offline:
            result = {"ok": 0, "m3u8": "", "cid": str(cid),
                      "ts": time.time(), "offline": 1}
        else:
            # 等待播放器自动请求 m3u8 (最多 ~10s)
            for _ in range(20):
                if found["m3u8"]:
                    break
                await page.wait_for_timeout(500)

            # 兜底: 点击播放区域 (最多 ~6s)
            if not found["m3u8"]:
                try:
                    await page.mouse.click(640, 400)
                except Exception:
                    pass
                for _ in range(12):
                    if found["m3u8"]:
                        break
                    await page.wait_for_timeout(500)

            if found["m3u8"]:
                result = {"ok": 1, "m3u8": found["m3u8"],
                          "cid": str(cid), "ts": time.time()}
        try:
            await browser.close()
        except Exception:
            pass

    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: migu_worker.py <cid> <out_json> [storage_state]")
        sys.exit(1)
    cid = sys.argv[1]
    out = sys.argv[2]
    storage = sys.argv[3] if len(sys.argv) > 3 else "/root/migu_login/storage_state.json"
    asyncio.run(run(cid, out, storage))
