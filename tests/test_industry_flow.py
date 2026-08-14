# -*- coding: utf-8 -*-
"""行业板块资金流（东财行业板块 clist / fflow）的离线单元测试。
全部使用 mock，不访问真实网络接口；快照重定向到临时文件，不读写真实 data 目录。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app


def _boards_payload():
    """clist 接口的典型返回（数组形态 diff）。"""
    return {"data": {"diff": [
        {"f12": "BK0475", "f14": "银行", "f3": 1.2, "f62": 5.6e8, "f184": 3.1},
        {"f12": "BK0737", "f14": "半导体", "f3": -0.8, "f62": -2.3e8, "f184": -1.5},
        {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
    ]}}


class TestIndustryRealtime(unittest.TestCase):
    def setUp(self):
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, None
        app._MKT[0], app._MKT[1] = 0.0, None

    def test_parse_array_form(self):
        with mock.patch.object(app, "fetch_json", return_value=_boards_payload()):
            boards = app.fetch_industry_boards_realtime()
        self.assertEqual(len(boards), 3)
        # 按主力净流入降序：计算机(8.9) > 银行(5.6) > 半导体(-2.3)
        self.assertEqual([b["name"] for b in boards], ["计算机", "银行", "半导体"])
        # 1e8 换算为亿元
        self.assertAlmostEqual(boards[0]["main_net"], 8.9)
        self.assertAlmostEqual(boards[1]["main_net"], 5.6)
        self.assertAlmostEqual(boards[2]["main_net"], -2.3)
        # 字段映射
        self.assertEqual(boards[0]["code"], "BK0436")
        self.assertEqual(boards[0]["chg"], 2.5)
        self.assertEqual(boards[0]["main_pct"], 5.2)

    def test_parse_dict_form(self):
        # 部分情况下 diff 是 {序号: 对象} 的字典，需兼容
        payload = {"data": {"diff": {
            "0": {"f12": "BK0475", "f14": "银行", "f3": 1.2, "f62": 5.6e8, "f184": 3.1},
            "1": {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
        }}}
        with mock.patch.object(app, "fetch_json", return_value=payload):
            boards = app.fetch_industry_boards_realtime()
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[0]["name"], "计算机")

    def test_dash_and_missing_fields(self):
        payload = {"data": {"diff": [
            {"f12": "BK0475", "f14": "银行", "f3": "-", "f62": "-", "f184": "-"},
            {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
        ]}}
        with mock.patch.object(app, "fetch_json", return_value=payload):
            boards = app.fetch_industry_boards_realtime()
        self.assertEqual(len(boards), 2)
        bank = [b for b in boards if b["name"] == "银行"][0]
        self.assertIsNone(bank["chg"])
        self.assertIsNone(bank["main_net"])

    def test_failure_returns_empty(self):
        with mock.patch.object(app, "fetch_json", side_effect=Exception("502")):
            boards = app.fetch_industry_boards_realtime()
        self.assertEqual(boards, [])


class TestIndustryHistory(unittest.TestCase):
    def test_history_parse(self):
        payload = {"data": {"klines": [
            "2026-08-12,56000000,-12000000,3000000,20000000,40000000",
            "2026-08-13,89000000,-15000000,5000000,30000000,59000000",
        ]}}
        with mock.patch.object(app, "fetch_json", return_value=payload) as m:
            rows = app.fetch_industry_flow_history("BK0436", days=5)
        # secid 应拼成 90.BKxxxx
        called_url = m.call_args[0][0]
        self.assertIn("secid=90.BK0436", called_url)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-08-12")
        self.assertAlmostEqual(rows[0]["main"], 0.56)  # 56000000 / 1e8
        self.assertAlmostEqual(rows[1]["main"], 0.89)

    def test_history_failure_returns_empty(self):
        with mock.patch.object(app, "fetch_json", side_effect=Exception("timeout")):
            rows = app.fetch_industry_flow_history("BK0436")
        self.assertEqual(rows, [])


class TestIndustryOverviewDegrade(unittest.TestCase):
    def setUp(self):
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, None
        self.tmpdir = tempfile.TemporaryDirectory()
        self.snap = os.path.join(self.tmpdir.name, "snap.json")
        self.patcher = mock.patch.object(app, "INDUSTRY_SNAPSHOT", self.snap)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    def test_success_writes_snapshot(self):
        boards = [{"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2}]
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=boards):
            out = app.industry_overview()
        self.assertFalse(out["stale"])
        self.assertEqual(out["boards"][0]["name"], "计算机")
        # 成功结果应写入磁盘快照
        self.assertTrue(os.path.exists(self.snap))
        with open(self.snap, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["boards"][0]["name"], "计算机")

    def test_failure_falls_back_to_snapshot_stale(self):
        # 先制造一份成功快照
        boards = [{"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2}]
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=boards):
            app.industry_overview()
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, None  # 清空内存缓存
        # 接口失败：应回读快照并标 stale
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=[]):
            out = app.industry_overview()
        self.assertTrue(out["stale"])
        self.assertEqual(out["boards"][0]["name"], "计算机")

    def test_failure_no_snapshot_returns_empty_stale(self):
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=[]):
            out = app.industry_overview()
        self.assertTrue(out["stale"])
        self.assertEqual(out["boards"], [])


class TestMarketOverviewCompat(unittest.TestCase):
    def setUp(self):
        app._MKT[0], app._MKT[1] = 0.0, None
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, None

    def _fake_quote(self, prefix, code, name=""):
        return {"code": code, "name": name, "chg": 1.0, "price": 100.0, "change": 1.0}

    def test_industry_source_keeps_compat_fields(self):
        boards = [{"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2}]
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview",
                               return_value={"boards": boards, "stale": False, "time": "2026-08-14 15:00"}):
            data = app.market_overview()
        self.assertEqual(data["source"], "industry_flow")
        self.assertFalse(data["stale"])
        sec = data["sectors"][0]
        # 向后兼容旧字段
        self.assertEqual(sec["code"], "BK0436")
        self.assertEqual(sec["name"], "计算机")
        self.assertEqual(sec["chg"], 2.5)
        # 新增资金流字段
        self.assertEqual(sec["main_net"], 8.9)
        self.assertEqual(sec["main_pct"], 5.2)

    def test_industry_source_orders_cards_by_main_net_inflow(self):
        boards = [
            {"code": "BK0475", "name": "银行", "chg": 3.0, "main_net": 1.0, "main_pct": 0.2},
            {"code": "BK0436", "name": "计算机", "chg": 1.0, "main_net": 8.9, "main_pct": 5.2},
        ]
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview",
                               return_value={"boards": boards, "stale": False, "time": "2026-08-14 15:00"}):
            data = app.market_overview()

        self.assertEqual([item["name"] for item in data["sectors"]], ["计算机", "银行"])

    def test_etf_fallback_when_industry_empty(self):
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview",
                               return_value={"boards": [], "stale": True, "time": "2026-08-14 15:00"}):
            data = app.market_overview()
        self.assertEqual(data["source"], "sector_etf_fallback")
        self.assertTrue(data["stale"])
        # 回退后仍有板块数据（来自旧板块ETF口径）
        self.assertTrue(len(data["sectors"]) > 0)


if __name__ == "__main__":
    unittest.main()
