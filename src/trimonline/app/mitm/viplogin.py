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
import subprocess
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
DATA_DIR = os.environ.get("AIMIMANG_DATA", "/usr/local/trim_media_mitm/vip_data")
LOG_FILE = os.path.join(DATA_DIR, "login.log")


def log(*a):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a)))
    except Exception:
        pass

PLATFORMS = {
    "iqiyi": {
        "name": "AI",
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
        "name": "MI",
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
        "agree": ["q.checkbox", "input.J_mobileIsReadPrivacy",
                   "div.protocol i", "div.protocol span", "div.protocol",
                   "input[type=checkbox]"],
        "auth": ["MIGU_UID", "mgnd_session_id", "userInfo",
                 "UserInfo", "checked_token"],
    },
    "mgtv": {
        "name": "MANG",
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
        "auth": ["UID", "loginAccount", "id"],
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
    """截一张二维码图。返回 (data_url, 是否精确截到二维码元素)。"""
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
                        png = el.screenshot(type="png")
                    except Exception:
                        png = el.screenshot()
                    png = _pad_qr(png)
                    return ("data:image/jpeg;base64,%s" % (
                        base64.b64encode(png).decode()), True)
            except Exception:
                continue
    # 2) 兜底：截登录窗口中间一块（别回传整页 1MB 截图）
    try:
        vp = page.viewport_size or {"width": 1440, "height": 900}
        clip = {"x": max(0, vp["width"] / 2 - 320),
                "y": 120, "width": 640, "height": 620}
        png = page.screenshot(full_page=False, clip=clip, type="jpeg",
                              quality=80)
        return ("data:image/jpeg;base64," + base64.b64encode(png).decode(),
                False)
    except Exception:
        return "", False


def _pad_qr(png):
    """给二维码补白边（很多扫码器需要安静区），PIL 不可用时原样返回。"""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(png)).convert("RGB")
        pad = max(16, im.width // 10)
        out = Image.new("RGB", (im.width + pad * 2, im.height + pad * 2),
                        "white")
        out.paste(im, (pad, pad))
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return png


def _cookie_names(context):
    try:
        return {c.get("name") for c in context.cookies()}
    except Exception:
        return set()


def _logged(page, context, spec):
    names = _cookie_names(context)
    if any(a in names for a in spec.get("auth") or []):
        return True
    # 咪咕登录成功后登录 iframe 会关闭，父页面 .side-wrapper 的
    # islogin 会从 false 变成 true；这是比 cookie 名更可靠的信号。
    try:
        islogin = page.evaluate(
            "() => { const e = document.querySelector('.side-wrapper');"
            " return e ? (e.getAttribute('islogin') || '') : ''; }")
        if str(islogin).lower() == "true":
            return True
    except Exception:
        pass
    # 有些平台扫码后不写 cookie，而是把 iframe 跳到账号页；用页面文案兜底。
    for frame in _frames(page):
        try:
            ls = frame.evaluate(
                "() => { try { return Object.keys(localStorage); }"
                " catch (e) { return []; } }")
            if any(k in (ls or []) for k in ("userInfo", "USERINFO", "MIGU_UID")):
                return True
        except Exception:
            pass
        try:
            txt = (frame.locator("body").inner_text(timeout=500) or "")
            if "退出登录" in txt or "退出帐号" in txt:
                return True
        except Exception:
            continue
    return False


def _auto_confirm(page):
    """扫码后有些平台要在网页上再点一次确认；轮询时顺手点掉。"""
    for text in ("确认登录", "确认授权", "同意并登录", "立即登录"):
        if _click_any(page, ["text=" + text], force=True, timeout=700):
            return True
    return False


def _visible_error(page):
    """登录失败时把页面上的真实原因带出来，别再只显示“验证码可能不对”。"""
    words = ("验证码错误", "验证码不正确", "短信验证码错误", "验证码已失效",
             "验证码已过期", "验证码错误或已失效", "请拖动", "滑块",
             "操作频繁", "请求过于频繁", "账号异常", "手机号格式",
             "请输入正确的手机号", "图形验证码", "图片验证码")
    for frame in _frames(page):
        try:
            txt = frame.locator("body").inner_text(timeout=800) or ""
        except Exception:
            continue
        for w in words:
            if w in txt:
                return w
    return ""


def _agree_state(page, spec):
    """返回 True=已勾选，False=明确未勾选，None=看不出来。"""
    for frame in _frames(page):
        for sel in spec.get("agree") or []:
            try:
                loc = frame.locator(sel)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    cls = (el.get_attribute("class") or "").lower()
                    if "not-agree" in cls or "_check_off_" in cls or " off" in cls:
                        return False
                    if "checked" in cls or "_check_on_" in cls:
                        return True
                    if "protocol-agree" in cls:
                        return True
                    try:
                        if el.is_checked():
                            return True
                    except Exception:
                        pass
                    return None
            except Exception:
                continue
    return None


def _ensure_agree(page, spec):
    """只勾选、不反复点击；已经勾上时绝不再点（否则会取消）。"""
    state = _agree_state(page, spec)
    if state is True:
        return True
    return _click_any(page, spec.get("agree"), force=True, timeout=1500)


def _mask_phone(phone):
    phone = str(phone or "")
    return phone[:3] + "****" + phone[-2:] if len(phone) >= 6 else phone


def _trace_page(page, platform):
    """只记录咪咕登录链路的接口状态和平台 code/msg，不记录 cookie/token。"""
    import re as _re
    def on_resp(r):
        try:
            u = r.url
            if platform != "migu" or "passport.migu.cn" not in u:
                return
            if not any(k in u for k in ("login", "qrc", "dynamic", "password",
                                        "check", "auth")):
                return
            detail = ""
            try:
                body = r.text()[:1200]
                m = _re.search(r'"(?:code|resultCode|message|msg)"\s*:\s*"?([^",}]+)', body)
                if m:
                    detail = m.group(0)[:160]
                if "/qrcquery" in u:
                    try:
                        mm = _re.search(r'"message"\s*:\s*"([^"]+)"', body)
                        if mm:
                            page._qr_message = mm.group(1)
                        if any(k in body for k in ("已扫码", "确认登录", "待确认")):
                            page._qr_scanned = True
                    except Exception:
                        pass
            except Exception:
                pass
            log(platform, "net", r.status, u.split("?")[0][-90:], detail)
        except Exception:
            pass
    try:
        page.on("response", on_resp)
        page.on("framenavigated", lambda f: log(
            platform, "frame", f.url.split("?")[0][-110:])
            if platform == "migu" and "passport.migu.cn" in f.url else None)
    except Exception:
        pass


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
        old = self.status.get("state")
        with self.lock:
            self.status.update(kw)
            self.status["ts"] = time.time()
            new = self.status.get("state")
        if old != new:
            log(self.platform, "state", old, "->", new,
                "|", str(self.status.get("message") or "")[:160])

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:
            log(self.platform, "ERROR", repr(e), traceback.format_exc()[:1200])
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
        xvfb = None
        env = dict(os.environ)
        headless = True
        # 咪咕对无头浏览器风控更严：用 Xvfb 跑“有头”Chromium，
        # 扫码/短信的页面行为更接近真实浏览器。
        if self.platform == "migu" and os.path.exists("/usr/bin/Xvfb"):
            try:
                xvfb = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1440x900x24",
                     "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.0)
                env["DISPLAY"] = ":99"
                headless = False
                log(self.platform, "using Xvfb headless=False")
            except Exception as e:
                log(self.platform, "Xvfb failed", repr(e)[:100])
                xvfb = None
        try:
            with sync_playwright() as p:
                kw = {"headless": headless,
                      "args": ["--no-sandbox",
                               "--disable-blink-features=AutomationControlled",
                               "--autoplay-policy=no-user-gesture-required"],
                      "env": env}
                if self.chromium:
                    kw["executable_path"] = self.chromium
                browser = p.chromium.launch(**kw)
                try:
                    ctx = browser.new_context(
                        user_agent=UA,
                        viewport={"width": 1440, "height": 900},
                        locale="zh-CN")
                    ctx.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                    page = ctx.new_page()
                    _trace_page(page, self.platform)
                    self._set(message="正在打开 %s…" % self.spec["name"])
                    page.goto(self.spec["home"],
                              wait_until="domcontentloaded", timeout=60000)
                    _wait_ready(page)
                    _click_any(page, self.spec.get("privacy"),
                               force=True, timeout=2500)
                    if not _click_any(page, self.spec.get("open"),
                                      force=True, timeout=5000):
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
        finally:
            if xvfb is not None:
                try:
                    xvfb.terminate()
                except Exception:
                    pass

    def _qr_flow(self, page, ctx, state_path):
        _choose_qr(page, self.spec)
        # 咪咕/芒果的扫码页同样有协议勾选；不勾会让手机确认后仍不写登录态。
        _ensure_agree(page, self.spec)
        qr, exact = "", False
        # 二维码是异步渲染的：先等它真出来，别把加载中的页面截图当二维码。
        for _ in range(20):
            if self.stop_event.is_set():
                return
            qr, exact = _capture_qr(page, self.spec)
            if exact:
                break
            time.sleep(1.0)
        self._set(state="qr" if qr else "starting", qr=qr,
                  message=("请用对应平台 App 扫码登录" if exact
                           else "二维码加载较慢，可稍等刷新或改用短信登录"))
        log(self.platform, "qr ready exact=%s len=%d" % (exact, len(qr)))
        deadline = time.time() + 480
        last_refresh = time.time()
        while time.time() < deadline and not self.stop_event.is_set():
            if _logged(page, ctx, self.spec):
                return self._finish(ctx, state_path)
            _qmsg = getattr(page, "_qr_message", "")
            if _qmsg:
                self._set(message=_qmsg)
            _ensure_agree(page, self.spec)
            _auto_confirm(page)
            if (time.time() - last_refresh > 25
                    and not getattr(page, "_qr_scanned", False)):
                new_qr, new_exact = _capture_qr(page, self.spec)
                if new_qr and (new_exact or not exact):
                    self._set(qr=new_qr)
                    exact = new_exact or exact
                last_refresh = time.time()
            time.sleep(1.5)
        if not self.stop_event.is_set():
            log(self.platform, "qr expired")
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
        _ensure_agree(page, self.spec)
        time.sleep(0.3)
        if not _click_any(page, self.spec.get("send"), force=True, timeout=4000):
            self._set(state="error", message="没找到「获取验证码」按钮")
            return
        time.sleep(2.0)
        cap = ""
        for frame in _frames(page):
            try:
                if frame.locator("#J_ImgCodeOne").first.is_visible():
                    cap = "需要图形验证码"
                    break
            except Exception:
                pass
        if cap:
            self._set(state="need_code",
                      message="平台要求图形验证码，请改用扫码登录")
        else:
            self._set(state="need_code",
                      message="验证码已发送到 %s，请输入短信验证码" % _mask_phone(self.phone))
        deadline = time.time() + 480
        while time.time() < deadline and not self.stop_event.is_set():
            if _logged(page, ctx, self.spec):
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
            _ensure_agree(page, self.spec)
            if not _click_any(page, self.spec.get("submit"), force=True,
                              timeout=4000):
                self._set(message="没找到登录按钮")
                continue
            self._set(state="verifying", message="正在验证…")
            err = ""
            for _ in range(12):
                time.sleep(1.0)
                if _logged(page, ctx, self.spec):
                    return self._finish(ctx, state_path)
                err = _visible_error(page)
                if err:
                    break
            log(self.platform, "sms verify failed:", err or "no-visible-error")
            try:
                log(self.platform, "fail cookies",
                    ",".join(sorted(_cookie_names(ctx))))
                log(self.platform, "fail islogin",
                    page.evaluate(
                        "() => { const e = document.querySelector('.side-wrapper');"
                        " return e ? (e.getAttribute('islogin') || '') : ''; }"))
            except Exception:
                pass
            try:
                pg_path = os.path.join(DATA_DIR, "migu_fail_%d.png" % int(time.time()))
                page.screenshot(path=pg_path)
                log(self.platform, "fail screenshot", pg_path)
                for _fi, _fr in enumerate(_frames(page)):
                    try:
                        log(self.platform, "fail frame", _fi,
                            (_fr.locator("body").inner_text(timeout=1000) or "")[:300])
                    except Exception:
                        pass
            except Exception:
                pass
            if err:
                self._set(state="need_code",
                          message=(err + "，请重新输入")
                                  if "图形验证码" not in err and "图片验证码" not in err
                                  else (err + "，请改用扫码登录"))
            else:
                self._set(state="need_code", message="验证码可能不对，请重新输入")
        if not self.stop_event.is_set() and self.status.get("state") not in (
                "error", "success"):
            self._set(state="expired", message="登录超时，请重新发起")

    def _finish(self, ctx, state_path):
        try:
            st = ctx.storage_state()
            names = sorted({c.get("name") for c in st.get("cookies", [])})
            log(self.platform, "login detected cookies=", ",".join(names))
            ok, msg = self.save_state(self.platform, st, state_path)
            if not ok:
                self._set(state="error", message=msg)
                return
            self._set(state="success", logged=True, qr="",
                      message="登录成功，已保存 %s 登录态" % self.spec["name"])
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
