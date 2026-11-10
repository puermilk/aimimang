#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱迷茫 2.0 · VIP 源登录会话（扫码 / 手机验证码）。

只在本机跑无头 Chromium，把登录对话框里的二维码截图给配置页；
手机验证码由平台页面自己发送，插件不接触账号密码。
登录成功后把浏览器 storage_state 写到 conf/<平台>_state.json（权限 600）。
"""
import base64
import json
import os
import queue
import threading
import time
import traceback

try:
    from playwright.sync_api import sync_playwright
    _PW_ERR = ""
except Exception as e:                       # 没装 Playwright 时给配置页一个明确原因
    sync_playwright = None
    _PW_ERR = repr(e)[:160]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CHROMIUM = os.environ.get("AIMIMANG_CHROMIUM") or (
    "/usr/bin/chromium" if os.path.exists("/usr/bin/chromium") else "")

PLATFORMS = {
    "iqiyi": {
        "name": "爱奇艺",
        "home": "https://www.iqiyi.com/",
        "open": ["div[class*='topbar_userBtn']", "#btn_user"],
        "qr": ["div[class*='qkLogin'] img", "div[class*='qkLogin'] canvas",
               "[class*='qrcode']", "[class*='qrCode']", "[class*='qr_']",
               "div[class*='_login_'] img", "canvas", "img[src*='qr']"],
        "sms_tab": ["text=短信登录"],
        "phone": ["input[type=tel]", "input[placeholder*='手机号']"],
        "code": ["input[placeholder*='验证码']"],
        "send": ["text=获取验证码", "text=获取短信验证码", "text=发送验证码"],
        "submit": ["div[class*='_loginBtn_']", "div[class*='loginBtn']"],
        "agree": ["div[class*='_check_off_']",
                   "div[class*='_agree_check_box_']",
                   "div[class*='_checkbox_']", "input[type=checkbox]"],
        "auth": ["P00001"],
    },
    "migu": {
        "name": "咪咕",
        "home": "https://www.miguvideo.com/",
        "open": [".header-user", ".icon-item.user"],
        "qr_toggle": ["a#qrccode", "a.qrcode"],
        "qr": ["img[src*='qr']", "[class*='qrcode']", "[class*='qrCode']",
               "canvas", "div[class*='qr'] img"],
        "sms_tab": ["text=短信登录"],
        "phone": ["input[placeholder*='手机号']", "input[type=tel]"],
        "code": ["input[placeholder*='验证码']"],
        "send": ["input[type=button]", "text=获取验证码", "text=获取短信验证码"],
        "submit": ["input.login-submit", "text=登录"],
        "agree": ["div.protocol i", "div.protocol span", "div.protocol",
                   "input[type=checkbox]"],
        "auth": ["MIGU_UID"],
    },
    "mgtv": {
        "name": "芒果TV",
        "home": "https://www.mgtv.com/",
        "open": [".top-header-v2-actions__avatar"],
        "privacy": ["button.allow", "button:has-text('我同意')"],
        "qr": ["div.scan-code .code-img canvas", "div.code-img canvas",
               "div.code-img img"],
        "sms_tab": ["text=短信登录"],
        "phone": ["input[placeholder*='手机号']"],
        "code": ["input[placeholder*='校验码']", "input[placeholder*='验证码']"],
        "send": ["div.send-code", "text=获取校验码"],
        "submit": ["div.login-btn", "text=登录"],
        "agree": [".protocol-agree", "input[type=checkbox]"],
        "auth": ["UID"],
    },
}


def _visible(loc):
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _frames(page):
    return [page] + list(page.frames)


def _click_any(page, selectors, force=False, timeout=3000):
    """在所有 frame 里按顺序找第一个可见元素并点击。"""
    for frame in _frames(page):
        for sel in selectors or []:
            try:
                loc = frame.locator(sel)
                n = min(loc.count(), 4)
                for i in range(n):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(force=force, timeout=timeout)
                        return True
            except Exception:
                continue
    return False


def _fill_any(page, selectors, value):
    for frame in _frames(page):
        for sel in selectors or []:
            try:
                loc = frame.locator(sel)
                n = min(loc.count(), 4)
                for i in range(n):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.fill(str(value), timeout=5000)
                        return True
            except Exception:
                continue
    return False


def _choose_sms(page, spec):
    _click_any(page, spec.get("sms_tab"), timeout=3000)


def _choose_qr(page, spec):
    # 咪咕默认是短信 tab，二维码在右上角的小图标里
    _click_any(page, spec.get("qr_toggle"), timeout=2500)
    time.sleep(0.6)


def _wait_ready(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(2.0)


def _capture_qr(page, spec):
    """截一张二维码图（data URL）。找不到二维码返回空串。"""
    # 1) 精确选择器
    for frame in _frames(page):
        for sel in spec.get("qr") or []:
            try:
                loc = frame.locator(sel)
                for i in range(min(loc.count(), 6)):
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box:
                        continue
                    w, h = box.get("width") or 0, box.get("height") or 0
                    if not (80 <= w <= 520 and 80 <= h <= 520):
                        continue
                    try:
                        png = el.screenshot(type="jpeg", quality=82)
                        mime = "image/jpeg"
                    except Exception:
                        png = el.screenshot()
                        mime = "image/png"
                    return "data:%s;base64,%s" % (
                        mime, base64.b64encode(png).decode())
            except Exception:
                continue
    # 2) 兜底：截登录窗口中间一块（别回传整页 1MB 截图）
    try:
        vp = page.viewport_size or {"width": 1440, "height": 900}
        clip = {"x": max(0, vp["width"] / 2 - 320),
                "y": 120, "width": 640, "height": 620}
        png = page.screenshot(full_page=False, clip=clip, type="jpeg",
                              quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(png).decode()
    except Exception:
        return ""


def _cookie_names(context):
    try:
        return {c.get("name") for c in context.cookies()}
    except Exception:
        return set()


def _logged(context, spec):
    names = _cookie_names(context)
    return any(a in names for a in spec.get("auth") or [])


def _mask_phone(phone):
    phone = str(phone or "")
    return phone[:3] + "****" + phone[-2:] if len(phone) >= 6 else phone


class LoginSession:
    def __init__(self, platform, mode, phone, state_path, save_state,
                 chromium=""):
        self.platform = platform
        self.spec = PLATFORMS[platform]
        self.mode = "sms" if mode == "sms" else "qr"
        self.phone = str(phone or "").strip()
        self.state_path = state_path
        self.save_state = save_state
        self.chromium = chromium or CHROMIUM
        self.lock = threading.Lock()
        self.cmds = queue.Queue()
        self.stop_event = threading.Event()
        self.alive = False
        self.thread = None
        self.status = {
            "platform": platform,
            "name": self.spec["name"],
            "mode": self.mode,
            "state": "starting",
            "qr": "",
            "message": "正在启动登录窗口…",
            "logged": False,
            "phone": _mask_phone(self.phone),
            "ts": time.time(),
        }

    def snapshot(self):
        with self.lock:
            out = dict(self.status)
            out["alive"] = self.alive
            return out

    def start(self):
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def push(self, code):
        self.cmds.put(("verify", str(code or "").strip()))

    def stop(self):
        self.stop_event.set()
        self.cmds.put(("stop", ""))

    def _set(self, **kw):
        with self.lock:
            self.status.update(kw)
            self.status["ts"] = time.time()

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:
            self._set(state="error", message="登录窗口出错：%s" % repr(e)[:160])
        finally:
            self.alive = False
            self.stop_event.set()

    def _run_inner(self):
        if sync_playwright is None:
            self._set(state="error",
                      message="当前环境缺少 Playwright：" + _PW_ERR)
            return
        state_path = self.state_path
        with sync_playwright() as p:
            kw = {"headless": True,
                  "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                           "--autoplay-policy=no-user-gesture-required"]}
            if self.chromium:
                kw["executable_path"] = self.chromium
            browser = p.chromium.launch(**kw)
            try:
                ctx = browser.new_context(
                    user_agent=UA, viewport={"width": 1440, "height": 900},
                    locale="zh-CN")
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = ctx.new_page()
                self._set(message="正在打开%s…" % self.spec["name"])
                page.goto(self.spec["home"], wait_until="domcontentloaded",
                          timeout=60000)
                _wait_ready(page)
                # 芒果首次会弹隐私协议
                _click_any(page, self.spec.get("privacy"), force=True, timeout=2500)
                if not _click_any(page, self.spec.get("open"), force=True, timeout=5000):
                    self._set(state="error",
                              message="没找到登录入口，请稍后重试或换用扫码登录")
                    return
                time.sleep(2.0)

                if self.mode == "sms":
                    self._sms_flow(page, ctx, state_path)
                else:
                    self._qr_flow(page, ctx, state_path)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def _qr_flow(self, page, ctx, state_path):
        _choose_qr(page, self.spec)
        time.sleep(1.2)
        qr = _capture_qr(page, self.spec)
        self._set(state="qr", qr=qr, message="请用对应平台 App 扫码登录")
        deadline = time.time() + 480
        last_refresh = time.time()
        while time.time() < deadline and not self.stop_event.is_set():
            if _logged(ctx, self.spec):
                return self._finish(ctx, state_path)
            if time.time() - last_refresh > 15:
                new_qr = _capture_qr(page, self.spec)
                if new_qr:
                    self._set(qr=new_qr)
                last_refresh = time.time()
            time.sleep(1.5)
        if not self.stop_event.is_set():
            self._set(state="expired", message="二维码已过期，请重新发起登录")

    def _sms_flow(self, page, ctx, state_path):
        _choose_sms(page, self.spec)
        time.sleep(0.8)
        if not self.phone:
            self._set(state="need_phone", message="请输入手机号")
            return
        if not _fill_any(page, self.spec.get("phone"), self.phone):
            self._set(state="error", message="没找到手机号输入框")
            return
        # 协议勾选（已勾过时点击通常无害）
        _click_any(page, self.spec.get("agree"), force=True, timeout=1500)
        time.sleep(0.3)
        if not _click_any(page, self.spec.get("send"), force=True, timeout=4000):
            self._set(state="error", message="没找到「获取验证码」按钮")
            return
        time.sleep(2.0)
        self._set(state="need_code",
                  message="验证码已发送到 %s，请输入短信验证码" % _mask_phone(self.phone))
        deadline = time.time() + 480
        while time.time() < deadline and not self.stop_event.is_set():
            if _logged(ctx, self.spec):
                return self._finish(ctx, state_path)
            try:
                cmd, val = self.cmds.get(timeout=1.2)
            except queue.Empty:
                continue
            if cmd == "stop":
                break
            if cmd != "verify" or not val:
                continue
            if not _fill_any(page, self.spec.get("code"), val):
                self._set(message="没找到验证码输入框")
                continue
            _click_any(page, self.spec.get("agree"), force=True, timeout=1200)
            if not _click_any(page, self.spec.get("submit"), force=True,
                              timeout=4000):
                self._set(message="没找到登录按钮")
                continue
            self._set(state="verifying", message="正在验证…")
            for _ in range(8):
                time.sleep(1.0)
                if _logged(ctx, self.spec):
                    return self._finish(ctx, state_path)
            self._set(state="need_code", message="验证码可能不对，请重新输入")
        if not self.stop_event.is_set() and self.status.get("state") not in (
                "error", "success"):
            self._set(state="expired", message="登录超时，请重新发起")

    def _finish(self, ctx, state_path):
        try:
            st = ctx.storage_state()
            ok, msg = self.save_state(self.platform, st, state_path)
            if not ok:
                self._set(state="error", message=msg)
                return
            self._set(state="success", logged=True, qr="",
                      message="登录成功，已保存%s登录态" % self.spec["name"])
        except Exception as e:
            self._set(state="error", message="保存登录态失败：%s" % repr(e)[:140])


class LoginManager:
    def __init__(self, save_state):
        # save_state(platform, storage_state, path) -> (ok, msg)
        self.save_state = save_state
        self.sessions = {}
        self.lock = threading.Lock()

    def start(self, platform, mode="qr", phone="", state_path=""):
        platform = str(platform or "").lower()
        if platform not in PLATFORMS:
            return {"ok": False, "msg": "不认识的平台"}
        mode = "sms" if mode == "sms" else "qr"
        with self.lock:
            old = self.sessions.get(platform)
            if old and old.alive:
                return {"ok": True, "msg": "登录会话已在运行",
                        "session": old.snapshot()}
            s = LoginSession(platform, mode, phone, state_path,
                             self.save_state)
            self.sessions[platform] = s
            s.start()
            return {"ok": True, "msg": "登录窗口已启动",
                    "session": s.snapshot()}

    def status(self, platform):
        platform = str(platform or "").lower()
        with self.lock:
            s = self.sessions.get(platform)
        if not s:
            return {"platform": platform, "state": "idle", "alive": False,
                    "message": "没有进行中的登录"}
        return s.snapshot()

    def verify(self, platform, code):
        platform = str(platform or "").lower()
        with self.lock:
            s = self.sessions.get(platform)
        if not s or not s.alive:
            return False, "没有进行中的登录"
        s.push(code)
        return True, "验证码已提交"

    def cancel(self, platform):
        platform = str(platform or "").lower()
        with self.lock:
            s = self.sessions.get(platform)
        if s:
            s.stop()
        return True, "已取消"
