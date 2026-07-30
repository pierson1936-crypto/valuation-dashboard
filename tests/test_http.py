import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from monitoring.config import MonitorConfig
from monitoring.db import MonitorRepository
from monitoring.web import MonitorWebController


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
        self.assertNotIn('id="mktFlowCanvas"', html)

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

    def test_chat_without_key_returns_controlled_error(self):
        with patch.object(app, "AGENT_KEY_ENV", ""):
            status, payload = self.post_json(
                "/api/chat", {"history": [{"role": "user", "content": "test"}]}
            )

        self.assertEqual(status, 200)
        self.assertIn("error", payload)
        self.assertIn("API Key", payload["error"])

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
