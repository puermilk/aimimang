#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线条目合成响应的「客户端数值字段」契约测试。

背景（真实故障）：飞牛影视 TV 端用 Gson 严格反序列化，
`VideoStream.dvProfile` / `ext1` 声明为 int，服务端却给了空字符串 "",
于是 Gson 抛 `NumberFormatException: empty String`，整个 /stream 响应作废，
播放器拿不到流信息 → 弹「抱歉，播放出错了」。网页端是 JS，容错，所以只有 TV 端炸。

这里不测函数名/内部结构，只锁定用户能看到的契约：
**客户端按数字解析的字段，JSON 里就必须是数字（或 null/缺省），不能是字符串。**
缺了这个约束，肉眼 review 发现不了，只有真机上电视才会炸。

注意：字段名会撞车（条目里的 `type` 是 "Movie"/"TV" 字符串，
file_stream 里的 `type` 才是数字），所以按容器分别声明，不能全局一把梭。

跑法：
    PYTHONPATH=src/trimonline/app/mitm/lib python3 tools/test_stream_types.py
"""
import os
import tempfile
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MITM = os.path.join(os.path.dirname(HERE), "src", "trimonline", "app", "mitm")
sys.path.insert(0, MITM)

import mitm                                      # noqa: E402

# 飞牛影视 TV 端按 int / Integer / Long 解析的字段（对照 com.trim.base.entity.*）
VIDEO_INT = {
    "dv_profile", "ext1", "coded_width", "coded_height", "width", "height",
    "index", "refs", "duration", "rotation", "bit_depth", "progressive", "bps",
}
AUDIO_INT = {
    "channels", "duration", "index", "bit_depth", "bps", "is_default", "is_fake",
    "start_timestamp",
}
FILE_INT = {
    "size", "type", "sort_num", "can_play", "timestamp", "create_time",
    "update_time", "file_birth_time",
}
PLAYINFO_INT = {
    "ts", "subtitle_index", "is_subtitle_external", "direct_link_audio_index",
    "isDirectLinkEnabled",
}
ITEM_INT = {
    "runtime", "vote_count", "year", "is_favorite", "is_watched", "watched_ts",
    "season_number", "episode_number", "number_of_episodes",
    "local_number_of_episodes", "local_number_of_seasons", "number_of_seasons",
    "can_play", "poster_width", "poster_height",
}
QUALITY_INT = {"bitrate"}


def _bad(obj, int_keys, path):
    """obj 里属于 int_keys 的字段若是字符串，就记一笔。"""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = "%s.%s" % (path, k)
            if k in int_keys and isinstance(v, str):
                bad.append("%s=%r" % (p, v))
    return bad


class StreamTypeContractTest(unittest.TestCase):
    def setUp(self):
        # 隔离配置目录：media_pass.key 会落在 CONF_DIR，绝不能碰线上真实目录
        self.tmp = tempfile.TemporaryDirectory()
        self._conf0 = mitm.CONF_DIR
        mitm.CONF_DIR = self.tmp.name
        mitm._MEDIA_PASS["secret"] = ""
        self.guid = "on" + "a" * 30
        mitm.registry[self.guid] = {
            "title": "测试片",
            "site": "测试源",
            "referer": "http://example.test/",
            "duration": 600,
            "eps": [{"name": "第1集",
                     "url": "http://example.test/a/index.m3u8"}],
        }

    def tearDown(self):
        mitm.registry.pop(self.guid, None)
        mitm.CONF_DIR = self._conf0
        mitm._MEDIA_PASS["secret"] = ""
        self.tmp.cleanup()

    def test_stream_video_fields_are_numbers(self):
        resp = mitm.synth_stream(self.guid)
        vs = resp["data"]["video_stream"]
        # 先确认测试自己有效：回归点字段确实存在
        self.assertIn("dv_profile", vs)
        self.assertIn("ext1", vs)
        self.assertEqual(_bad(vs, VIDEO_INT, "video_stream"), [],
                         "TV 端 Gson 会解析失败")

    def test_stream_audio_fields_are_numbers(self):
        resp = mitm.synth_stream(self.guid)
        bad = []
        for i, a in enumerate(resp["data"]["audio_streams"]):
            bad += _bad(a, AUDIO_INT, "audio_streams[%d]" % i)
        self.assertEqual(bad, [], "TV 端 Gson 会解析失败")

    def test_stream_file_fields_are_numbers(self):
        resp = mitm.synth_stream(self.guid)
        bad = _bad(resp["data"]["file_stream"], FILE_INT, "file_stream")
        self.assertEqual(bad, [], "TV 端 Gson 会解析失败")

    def test_stream_qualities_are_numbers(self):
        resp = mitm.synth_stream(self.guid)
        bad = []
        for i, q in enumerate(resp["data"]["qualities"]):
            bad += _bad(q, QUALITY_INT, "qualities[%d]" % i)
        self.assertEqual(bad, [], "TV 端 Gson 会解析失败")

    def test_play_info_fields_are_numbers(self):
        resp = mitm.synth_play_info(self.guid, dict(mitm.registry[self.guid]))
        data = resp["data"]
        bad = _bad(data, PLAYINFO_INT, "data")
        bad += _bad(data.get("item") or {}, ITEM_INT, "data.item")
        self.assertEqual(bad, [], "TV 端 Gson 会解析失败")

    def test_checker_catches_the_original_bug(self):
        """钉死回归点：字段再写成 "" 必须被抓住。"""
        self.assertEqual(len(_bad({"dv_profile": "", "ext1": ""},
                                  VIDEO_INT, "vs")), 2)


class TvPlaybackContractTest(unittest.TestCase):
    """TV 端播放链路契约（真机上就是靠这几个接口起播的）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._conf0 = mitm.CONF_DIR
        mitm.CONF_DIR = self.tmp.name
        mitm._MEDIA_PASS["secret"] = ""
        self.guid = "on" + "b" * 30
        mitm.registry[self.guid] = {
            "title": "测试剧",
            "site": "测试源",
            "referer": "http://example.test/",
            "duration": 600,
            "eps": [{"name": "第1集", "url": "http://example.test/a/1.m3u8"},
                    {"name": "第2集", "url": "http://example.test/a/2.m3u8"}],
        }

    def tearDown(self):
        mitm.registry.pop(self.guid, None)
        mitm.CONF_DIR = self._conf0
        mitm._MEDIA_PASS["secret"] = ""
        self.tmp.cleanup()

    def test_play_link_carries_pass(self):
        """play_link 必须在没有登录态时也能被清单侧放行。"""
        link = mitm.synth_play(self.guid)["data"]["play_link"]
        self.assertIn(".m3u8", link)
        self.assertIn("ps=", link, "play_link 缺清单通行证")
        self.assertIn("pe=", link, "play_link 缺通行证有效期")

    def test_stream_list_has_official_shape(self):
        """stream/list 回空数组时，客户端会退回 media/range 解复用而失败。"""
        data = mitm.synth_stream_list(self.guid)["data"]
        self.assertTrue(data["files"], "files 不能为空")
        self.assertEqual(data["files"][0]["guid"], self.guid)
        self.assertTrue(data["video_streams"], "video_streams 不能为空")

    def test_pass_roundtrip_accepts_own_signature(self):
        ps = mitm.media_pass_make(self.guid, 0)
        self.assertTrue(mitm.media_pass_ok("/v/media/online/%s.m3u8" % self.guid,
                                           {"ep": ["0"], "pe": [ps.split("pe=")[1].split("&")[0]],
                                            "ps": [ps.split("ps=")[1]]}))

    def test_pass_rejects_tampered_signature(self):
        ps = mitm.media_pass_make(self.guid, 0)
        pe = ps.split("pe=")[1].split("&")[0]
        self.assertFalse(mitm.media_pass_ok(
            "/v/media/online/%s.m3u8" % self.guid,
            {"ep": ["0"], "pe": [pe], "ps": ["0" * 24]}))

    def test_pass_rejects_other_item(self):
        """通行证只对签发的条目有效，不能拿去开别的片子。"""
        ps = mitm.media_pass_make(self.guid, 0)
        pe = ps.split("pe=")[1].split("&")[0]
        other = "on" + "c" * 30
        self.assertFalse(mitm.media_pass_ok(
            "/v/media/online/%s.m3u8" % other,
            {"ep": ["0"], "pe": [pe], "ps": [ps.split("ps=")[1]]}))


class PlaylistRangeTest(unittest.TestCase):
    """清单要能按 Range 供出：电视端先探 bytes=0-1 要 206，拿不到就不解析。"""

    class _W:
        def __init__(self):
            self.buf = b""

        def write(self, b):
            self.buf += b

        async def drain(self):
            return None

    def _serve(self, data, rng):
        import asyncio
        w = self._W()
        ok = asyncio.get_event_loop().run_until_complete(
            mitm._serve_playlist_bytes(w, data, {"range": rng}, None))
        head, _, body = w.buf.partition(b"\r\n\r\n")
        return ok, head.decode("latin-1"), body

    def test_no_range_returns_full_200(self):
        data = b"#EXTM3U\n" + b"x" * 100
        ok, head, body = self._serve(data, "")
        self.assertTrue(ok)
        self.assertIn("200 OK", head)
        self.assertEqual(body, data)

    def test_probe_range_returns_206_with_content_range(self):
        data = b"#EXTM3U\n" + b"x" * 100
        ok, head, body = self._serve(data, "bytes=0-1")
        self.assertTrue(ok)
        self.assertIn("206 Partial Content", head)
        self.assertIn("Content-Range: bytes 0-1/%d" % len(data), head)
        self.assertEqual(body, data[:2], "探测响应体必须正好是前 2 字节")


if __name__ == "__main__":
    unittest.main(verbosity=2)
