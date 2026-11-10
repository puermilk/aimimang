#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2.0.0 VIP 源 / 搜索不截断 / 配置页文案 的行为测试。

跑法：
    PYTHONPATH=src/trimonline/app/mitm/lib \
      python3 tools/test_vip.py

为什么用行为测试：暗号、开关、搜索条数都是用户能直接看到的结果，
不去断言内部缓存或函数名，避免实现一换测试就假绿。
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MITM = os.path.join(os.path.dirname(HERE), "src", "trimonline", "app", "mitm")
sys.path.insert(0, MITM)

import mitm                                      # noqa: E402
import webui                                     # noqa: E402


def _fake_site(name="测试源"):
    return {"name": name, "api": "http://example.test/api.php/provide/vod/",
            "referer": "http://example.test/", "lines": "multi", "kind": "cms",
            "timeout": 0}


class VipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mitm.CONF_DIR = self.tmp.name
        mitm.CONFIG_FILE = os.path.join(self.tmp.name, "config.json")
        mitm._POLICY_CACHE["ts"] = 0.0
        mitm._SOURCE_CFG_CACHE["ts"] = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_wrong_code_does_not_unlock(self):
        ok, _msg, cfg = mitm.vip_apply({"code": "wrong", "iqiyi": True})
        self.assertFalse(ok)
        self.assertFalse(cfg.get("vip_unlocked"))

    def test_right_code_unlocks_and_enables_both(self):
        ok, _msg, cfg = mitm.vip_apply({"code": "aimimang"})
        self.assertTrue(ok)
        self.assertTrue(cfg.get("vip_unlocked"))
        self.assertTrue(cfg.get("vip_iqiyi"))
        self.assertTrue(cfg.get("vip_migu"))
        self.assertFalse(cfg.get("vip_migu_live"))
        self.assertTrue(cfg.get("vip_mgtv"))

    def test_toggle_requires_unlock(self):
        ok, _msg, cfg = mitm.vip_apply({"migu_live": True})
        self.assertFalse(ok)
        self.assertFalse(cfg.get("vip_migu_live"))

    def test_migu_live_only_when_migu_enabled(self):
        mitm.vip_apply({"code": "aimimang", "migu": False, "migu_live": True})
        names = [s["name"] for s in mitm.vip_sites()]
        self.assertNotIn("咪咕直播", names)
        mitm.vip_apply({"migu": True, "migu_live": True})
        self.assertIn("咪咕直播", [s["name"] for s in mitm.vip_sites()])

    def test_vip_sites_follow_switches(self):
        mitm.vip_apply({"code": "aimimang", "iqiyi": True, "migu": False,
                        "mgtv": False})
        names = [s["name"] for s in mitm.vip_sites()]
        self.assertEqual(names, ["爱奇艺VIP"])
        mitm.vip_apply({"iqiyi": False, "migu": True, "migu_live": False,
                        "mgtv": False})
        self.assertEqual([s["name"] for s in mitm.vip_sites()], ["咪咕VIP"])

    def test_public_config_never_leaks_code_hash(self):
        mitm.vip_apply({"code": "aimimang"})
        pub = mitm.public_config()
        self.assertNotIn("vip_code_sha256", pub)
        self.assertTrue(pub.get("vip_unlocked"))

    def test_import_login_state_writes_private_file(self):
        st = {"cookies": [{"name": "P00001", "value": "x", "domain": ".iqiyi.com"}]}
        ok, msg = mitm.vip_import_state("iqiyi", st)
        self.assertTrue(ok, msg)
        path = mitm.vip_state_path("iqiyi")
        self.assertEqual(os.path.dirname(path), self.tmp.name)
        self.assertEqual(json.load(open(path))["cookies"][0]["name"], "P00001")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertTrue(mitm.vip_states_payload()["iqiyi"]["configured"])

    def test_import_rejects_cookie_free_json(self):
        ok, _msg = mitm.vip_import_state("migu", {"hello": 1})
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(mitm.vip_state_path("migu")))

    def test_clear_login_state(self):
        mitm.vip_import_state("mgtv", {"cookies": [{"name": "a", "value": "b"}]})
        ok, _msg = mitm.vip_clear_state("mgtv")
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(mitm.vip_state_path("mgtv")))

    def test_merge_zero_limit_returns_everything(self):
        cms = [{"guid": "a%d" % i, "title": "片%d" % i,
                "ancestor_name": "站A"} for i in range(30)]
        js = [{"guid": "b%d" % i, "title": "剧%d" % i,
               "ancestor_name": "站B"} for i in range(30)]
        self.assertEqual(len(mitm._merge_online_items(cms, js, 0)), 60)


class JsSearchTest(unittest.TestCase):
    def test_js_query_does_not_cap_results(self):
        """一个 JS 源搜到 10 条，就该回 10 条；旧实现只取前 3 条。"""
        site = _fake_site("JS站")
        items = [{"vod_id": "v%d" % i, "vod_name": "剧%d" % i}
                 for i in range(10)]

        def fake_search(_site, _kw, _page):
            return items

        def fake_detail(_site, vid):
            i = int(vid[1:])
            return {"vod_id": vid, "vod_name": "剧%d" % i,
                    "vod_play_url": "正片$https://cdn.test/%s.m3u8" % vid}

        old_search, old_detail = mitm.jsspider.search, mitm.jsspider.detail
        mitm.jsspider.search = fake_search
        mitm.jsspider.detail = fake_detail
        try:
            got = asyncio.run(mitm.js_query(site, "剧"))
        finally:
            mitm.jsspider.search = old_search
            mitm.jsspider.detail = old_detail
        self.assertEqual(len(got), 10)


class PageTest(unittest.TestCase):
    def test_page_has_unlock_live_and_cast_copy(self):
        page = webui.PAGE
        for needle in ("暗号", "开启爱奇艺", "开启咪咕", "开启芒果",
                       "看直播电视", "手机投屏", "免责声明", "会员",
                       "扫码登录", "短信登录", "保活"):
            self.assertIn(needle, page, "配置页缺少：%s" % needle)

    def test_secret_code_not_hardcoded_in_page(self):
        self.assertNotIn("aimimang", webui.PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
