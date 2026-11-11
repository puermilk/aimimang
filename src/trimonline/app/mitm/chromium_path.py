#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析可用的 Chromium 路径。

优先级：
  1. AIMIMANG_CHROMIUM（必须是真实可执行文件）
  2. NAS 系统自带 chromium / chrome
  3. Playwright 下载到 vip_data/ms-playwright 的 chromium

不在这里下载浏览器；下载由配置页的「安装 Chromium」按钮显式触发。
"""
import glob
import os
import sys


def _exe(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def playwright_root():
    return (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            or "/usr/local/trim_media_mitm/vip_data/ms-playwright")


def find_chromium(root=None):
    env = os.environ.get("AIMIMANG_CHROMIUM") or ""
    if _exe(env):
        return env
    for path in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                 "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chrome"):
        if _exe(path):
            return path
    base = root or playwright_root()
    patterns = [
        os.path.join(base, "chromium-*", "chrome-linux", "chrome"),
        os.path.join(base, "chromium-*", "chrome-linux", "headless_shell"),
        os.path.join(base, "chromium_headless_shell-*",
                     "chrome-headless-shell-linux64", "chrome-headless-shell"),
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern), reverse=True):
            if _exe(path):
                return path
    return ""


if __name__ == "__main__":
    found = find_chromium()
    print(found)
    sys.exit(0 if found else 1)
