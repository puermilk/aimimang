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

    def test_migu_live_is_separate_library_not_search_source(self):
        # 直播是独立媒体库，不再作为搜索源；搜索源里只应有三路 VIP。
        mitm.vip_apply({"code": "aimimang", "migu": True, "migu_live": True})
        self.assertTrue(mitm.migu_live_enabled())
        names = [s["name"] for s in mitm.vip_sites()]
        self.assertNotIn("咪咕直播", names)
        self.assertIn("咪咕VIP", names)

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


class SubtitleTest(unittest.TestCase):
    def _rec(self):
        return {"guid": "on" + "a" * 30, "site": "爱奇艺VIP",
                "title": "我不是大师", "eps": [
                    {"name": "第1集",
                     "url": "http://127.0.0.1:8770/p/6038403692778500.m3u8"},
                    {"name": "第2集",
                     "url": "http://127.0.0.1:8770/p/4412413519445700.m3u8"},
                ]}

    def test_sub_for_builds_and_resolves(self):
        rec = self._rec()
        info = mitm._sub_for(rec, rec["guid"], 1)
        self.assertTrue(info["guid"].startswith("onsub"))
        self.assertEqual(info["tvid"], "4412413519445700")
        got = mitm._resolve_sub(info["guid"])
        self.assertEqual(got["tvid"], "4412413519445700")

    def test_play_info_exposes_external_subtitle(self):
        rec = self._rec()
        mitm.registry[rec["guid"]] = rec
        data = mitm.synth_play_info(rec["guid"], rec)["data"]
        self.assertEqual(data["is_subtitle_external"], 1)
        self.assertTrue(data["subtitle_guid"].startswith("onsub"))
        self.assertTrue(data["subtitle_link"].startswith(
            "/v/api/v1/subtitle/dl/"))

    def test_play_exposes_external_subtitle(self):
        rec = self._rec()
        mitm.registry[rec["guid"]] = rec
        data = mitm.synth_play(rec["guid"], 1)["data"]
        self.assertEqual(data["subtitle_index"], 0)
        self.assertEqual("4412413519445700", mitm._resolve_sub(
            data["subtitle_guid"])["tvid"])


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


class VipCoverTest(unittest.TestCase):
    def test_vip_poster_uses_root_relative_gold_endpoint(self):
        u = mitm._vip_img_url("https://pic.example/a.jpg")
        self.assertTrue(u.startswith("/v/__mitm__/vipimg?v=3&u="))

    def test_vip_results_sort_first(self):
        items = [
            {"ancestor_name": "量子 · 更新至1集"},
            {"ancestor_name": "芒果VIP · 共1集"},
            {"ancestor_name": "爱奇艺VIP · 共1集"},
            {"ancestor_name": "咪咕VIP · 共1集"},
        ]
        got = mitm._vip_first_sort(items)
        names = [x["ancestor_name"].split(" · ")[0] for x in got[:3]]
        self.assertEqual(names, ["AI", "MI", "MANG"])

    def test_old_vip_record_displays_latin_source_name(self):
        rec = {
            "title": "测试片", "site": "爱奇艺VIP",
            "pic": "https://pic.example/a.jpg",
            "eps": [{"name": "正片", "url": "http://example.test/a.m3u8"}],
        }
        item = mitm.to_search_item("on" + "d" * 30, rec)
        self.assertTrue(item["ancestor_name"].startswith("AI"))
        self.assertEqual(mitm._vip_display_site("咪咕直播"), "MI 直播")

    def test_build_item_has_cover_dims(self):
        guid = "on" + "c" * 30
        item = mitm.build_item(guid, {
            "title": "测试片", "site": "爱奇艺VIP",
            "pic": "https://pic.example/a.jpg",
            "eps": [{"name": "正片", "url": "http://example.test/a.m3u8"}],
        })
        self.assertEqual(item["poster_width"], 400)
        self.assertEqual(item["poster_height"], 600)
        self.assertTrue(
            item["poster"].startswith("/v/__mitm__/vipimg?v=3&u="))


class LabelTest(unittest.TestCase):
    def test_migu_keepalive_accepts_real_cookies(self):
        import vip_keepalive
        auth = vip_keepalive.PLATFORMS["migu"]["auth"]
        for name in ("mgnd_session_id", "userInfo", "UserInfo", "checked_token"):
            self.assertIn(name, auth)

    def test_live_library_uses_latin_name(self):
        self.assertEqual(mitm.migu_live_lib_info()["name"], "MI 直播")


class ChromiumTest(unittest.TestCase):
    def test_existing_chromium_is_detected_and_not_redownloaded(self):
        from chromium_path import find_chromium
        if not find_chromium():
            self.skipTest("本机没有 Chromium，跳过")
        st = mitm._chromium_status()
        self.assertTrue(st["available"])
        ok, msg = mitm._start_chromium_install()
        self.assertFalse(ok)
        self.assertIn("已就绪", msg)


class PageTest(unittest.TestCase):
    def test_page_has_unlock_live_and_login_copy(self):
        page = webui.PAGE
        for needle in ("暗号", "开启 AI", "开启 MI", "开启 MANG",
                       "看直播电视", "VIP 在线源接进来", "已识别登录",
                       "建议短信登录", "扫码登录", "短信登录", "保活",
                       "会员", "2.0.1 正式版"):
            self.assertIn(needle, page, "配置页缺少：%s" % needle)
        self.assertNotIn("手机投屏", page)

    def test_secret_code_not_hardcoded_in_page(self):
        self.assertNotIn("aimimang", webui.PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
