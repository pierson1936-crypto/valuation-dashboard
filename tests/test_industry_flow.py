# -*- coding: utf-8 -*-
"""行业板块资金流（同花顺主源、东财回退、快照）的离线单元测试。
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
        app._INDUSTRY_REFRESHING = False

    def test_parse_array_form(self):
        with mock.patch.object(app, "fetch_json", return_value=_boards_payload()) as fetch:
            boards = app.fetch_industry_boards_eastmoney()
        self.assertEqual(fetch.call_count, 2)
        urls = [call.args[0] for call in fetch.call_args_list]
        self.assertTrue(all("pz=50" in url and "fs=m:90+s:4" in url for url in urls))
        self.assertTrue(any("po=1" in url for url in urls))
        self.assertTrue(any("po=0" in url for url in urls))
        self.assertEqual(len(boards), 3)
        # 按主力净流入降序：计算机(8.9) > 银行(5.6) > 半导体(-2.3)
        self.assertEqual([b["name"] for b in boards], ["计算机", "银行", "半导体"])
        # 1e8 换算为亿元
        self.assertAlmostEqual(boards[0]["main_net"], 8.9)
        self.assertAlmostEqual(boards[1]["main_net"], 5.6)
        self.assertAlmostEqual(boards[2]["main_net"], -2.3)
        self.assertAlmostEqual(boards[0]["flow_net"], 8.9)
        self.assertEqual(boards[0]["flow_source"], "eastmoney")
        # 字段映射
        self.assertEqual(boards[0]["code"], "BK0436")
        self.assertEqual(boards[0]["chg"], 2.5)
        self.assertEqual(boards[0]["main_pct"], 5.2)

    def test_parse_dict_form(self):
        # 部分情况下 diff 是 {序号: 对象} 的字典，需兼容
        payload = {"data": {"diff": {
            "0": {"f12": "BK0475", "f14": "银行", "f3": 1.2, "f62": 5.6e8, "f184": 3.1},
            "1": {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
            "2": {"f12": "BK0737", "f14": "半导体", "f3": -0.8, "f62": -2.3e8, "f184": -1.5},
        }}}
        with mock.patch.object(app, "fetch_json", return_value=payload):
            boards = app.fetch_industry_boards_eastmoney()
        self.assertEqual(len(boards), 3)
        self.assertEqual(boards[0]["name"], "计算机")

    def test_dash_and_missing_fields(self):
        payload = {"data": {"diff": [
            {"f12": "BK0475", "f14": "银行", "f3": "-", "f62": "-", "f184": "-"},
            {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
            {"f12": "BK0737", "f14": "半导体", "f3": -0.8, "f62": -2.3e8, "f184": -1.5},
        ]}}
        with mock.patch.object(app, "fetch_json", return_value=payload):
            boards = app.fetch_industry_boards_eastmoney()
        self.assertEqual(len(boards), 3)
        bank = [b for b in boards if b["name"] == "银行"][0]
        self.assertIsNone(bank["chg"])
        self.assertIsNone(bank["main_net"])

    def test_failure_returns_empty(self):
        with mock.patch.object(app, "fetch_json", side_effect=Exception("502")):
            boards = app.fetch_industry_boards_eastmoney()
        self.assertEqual(boards, [])

    def test_one_sided_flow_is_rejected(self):
        payload = {"data": {"diff": [
            {"f12": "BK0475", "f14": "银行", "f3": 1.2, "f62": 5.6e8, "f184": 3.1},
            {"f12": "BK0436", "f14": "计算机", "f3": 2.5, "f62": 8.9e8, "f184": 5.2},
        ]}}
        with mock.patch.object(app, "fetch_json", return_value=payload):
            boards = app.fetch_industry_boards_eastmoney()
        self.assertEqual(boards, [])


class TestTHSIndustryRealtime(unittest.TestCase):
    @staticmethod
    def _page(rows, page="1/1"):
        body = "".join(
            "<tr><td>%s</td><td>%s</td><td>1000</td><td>%s</td>"
            "<td>10</td><td>8</td><td>%s</td><td>20</td>"
            "<td>样例</td><td>1%%</td><td>10</td></tr>" % row
            for row in rows
        )
        return ("<table><tr><th>序号</th><th>行业</th><th>行业指数</th><th>涨跌幅</th>"
                "<th>流入资金(亿)</th><th>流出资金(亿)</th><th>净额(亿)</th></tr>"
                + body + "</table><span class=\"page_info\">" + page + "</span>")

    def test_parse_keeps_ths_net_separate_from_main_net(self):
        boards = app._parse_ths_industry_boards(self._page([
            ("1", "半导体", "3.86%", "8.90"),
            ("2", "银行", "-1.20%", "-2.30"),
        ]))

        self.assertEqual([item["name"] for item in boards], ["半导体", "银行"])
        self.assertEqual(boards[0]["flow_net"], 8.9)
        self.assertIsNone(boards[0]["main_net"])
        self.assertEqual(boards[0]["flow_kind"], "net_amount")
        self.assertEqual(boards[0]["flow_source"], "ths")

    def test_fetch_merges_all_pages_and_requires_both_sides(self):
        pages = [
            self._page([("1", "半导体", "3.86%", "8.90")], "1/2"),
            self._page([("2", "银行", "-1.20%", "-2.30")], "2/2"),
        ]
        with mock.patch.object(app, "_fetch_ths_industry_page", side_effect=pages) as fetch:
            boards = app.fetch_industry_boards_ths()

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([item["name"] for item in boards], ["半导体", "银行"])

    def test_provider_chain_prefers_ths_then_falls_back_to_eastmoney(self):
        ths = [
            {"code": "THS:半导体", "name": "半导体", "chg": 1.0,
             "main_net": None, "flow_net": 8.9, "flow_source": "ths"},
            {"code": "THS:银行", "name": "银行", "chg": -1.0,
             "main_net": None, "flow_net": -2.3, "flow_source": "ths"},
        ]
        with mock.patch.object(app, "fetch_industry_boards_ths", return_value=ths), \
             mock.patch.object(app, "fetch_industry_boards_eastmoney") as eastmoney:
            self.assertEqual(app.fetch_industry_boards_realtime(), ths)
        eastmoney.assert_not_called()

        eastmoney_boards = _boards_payload()["data"]["diff"]
        with mock.patch.object(app, "fetch_industry_boards_ths", return_value=[]), \
             mock.patch.object(app, "fetch_industry_boards_eastmoney",
                               return_value=eastmoney_boards) as eastmoney:
            self.assertEqual(app.fetch_industry_boards_realtime(), eastmoney_boards)
        eastmoney.assert_called_once_with()


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
        app._INDUSTRY_REFRESHING = False
        self.tmpdir = tempfile.TemporaryDirectory()
        self.snap = os.path.join(self.tmpdir.name, "snap.json")
        self.patcher = mock.patch.object(app, "INDUSTRY_SNAPSHOT", self.snap)
        self.patcher.start()

    def tearDown(self):
        app._INDUSTRY_REFRESHING = False
        self.patcher.stop()
        self.tmpdir.cleanup()

    def test_success_writes_snapshot(self):
        boards = [
            {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
            {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
        ]
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=boards):
            out = app.industry_overview()
        self.assertFalse(out["stale"])
        self.assertTrue(out["flow_complete"])
        self.assertEqual(out["boards"][0]["name"], "计算机")
        # 成功结果应写入磁盘快照
        self.assertTrue(os.path.exists(self.snap))
        with open(self.snap, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["boards"][0]["name"], "计算机")
        self.assertEqual(saved["source"], "eastmoney")

    def test_failure_falls_back_to_snapshot_stale(self):
        # 先制造一份成功快照
        boards = [
            {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
            {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
        ]
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
        self.assertFalse(out["flow_complete"])
        self.assertEqual(out["boards"], [])

    def test_background_mode_returns_snapshot_before_network(self):
        saved = {
            "boards": [
                {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
                {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
            ],
            "stale": False,
            "flow_complete": True,
            "time": "2026-08-14 15:00",
        }
        with open(self.snap, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False)

        def mark_refreshing(force=False):
            app._INDUSTRY_REFRESHING = True
            return True

        with mock.patch.object(app, "_start_industry_refresh", side_effect=mark_refreshing) as start:
            out = app.industry_overview(background=True)

        start.assert_called_once_with(force=False)
        self.assertTrue(out["stale"])
        self.assertTrue(out["flow_complete"])
        self.assertTrue(out["refreshing"])
        self.assertEqual(out["time"], "2026-08-14 15:00")

    def test_failed_background_refresh_keeps_complete_snapshot(self):
        boards = [
            {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
            {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
        ]
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, {
            "boards": boards, "stale": False, "flow_complete": True,
            "refreshing": True, "time": "2026-08-14 15:00",
        }
        app._INDUSTRY_REFRESHING = True
        with mock.patch.object(app, "fetch_industry_boards_realtime", return_value=[]):
            app._refresh_industry_cache()

        out = app._INDUSTRY[1]
        self.assertTrue(out["stale"])
        self.assertTrue(out["flow_complete"])
        self.assertFalse(out["refreshing"])
        self.assertEqual(out["boards"], boards)
        self.assertEqual(out["time"], "2026-08-14 15:00")


class TestMarketOverviewCompat(unittest.TestCase):
    def setUp(self):
        app._MKT[0], app._MKT[1] = 0.0, None
        app._INDUSTRY[0], app._INDUSTRY[1] = 0.0, None
        app._INDUSTRY_REFRESHING = False

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
            {"code": "BK0737", "name": "半导体", "chg": 4.0, "main_net": -2.3, "main_pct": -1.5},
        ]
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview",
                               return_value={"boards": boards, "stale": False, "flow_complete": True,
                                             "time": "2026-08-14 15:00"}):
            data = app.market_overview()

        self.assertEqual([item["name"] for item in data["sectors"]], ["计算机", "银行", "半导体"])

    def test_ths_source_uses_generic_flow_net_without_changing_main_net(self):
        boards = [
            {"code": "THS:半导体", "name": "半导体", "chg": 3.0,
             "main_net": None, "main_pct": None, "flow_net": 8.9,
             "flow_kind": "net_amount", "flow_source": "ths"},
            {"code": "THS:银行", "name": "银行", "chg": -1.0,
             "main_net": None, "main_pct": None, "flow_net": -2.3,
             "flow_kind": "net_amount", "flow_source": "ths"},
        ]
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview", return_value={
                 "boards": boards, "stale": False, "flow_complete": True,
                 "source": "ths", "time": "2026-08-17 15:00",
             }):
            data = app.market_overview()

        self.assertEqual(data["flow_source"], "ths")
        self.assertEqual(data["flow_label"], "同花顺行业资金净额")
        self.assertEqual(data["sectors"][0]["flow_net"], 8.9)
        self.assertIsNone(data["sectors"][0]["main_net"])

    def test_force_bypasses_market_cache_and_starts_industry_refresh(self):
        app._MKT[0], app._MKT[1] = app.time.time(), {"marker": "old"}
        boards = [
            {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
            {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
        ]
        with mock.patch.object(app, "fetch_quote", side_effect=self._fake_quote), \
             mock.patch.object(app, "industry_overview", return_value={
                 "boards": boards, "stale": False, "flow_complete": True,
                 "refreshing": True, "time": "2026-08-14 15:00",
             }) as industry:
            data = app.market_overview(force=True)

        self.assertNotIn("marker", data)
        self.assertTrue(data["refreshing"])
        industry.assert_called_once_with(force=True, background=True)

    def test_poll_reuses_cached_indices(self):
        indices = [{"code": "000001", "name": "上证指数", "chg": 1.0, "price": 3500.0}]
        app._MKT[0], app._MKT[1] = app.time.time(), {"indices": indices}
        boards = [
            {"code": "BK0436", "name": "计算机", "chg": 2.5, "main_net": 8.9, "main_pct": 5.2},
            {"code": "BK0737", "name": "半导体", "chg": -0.8, "main_net": -2.3, "main_pct": -1.5},
        ]
        with mock.patch.object(app, "fetch_quote", side_effect=AssertionError("不应重复请求指数")), \
             mock.patch.object(app, "industry_overview", return_value={
                 "boards": boards, "stale": False, "flow_complete": True,
                 "refreshing": False, "time": "2026-08-14 15:00",
             }):
            data = app.market_overview(poll=True)

        self.assertEqual(data["indices"], indices)

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
