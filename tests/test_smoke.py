#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py 核心逻辑冒烟测试（无需真实网络/飞书）
================================================
运行: python3 tests/test_smoke.py
覆盖:
  1. .js?xxx query string 资源识别
  2. is_valid_page 页面有效性判断（真实页 / WAF挑战页 / 错误页 / 无特征页）
  3. save_state 原子写入（不留 .tmp 残留）
  4. P0 回归: 推送失败 → pending.json 暂存 + 基线保留；恢复后自动补推 + 基线更新
  5. P0 回归: JS 全量哈希（每轮完整、不因数量漂移导致 snap_hash 变化）
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

REAL_HTML = """<!doctype html><html lang="en"><head><meta charset="UTF-8">
<title>DeepSeek</title>
<meta name="commit-id" content="a610cb2">
<meta name="description" content="Chat with DeepSeek AI">
<meta name="ip" content="1.2.3.4"><meta name="region" content="CN">
<script src="/static/main.7863ea53ee.js"></script>
<script src="/static/default-vendors.80d8928a1a.js?v=2"></script>
<link rel="stylesheet" href="/static/main.0dd14bec80.css">
</head><body>DeepSeek 欢迎使用。</body></html>
<!-- padding: 用于让 HTML 长度超过页面有效性判断的最小阈值 500 字符，模拟真实页面的体积；
     真实 DeepSeek 首页约 10000 字符。这一段注释与正文无关，仅用于测试构造，
     不影响任何断言逻辑。padding padding padding padding padding padding -->
"""


class TestBasics(unittest.TestCase):
    def test_query_string_js(self):
        from urllib.parse import urlparse
        for u in ("https://cdn.x.com/app.js?v=123",
                  "https://cdn.x.com/chunk.js?hash=abc&t=1",
                  "https://cdn.x.com/app.js"):
            self.assertTrue(urlparse(u).path.lower().endswith(".js"), u)
        self.assertFalse(urlparse("https://cdn.x.com/app.jsx").path.endswith(".js"))

    def test_is_valid_page(self):
        snap = monitor.extract_snapshot(REAL_HTML)
        self.assertTrue(monitor.is_valid_page(200, snap, REAL_HTML))
        # 动态 meta 被排除
        self.assertNotIn("ip", snap["metas"])
        self.assertNotIn("region", snap["metas"])

        # AWS WAF 挑战页必须被拒
        waf = ('<html><head><title></title>'
               '<script src="https://xxx.token.awswaf.com/xxx/challenge.js"></script>'
               '<body><div id="challenge-container"></div>'
               '<script>AwsWafIntegration.getToken()</script></body></html>')
        self.assertFalse(monitor.is_valid_page(200, monitor.extract_snapshot(waf), waf))

        # 错误页 / 无 DeepSeek 特征页必须被拒
        err = "<html><head><title>Service unavailable</title></head><body>503</body></html>"
        self.assertFalse(monitor.is_valid_page(503, monitor.extract_snapshot(err), err))
        other = "<html><head><title>Hello World</title></head><body>hi</body></html>"
        self.assertFalse(monitor.is_valid_page(200, monitor.extract_snapshot(other), other))

    def test_save_state_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            monitor.STATE_FILE = Path(d) / "snapshot.json"
            monitor.save_state({"a": 1, "b": [1, 2, 3]})
            self.assertEqual(json.loads(monitor.STATE_FILE.read_text(encoding="utf-8")),
                             {"a": 1, "b": [1, 2, 3]})
            # 无 .tmp 残留
            self.assertFalse(monitor.STATE_FILE.with_suffix(".tmp").exists())


class TestJSHashStable(unittest.TestCase):
    """P0 回归：每轮全量哈希，js_hashes 集合恒定 → snap_hash 不漂移。"""

    def _run_once(self):
        """用 mock 的 fetch 模拟真实页面，跑一轮 do_run，返回新 state。"""
        def fake_fetch(url, timeout=30, max_bytes=None, etag=None, last_modified=None):
            if url.startswith("http") and ".js" in url:
                return 200, url, "console.log('deepseek %s')" % url, "W/\"etag-1\"", None
            return 200, url, REAL_HTML, None, None

        with mock.patch.object(monitor, "fetch", side_effect=fake_fetch), \
                mock.patch.object(monitor, "send_feishu", return_value=(True, "ok")):
            monitor.do_run()
        return monitor.load_state()

    def test_no_drift_across_runs(self):
        with tempfile.TemporaryDirectory() as d:
            monitor.STATE_FILE = Path(d) / "snapshot.json"
            monitor.PENDING_FILE = Path(d) / "pending.json"
            s1 = self._run_once()
            s2 = self._run_once()
            self.assertEqual(s1["baseline_hash"], s2["baseline_hash"],
                             "网站无变化时 snap_hash 不得漂移")
            self.assertEqual(s1["js_hashes"].keys(), s2["js_hashes"].keys())
            self.assertEqual(len(s1["js_hashes"]), 2, "两个 .js 都必须被哈希（含 ?v= 的）")


class TestPendingRetry(unittest.TestCase):
    """P0 回归：推送失败不更新基线；恢复后补推成功再更新。"""

    def _state(self, d, baseline_hash, snap):
        monitor.STATE_FILE = Path(d) / "snapshot.json"
        monitor.PENDING_FILE = Path(d) / "pending.json"
        monitor.save_state({
            "baseline_hash": baseline_hash,
            "snapshot": snap,
            "js_hashes": {},
            "consecutive_failures": 0,
            "last_check": "t0",
            "last_status": "ok",
        })

    def _run(self, send_ok):
        def fake_fetch(url, timeout=30, max_bytes=None, etag=None, last_modified=None):
            if url.startswith("http") and ".js" in url:
                return 200, url, "console.log('x')", None, None
            return 200, url, REAL_HTML, None, None
        with mock.patch.object(monitor, "fetch", side_effect=fake_fetch), \
                mock.patch.object(monitor, "send_feishu", return_value=(send_ok, "resp")):
            monitor.do_run()

    def test_fail_keeps_baseline_then_retry(self):
        old_snap = monitor.extract_snapshot(REAL_HTML)
        # 制造一个「旧基线」：commit-id 改成旧值
        with tempfile.TemporaryDirectory() as d:
            self._state(d, "OLD_BASELINE", old_snap)

            # 第 1 轮：检测到变化，但推送失败 → 基线必须保留 + pending 生成
            self._run(send_ok=False)
            s = monitor.load_state()
            self.assertEqual(s["baseline_hash"], "OLD_BASELINE",
                             "推送失败后不得更新基线")
            self.assertTrue(monitor.PENDING_FILE.exists(), "失败后必须暂存 pending")

            # 第 2 轮：推送恢复 → 补推成功 → pending 清除 + 基线更新
            self._run(send_ok=True)
            s = monitor.load_state()
            self.assertFalse(monitor.PENDING_FILE.exists(), "补推成功后 pending 应删除")
            self.assertNotEqual(s["baseline_hash"], "OLD_BASELINE",
                                "补推成功后基线应更新")

            # 第 3 轮：页面无变化 → 不再推送
            with mock.patch.object(monitor, "send_feishu", return_value=(True, "ok")) as m:
                self._run(send_ok=True)
                self.assertEqual(s["baseline_hash"], monitor.load_state()["baseline_hash"])


class TestStringExtract(unittest.TestCase):
    """新版本具体内容：JS 字符串提取与新旧对比。"""

    def test_extract_js_strings(self):
        js = '''var a = "新增多模态能力";
var b = 'DeepSeek-V4 正式发布，支持文件上传';
var c = "The quick brown fox jumps over the lazy dog";
var d = "a1b2c3d4e5f6a1b2c3d4e5f6";          // 纯哈希 → 过滤
var e = "https://example.com/path";           // URL → 过滤
var f = "ab";                                  // 过短 → 过滤
var g = "getUserInfoList";                     // 无空格驼峰标识符 → 过滤
var h = "\\u65b0\\u589e\\u6df1\\u5ea6\\u601d\\u8003\\u6a21\\u5f0f";  // \\u 转义中文
var i = "12345";                               // 纯数字 → 过滤
'''
        strs = monitor.extract_js_strings(js)
        joined = "\n".join(strs)
        self.assertIn("新增多模态能力", joined)
        self.assertIn("DeepSeek-V4 正式发布，支持文件上传", joined)
        self.assertIn("The quick brown fox jumps over the lazy dog", joined)
        self.assertIn("新增深度思考模式", joined)  # \uXXXX 已解码
        for noise in ("a1b2c3d4e5f6a1b2c3d4e5f6", "https://example.com/path",
                      "ab", "getUserInfoList", "12345"):
            self.assertNotIn(noise, joined)

    def test_dangling_quote_lexer(self):
        """回归：minified JS 中悬空引号/模板字符串会导致正则错位，词法扫描必须免疫。

        模板字符串内含未闭合的双引号 + 后面的真实字符串，必须全部正确识别。
        """
        js = (
            "var tpl = `{name:\"downloadCodeFile\",message:\"中间被吞掉的文案\","
            "payload:\"\"}}`;\n"
            "tracker.info({name:\"downloadCodeFile\",message:\"下载代码文件\","
            "payload:{infostring:t||\"\"}});\n"
            "var a = \"新增多模态能力\";"
        )
        strs = monitor.extract_js_strings(js)
        joined = "\n".join(strs)
        self.assertIn("下载代码文件", joined)
        self.assertIn("新增多模态能力", joined)
        # 模板字符串内部的内容不应被错误提取为独立字符串
        self.assertNotIn("中间被吞掉的文案", joined)

    def test_build_string_diff_report(self):
        old = {"旧版文案A", "共同文案", "旧功能说明"}
        new = {"新版文案B", "共同文案", "新增的深度思考功能说明"}
        report = monitor.build_string_diff_report(old, new)
        self.assertIn("🧬", report)
        self.assertIn("新版文案B", report)
        self.assertIn("新增的深度思考功能说明", report)
        self.assertIn("旧版文案A", report)
        # 旧基线缺失 → 不生成对比报告
        self.assertIsNone(monitor.build_string_diff_report(set(), new))
        # 无变化 → 无报告
        self.assertIsNone(monitor.build_string_diff_report({"x"}, {"x"}))


class TestNewsMonitor(unittest.TestCase):
    """官方新闻公告监测：首次建基线不推送、新公告推送、失败重试。"""

    def test_first_run_records_only(self):
        seen = {}
        with mock.patch.object(monitor, "fetch_news_index",
                               return_value=["/news/news260424"]), \
                mock.patch.object(monitor, "send_feishu") as m:
            seen = monitor.check_news(seen)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen["/news/news260424"]["pushed"])
        m.assert_not_called()  # 首次运行不推送历史

    def test_new_article_push_and_retry(self):
        seen = {"/news/news260424": {"title": "旧新闻", "summary": "", "pushed": True}}
        # 第 1 轮：出现新公告，推送失败 → 保留待重试
        with mock.patch.object(monitor, "fetch_news_index",
                               return_value=["/news/news260424", "/news/news260500"]), \
                mock.patch.object(monitor, "fetch_news_article",
                                  return_value=("DeepSeek V5 Release", "新版本发布！")), \
                mock.patch.object(monitor, "send_feishu",
                                  return_value=(False, "err")) as m:
            seen = monitor.check_news(seen)
        self.assertEqual(m.call_count, 1)
        self.assertFalse(seen["/news/news260500"]["pushed"], "推送失败必须保留待重试")
        # 第 2 轮：重试成功 → 标记已推送
        with mock.patch.object(monitor, "fetch_news_index",
                               return_value=["/news/news260424", "/news/news260500"]), \
                mock.patch.object(monitor, "send_feishu",
                                  return_value=(True, "ok")) as m:
            seen = monitor.check_news(seen)
        self.assertTrue(seen["/news/news260500"]["pushed"])
        # 第 3 轮：无新公告 → 不再推送
        with mock.patch.object(monitor, "fetch_news_index",
                               return_value=["/news/news260424", "/news/news260500"]), \
                mock.patch.object(monitor, "send_feishu") as m:
            seen = monitor.check_news(seen)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
