import json
import base64
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import struct
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.web import MonitorWebController


def _chat_png_data_url(width=12, height=12):
    def chunk(kind, data):
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x42\x42\x42" * width
    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


class HttpSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        config = MonitorConfig(
            db_path=Path(cls.temp_dir.name) / "monitor.db",
            poll_seconds=15,
        )
        repository = MonitorRepository(config.db_path)
        app._MONITOR_WEB_CONTROLLER = MonitorWebController(config, repository)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        app._MONITOR_WEB_CONTROLLER.stop()
        app._MONITOR_WEB_CONTROLLER = None
        cls.temp_dir.cleanup()

    def get_json(self, path):
        with urllib.request.urlopen(self.base_url + path, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post_json(self, path, payload):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_root_serves_current_embedded_frontend(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn('id="mktFlowSvg"', html)
        self.assertIn("watch-head", html)
        self.assertIn('id="tab-monitor"', html)
        self.assertIn("/api/monitor/overview", html)
        self.assertIn("/api/monitor/simulate", html)
        self.assertIn("持仓逻辑转化", html)
        self.assertIn("/api/monitor/draft", html)
        self.assertIn("生成待确认风险规则", html)
        self.assertIn("advanced_mode:false", html)
        self.assertIn("人工复核事项", html)
        self.assertIn("异动 未设置", html)
        self.assertIn("w.configured?", html)
        self.assertIn("/api/monitor/explain", html)
        self.assertIn("klinePctSeries", html)
        self.assertIn("日涨跌幅", html)
        self.assertIn('id="watchModeHoldings"', html)
        self.assertIn('id="holdingScreenshot"', html)
        self.assertIn('id="chatImageInput"', html)
        self.assertIn('id="chatImageAttachment"', html)
        self.assertIn("selectChatImage", html)
        self.assertIn("clearChatImage", html)
        self.assertIn("chatPendingImage", html)
        self.assertIn('id="holdingQwenKey"', html)
        self.assertIn('id="holdingQwenBase"', html)
        self.assertIn("selectHoldingScreenshot", html)
        self.assertIn('id="holdingDropzone"', html)
        self.assertIn("dropHoldingScreenshot", html)
        self.assertIn("holdingDragEnter", html)
        self.assertIn("normalizeHoldingImageFile", html)
        self.assertIn("支持从微信直接拖入", html)
        self.assertIn("微信没有提供可读取的 PNG/JPG 文件", html)
        self.assertIn("/api/monitor/holding-ocr", html)
        self.assertIn("/api/monitor/holdings", html)
        self.assertIn("截图会发送至阿里云百炼", html)
        self.assertIn('id="holdingQuoteStatus"', html)
        self.assertIn('id="holdingQuoteRefreshBtn"', html)
        self.assertIn("refreshHoldingQuotes", html)
        self.assertIn("最新价 / 今日涨幅", html)
        self.assertIn("持仓收益率 / 成本数量", html)
        self.assertIn("holdingPnl", html)
        self.assertIn('id="portfolioJudgment"', html)
        self.assertIn("我的当前判断", html)
        self.assertIn('id="portfolioReportBtn"', html)
        self.assertIn('id="portfolioScanBtn"', html)
        self.assertIn('id="portfolioAiProvider"', html)
        self.assertIn('id="deepseekModel"', html)
        self.assertIn('value="deepseek-v4-flash"', html)
        self.assertIn('value="deepseek-v4-pro"', html)
        self.assertIn("deepseek_model:getDeepSeekModel()", html)
        self.assertIn("/api/monitor/portfolio-scan", html)
        self.assertIn("GPT 5.6 Sol", html)
        self.assertIn("公司定位与概念", html)
        self.assertIn("company_context", html)
        self.assertIn("/api/intraday", html)
        self.assertIn("今日分时 · 相对强弱", html)
        self.assertIn("loadIntraday", html)
        self.assertIn("/api/key-levels", html)
        self.assertIn('id="klineStructureBtn"', html)
        self.assertIn('id="chipStructureBtn"', html)
        self.assertIn("K线结构", html)
        self.assertIn("筹码结构", html)
        self.assertIn("toggleKeyLevelView", html)
        self.assertIn("currentKeyLevelView===view?null:view", html)
        self.assertIn("retryUnavailableChip", html)
        self.assertIn("&retry=1", html)
        self.assertIn("重试筹码结构", html)
        self.assertIn("keyLevelRequest&&keyLevelRequest.code===code", html)
        self.assertIn("近120日估算筹码", html)
        self.assertIn("['原始数据',chip.source_label||'—']", html)
        self.assertNotIn("70%估算成本区", html)
        self.assertNotIn("估算获利占比", html)
        self.assertNotIn("盘口快照", html)
        self.assertNotIn("买一", html)
        self.assertNotIn("卖一", html)
        self.assertNotIn("布林带", html)
        self.assertNotIn("布林上", html)
        self.assertNotIn("布林下", html)
        self.assertIn("/api/security_report", html)
        self.assertIn("生成 AI 独立分析", html)
        self.assertIn("模型自主选择重点", html)
        self.assertIn("展开规则数据底稿", html)
        self.assertIn("/api/monitor/portfolio-report", html)
        self.assertIn('id="portfolioReportHistory"', html)
        self.assertIn("/api/monitor/portfolio-report/history", html)
        self.assertIn("最近 7 次分析", html)
        self.assertIn("selectPortfolioReportHistory", html)
        self.assertIn("AI判断", html)
        self.assertIn("一致点", html)
        self.assertIn("分歧点", html)
        self.assertIn("可能遗漏", html)
        for kind in ("stock", "market", "portfolio", "panel"):
            self.assertIn(f"openAnalysisHelp('{kind}')", html)
        for heading in (
            "使用哪些数据",
            "如何形成分析",
            "主要回答什么",
            "不能判断什么",
            "数据截止与周期",
        ):
            self.assertIn(heading, html)
        self.assertIn("代码先计算仓位、累计与近期盈亏贡献", html)
        self.assertIn("静态历史按当前持仓数量回看", html)
        self.assertNotIn("openAnalysisHelp('multidim')", html)
        self.assertNotIn('id="multiDimCard"', html)
        self.assertNotIn("启动多维分析", html)
        self.assertNotIn("openMultiDim", html)
        self.assertIn("最大高相关连通组", html)
        self.assertIn("当前只有可分析持仓子集", html)
        self.assertIn("concentration.volatility_coverage_pct", html)
        self.assertNotIn("concentration.high_volatility_coverage_pct", html)
        self.assertNotIn("Tesseract", html)
        self.assertNotIn("tesseract.min.js", html)
        self.assertNotIn("parseHoldingOcrData", html)
        self.assertNotIn('id="mktFlowCanvas"', html)

    def test_embedded_frontend_uses_portfolio_volatility_coverage_contract(self):
        self.assertIn("concentration.volatility_coverage_pct", app.HTML)
        self.assertNotIn("concentration.high_volatility_coverage_pct", app.HTML)

    def test_watchlist_group_tracking_contract_is_present(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        for marker in (
            'id="wgroup"',
            'id="watchGroupDialog"',
            "watch_group_history_v1",
            "recordWatchGroupSnapshots",
            "watchGroupSignal",
            "watchGroupSparkline",
            "openWatchGroupDialog",
            "openRenameWatchGroupDialog",
            "renameWatchGroupHistory",
            'id="watchGroupDialogTitle"',
            "编辑分组名称",
            "同步回暖",
            "组均涨幅",
            "上涨家数",
        ):
            self.assertIn(marker, html)

    def test_invalid_analysis_input_is_rejected_before_analysis(self):
        with patch.object(app, "analyze_cached") as analyze_cached:
            status, payload = self.get_json("/api/analyze?code=abc")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"error": "请输入6位数字代码"})
        analyze_cached.assert_not_called()

    def test_analysis_endpoint_returns_stable_json_shape(self):
        fixed = {"code": "600000", "price": 12.34, "price_pct": 42.0, "tech": {}}
        with patch.object(app, "analyze_cached", return_value=fixed):
            status, payload = self.get_json("/api/analyze?code=600000")

        self.assertEqual(status, 200)
        self.assertEqual(payload, fixed)

    def test_intraday_endpoint_returns_isolated_comparison(self):
        fixed = {"code": "600000", "name": "固定样例", "tech": {}}
        comparison = {
            "code": "600000",
            "subject": {"name": "固定样例", "points": []},
            "summary": {},
        }
        with (
            patch.object(app, "analyze_cached", return_value=fixed),
            patch.object(app, "build_intraday_comparison", return_value=comparison) as build,
        ):
            status, payload = self.get_json("/api/intraday?code=600000")

        self.assertEqual(status, 200)
        self.assertEqual(payload, comparison)
        build.assert_called_once_with(fixed)

    def test_key_level_endpoint_is_explicit_and_on_demand(self):
        expected = {
            "code": "600000",
            "box": {"lower": 9.8, "upper": 10.6},
            "chip": {"average_cost": 10.1},
            "chip_status": "available",
        }
        with patch.object(app, "key_levels_cached", return_value=expected) as build:
            status, payload = self.get_json("/api/key-levels?code=600000")

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        build.assert_called_once_with("600000", retry_failure=False)

    def test_key_level_endpoint_can_retry_only_a_cached_failure(self):
        expected = {"code": "600000", "chip_status": "available"}
        with patch.object(app, "key_levels_cached", return_value=expected) as build:
            status, payload = self.get_json(
                "/api/key-levels?code=600000&retry=1"
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        build.assert_called_once_with("600000", retry_failure=True)

    def test_key_level_endpoint_rejects_invalid_code_before_fetch(self):
        with patch.object(app, "key_levels_cached") as build:
            status, payload = self.get_json("/api/key-levels?code=abc")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"error": "请输入6位数字代码"})
        build.assert_not_called()

    def test_security_report_route_is_explicit_and_does_not_return_key(self):
        expected = {
            "code": "600000",
            "name": "固定样例",
            "report": "独立判断。[[E01]][[E02]]",
            "evidence": [],
            "model": "deepseek-v4-pro",
        }
        with patch.object(
            app, "generate_security_ai_report", return_value=expected
        ) as generate:
            status, payload = self.post_json(
                "/api/security_report",
                {
                    "code": "600000",
                    "key": "browser-test-key",
                    "deepseek_model": "deepseek-v4-pro",
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        self.assertNotIn("browser-test-key", json.dumps(payload))
        generate.assert_called_once_with(
            "600000", "browser-test-key", "deepseek-v4-pro"
        )

    def test_security_report_requires_valid_code_and_key(self):
        with patch.object(app, "generate_security_ai_report") as generate:
            _, bad_code = self.post_json(
                "/api/security_report", {"code": "abc", "key": "test-key"}
            )
            with patch.object(app, "AGENT_KEY_ENV", ""):
                _, missing_key = self.post_json(
                    "/api/security_report", {"code": "600000"}
                )

        self.assertIn("6位数字代码", bad_code["error"])
        self.assertIn("DeepSeek Key", missing_key["error"])
        generate.assert_not_called()

    def test_chat_without_key_returns_controlled_error(self):
        with patch.object(app, "AGENT_KEY_ENV", ""):
            status, payload = self.post_json(
                "/api/chat", {"history": [{"role": "user", "content": "test"}]}
            )

        self.assertEqual(status, 200)
        self.assertIn("error", payload)
        self.assertIn("API Key", payload["error"])

    def test_chat_forwards_supported_model_and_rejects_unknown_model(self):
        with patch.object(
            app,
            "agent_run",
            return_value=("完成", [], [{"role": "assistant", "content": "完成"}]),
        ) as agent_run:
            _, result = self.post_json(
                "/api/chat",
                {
                    "history": [{"role": "user", "content": "测试"}],
                    "key": "browser-test-key",
                    "deepseek_model": "deepseek-v4-pro",
                },
            )

        self.assertEqual(result["model"], "deepseek-v4-pro")
        self.assertEqual(result["model_label"], "DeepSeek V4 Pro")
        agent_run.assert_called_once_with(
            [{"role": "user", "content": "测试"}],
            "browser-test-key",
            "deepseek-v4-pro",
        )

        with patch.object(app, "agent_run") as rejected_call:
            _, rejected = self.post_json(
                "/api/chat",
                {
                    "history": [],
                    "key": "browser-test-key",
                    "deepseek_model": "deepseek-unknown",
                },
            )
        self.assertIn("V4 Flash 或 V4 Pro", rejected["error"])
        rejected_call.assert_not_called()

    def test_chat_image_is_read_by_qwen_before_deepseek(self):
        image_data_url = _chat_png_data_url()
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请看看这张图"},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ]
        qwen_result = {
            "observation": "可见信息：图片显示 PE 为 18.2。\n无法确认：图片未显示统计样本期。",
            "model": "qwen3.7-flash",
        }
        with patch("monitoring.holding_ocr.describe_chat_screenshot", return_value=qwen_result) as vision, \
             patch.object(app, "agent_run", side_effect=lambda prepared, *_: ("完成", [], prepared + [{"role": "assistant", "content": "完成"}])) as agent_run:
            _, result = self.post_json(
                "/api/chat",
                {
                    "history": history,
                    "key": "browser-test-key",
                    "deepseek_model": "deepseek-v4-flash",
                    "qwen_key": "qwen-test-key",
                    "qwen_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                },
            )

        vision.assert_called_once_with(
            image_data_url,
            "qwen-test-key",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        forwarded_history = agent_run.call_args.args[0]
        self.assertIn("图片显示 PE 为 18.2", forwarded_history[0]["content"])
        self.assertNotIn("data:image", forwarded_history[0]["content"])
        returned_history = result["history"]
        self.assertIn("请看看这张图", returned_history[0]["content"])
        self.assertIn("图片未显示统计样本期", returned_history[0]["content"])
        self.assertNotIn("data:image", json.dumps(returned_history))

    def test_monitor_setup_overview_and_remove(self):
        status, saved = self.post_json(
            "/api/monitor/setup",
            {
                "code": "600519",
                "name": "贵州茅台",
                "watch_price": 1100,
                "risk_price": 1000,
                "target_price": 1400,
            },
        )
        overview_status, overview = self.get_json("/api/monitor/overview")

        self.assertEqual(status, 200)
        self.assertTrue(saved["ok"])
        self.assertEqual(overview_status, 200)
        self.assertEqual(overview["counts"]["watches"], 1)
        self.assertEqual(overview["counts"]["rules"], 5)
        self.assertEqual(overview["watches"][0]["watch_price"], 1100)
        self.assertEqual(overview["settings"]["token_usage_per_poll"], 0)
        self.assertNotIn("db_path", overview["settings"])

        _, removed = self.post_json("/api/monitor/remove", {"code": "600519"})
        _, after = self.get_json("/api/monitor/overview")
        self.assertTrue(removed["removed"])
        self.assertEqual(after["counts"]["watches"], 0)

    def test_monitor_setup_rejects_invalid_price_order(self):
        _, payload = self.post_json(
            "/api/monitor/setup",
            {
                "code": "600519",
                "watch_price": 100,
                "risk_price": 110,
            },
        )

        self.assertIn("error", payload)
        self.assertIn("风险价必须低于关注价", payload["error"])

    def test_holdings_api_saves_confirmed_rows_and_lists_them(self):
        status, saved = self.post_json(
            "/api/monitor/holdings",
            {
                "holdings": [
                    {
                        "code": "300750",
                        "name": "宁德时代",
                        "quantity": 100,
                        "cost_price": 180.5,
                    }
                ]
            },
        )
        listed_status, listed = self.get_json("/api/monitor/holdings")

        self.assertEqual(status, 200)
        self.assertEqual(saved["errors"], [])
        self.assertEqual(saved["saved"][0]["code"], "300750")
        self.assertEqual(listed_status, 200)
        self.assertEqual(listed["holdings"][0]["quantity"], 100)
        self.assertEqual(listed["holdings"][0]["cost_price"], 180.5)

        _, invalid = self.post_json(
            "/api/monitor/holdings",
            {
                "holdings": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "quantity": -1,
                        "cost_price": 1000,
                    }
                ]
            },
        )
        _, unchanged = self.get_json("/api/monitor/holdings")
        self.assertEqual(invalid["saved"], [])
        self.assertEqual(invalid["errors"][0]["field"], "quantity")
        self.assertEqual([item["code"] for item in unchanged["holdings"]], ["300750"])

        self.post_json("/api/monitor/remove", {"code": "300750"})

    def test_holding_ocr_route_forwards_key_without_returning_it(self):
        expected = {
            "holdings": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "quantity": 100,
                    "cost_price": 180.5,
                    "needs_review": False,
                }
            ],
            "needs_review": [],
            "cached": False,
            "model": "qwen3.7-flash",
        }
        controller = app._MONITOR_WEB_CONTROLLER
        payload = {
            "image_data_url": "data:image/png;base64,ZmFrZQ==",
            "base_url": (
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
        }
        with patch.object(
            controller,
            "recognize_holding_screenshot",
            return_value=expected,
        ) as recognize:
            request = urllib.request.Request(
                self.base_url + "/api/monitor/holding-ocr",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Qwen-Api-Key": "browser-test-key",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read().decode("utf-8"))

        self.assertEqual(result, expected)
        recognize.assert_called_once_with(payload, "browser-test-key")
        self.assertNotIn("browser-test-key", json.dumps(result))

    def test_portfolio_report_route_forwards_judgment_and_key_without_returning_key(self):
        controller = app._MONITOR_WEB_CONTROLLER
        expected = {
            "report_id": 1,
            "user_judgment": "我担心组合波动",
            "status": "complete",
            "cached": False,
            "token_usage": 200,
        }
        payload = {
            "user_judgment": "我担心组合波动",
            "key": "browser-deepseek-key",
        }
        with patch.object(
            controller, "generate_portfolio_report", return_value=expected
        ) as generate:
            _, result = self.post_json("/api/monitor/portfolio-report", payload)

        self.assertEqual(result, expected)
        generate.assert_called_once_with(payload, "browser-deepseek-key")
        self.assertNotIn("browser-deepseek-key", json.dumps(result))

        gpt_payload = {
            "user_judgment": "我担心组合波动",
            "provider": "gpt",
            "key": "browser-openai-key",
        }
        with patch.object(
            controller, "generate_portfolio_report", return_value=expected
        ) as generate_gpt:
            _, gpt_result = self.post_json(
                "/api/monitor/portfolio-report", gpt_payload
            )
        self.assertEqual(gpt_result, expected)
        generate_gpt.assert_called_once_with(gpt_payload, "browser-openai-key")
        self.assertNotIn("browser-openai-key", json.dumps(gpt_result))

        with patch.object(
            controller,
            "latest_portfolio_report",
            return_value={"report": expected},
        ):
            _, latest = self.get_json("/api/monitor/portfolio-report/latest")
        self.assertEqual(latest["report"]["report_id"], 1)

        with patch.object(
            controller,
            "portfolio_report_history",
            return_value={"reports": [expected], "limit": 7},
        ):
            _, history = self.get_json("/api/monitor/portfolio-report/history")
        self.assertEqual(history["limit"], 7)
        self.assertEqual(history["reports"][0]["report_id"], 1)

        scan_result = {"ai_used": False, "token_usage": 0, "analytics": {}}
        with patch.object(
            controller, "portfolio_scan", return_value=scan_result
        ) as scan:
            _, result = self.get_json("/api/monitor/portfolio-scan")
        self.assertEqual(result, scan_result)
        scan.assert_called_once_with()

    def test_monitor_simulation_previews_without_event_or_service(self):
        self.post_json(
            "/api/monitor/setup",
            {
                "code": "600519",
                "name": "贵州茅台",
                "watch_price": 1100,
                "risk_price": 1000,
            },
        )
        controller = app._MONITOR_WEB_CONTROLLER
        before = controller.repository.count_rows("events")

        status, preview = self.post_json(
            "/api/monitor/simulate", {"code": "600519", "kind": "risk"}
        )

        self.assertEqual(status, 200)
        self.assertTrue(preview["simulated"])
        self.assertEqual(preview["threshold"], 1000)
        self.assertIn("【卖出关注条件】贵州茅台（600519）", preview["message"])
        self.assertFalse(preview["persisted"])
        self.assertFalse(preview["notification_sent"])
        self.assertEqual(preview["token_usage"], 0)
        self.assertEqual(controller.repository.count_rows("events"), before)
        self.assertIsNone(controller._service)

    def test_monitor_runtime_rejects_unknown_action(self):
        _, payload = self.post_json(
            "/api/monitor/runtime", {"action": "unsupported"}
        )

        self.assertEqual(payload, {"error": "不支持的盯盘操作"})

    def test_monitor_logic_is_saved_without_creating_rules(self):
        status, payload = self.post_json(
            "/api/monitor/logic",
            {
                "code": "600519",
                "name": "贵州茅台",
                "thesis": "明确价格出现后复核",
                "invalidation": "原假设失效",
                "review_items": "核实公告",
            },
        )
        _, overview = self.get_json("/api/monitor/overview")

        self.assertEqual(status, 200)
        self.assertTrue(payload["logic"]["configured"])
        self.assertTrue(overview["watches"][0]["logic"]["configured"])
        self.assertEqual(overview["counts"]["rules"], 0)

    def test_monitor_ai_endpoints_require_key_when_not_cached(self):
        _, draft = self.post_json(
            "/api/monitor/draft",
            {"code": "600519", "logic_text": "明确价格后复核"},
        )
        _, explanation = self.post_json(
            "/api/monitor/explain", {"event_id": 999}
        )

        self.assertIn("DEEPSEEK_API_KEY", draft["error"])
        self.assertIn("DeepSeek Key", explanation["error"])

    def test_unknown_route_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base_url + "/not-found", timeout=3)

        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
