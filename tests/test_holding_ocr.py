import base64
import json
import struct
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import error as urlerror

from monitoring.db import MonitorRepository
from monitoring.holding_ocr import (
    HoldingOCRAssistant,
    _normalize_model_payload,
    describe_chat_screenshot,
)


BASE_URL = (
    "https://workspace-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)


def _png_data_url(width=120, height=240, shade=40, mime="image/png"):
    def chunk(kind, data):
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((shade, shade, shade)) * width
    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
    return f"data:{mime};base64," + base64.b64encode(image).decode("ascii")


def _model_response(payload, tokens=321):
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"total_tokens": tokens},
    }


class HoldingOCRAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = MonitorRepository(Path(self.temp_dir.name) / "monitor.db")
        self.repository.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_chat_screenshot_returns_a_free_form_observation(self):
        response = {
            "choices": [{"message": {"content": "可见信息：PE 为 18.2。\n无法确认：图片未显示样本期。"}}]
        }

        result = describe_chat_screenshot(
            _png_data_url(), "test-only-key", BASE_URL,
            api_post=lambda *args, **kwargs: response,
        )

        self.assertIn("PE 为 18.2", result["observation"])
        self.assertIn("图片未显示样本期", result["observation"])
        self.assertEqual(result["model"], "qwen3.7-flash")

    def test_recognize_uses_official_format_and_cache_without_storing_image_or_key(self):
        calls = []
        payload = {
            "holdings": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "quantity": 100,
                    "cost_price": 1188.5,
                    "needs_review": False,
                }
            ],
            "needs_review": ["第二行成本价模糊"],
        }

        def fake_api_post(url, headers, body, timeout, retries):
            calls.append((url, headers, body, timeout, retries))
            return _model_response(payload)

        now = datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)
        image_data_url = _png_data_url()
        assistant = HoldingOCRAssistant(self.repository, api_post=fake_api_post)

        result = assistant.recognize(
            image_data_url, "test-only-key", BASE_URL, now=now
        )

        self.assertFalse(result["cached"])
        self.assertEqual(result["model"], "qwen3.7-flash")
        self.assertEqual(result["token_usage"], 321)
        self.assertEqual(result["holdings"][0]["quantity"], 100)
        self.assertEqual(len(calls), 1)
        url, headers, body, timeout, retries = calls[0]
        self.assertEqual(url, BASE_URL + "/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-only-key")
        self.assertEqual(body["model"], "qwen3.7-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(
            body["messages"][0]["content"][0]["image_url"]["url"],
            image_data_url,
        )
        self.assertEqual(timeout, 120)
        self.assertEqual(retries, 2)

        with self.repository.connect() as conn:
            row = dict(conn.execute("SELECT * FROM holding_ocr_cache").fetchone())
        persisted = json.dumps(row, ensure_ascii=False)
        self.assertNotIn(image_data_url, persisted)
        self.assertNotIn("test-only-key", persisted)
        self.assertRegex(row["cache_key"], r"^[0-9a-f]{64}$")

        cached = assistant.recognize(image_data_url, "", "", now + timedelta(days=6))
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["token_usage"], 0)
        self.assertEqual(cached["holdings"], result["holdings"])
        self.assertEqual(len(calls), 1)

        with self.assertRaisesRegex(ValueError, "没有可用缓存"):
            assistant.recognize(image_data_url, "", "", now + timedelta(days=7))
        cleanup = self.repository.cleanup(7, 365, now + timedelta(days=7))
        self.assertEqual(cleanup["holding_ocr_cache"], 1)
        self.assertEqual(self.repository.count_rows("holding_ocr_cache"), 0)

    def test_cache_key_changes_with_image_and_model(self):
        payload = {"holdings": [], "needs_review": ["未识别到清晰持仓"]}

        def fake_api_post(*args, **kwargs):
            return _model_response(payload, tokens=0)

        first = HoldingOCRAssistant(self.repository, fake_api_post, model="qwen3.7-flash")
        second = HoldingOCRAssistant(self.repository, fake_api_post, model="qwen-test")
        first.recognize(_png_data_url(shade=20), "key", BASE_URL)
        first.recognize(_png_data_url(shade=30), "key", BASE_URL)
        second.recognize(_png_data_url(shade=20), "key", BASE_URL)

        self.assertEqual(self.repository.count_rows("holding_ocr_cache"), 3)

    def test_image_and_base_url_validation_happen_before_network(self):
        calls = []

        def fake_api_post(*args, **kwargs):
            calls.append(True)
            return _model_response({"holdings": [], "needs_review": []})

        assistant = HoldingOCRAssistant(self.repository, fake_api_post)
        cases = [
            (
                _png_data_url(mime="image/jpeg"),
                BASE_URL,
                "JPEG 类型不一致",
            ),
            (_png_data_url(width=10), BASE_URL, "宽高必须大于 10"),
            (_png_data_url(width=11, height=2300), BASE_URL, "宽高比不能超过"),
            (
                _png_data_url(),
                "http://workspace-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "阿里云 HTTPS",
            ),
            (
                _png_data_url(),
                "https://example.com/compatible-mode/v1",
                "阿里云 HTTPS",
            ),
        ]
        for image, base_url, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    assistant.recognize(image, "key", base_url)
        self.assertEqual(calls, [])

    def test_model_json_is_strictly_validated(self):
        valid = {
            "holdings": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "quantity": 0,
                    "cost_price": 0,
                    "needs_review": False,
                }
            ],
            "needs_review": [],
        }
        normalized = _normalize_model_payload(valid)
        self.assertEqual(normalized["holdings"][0]["quantity"], 0)
        self.assertFalse(normalized["holdings"][0]["needs_review"])

        incomplete = _normalize_model_payload(
            {
                "holdings": [
                    {
                        "code": "300750",
                        "name": "",
                        "quantity": None,
                        "cost_price": 180,
                        "needs_review": False,
                    }
                ],
                "needs_review": [],
            }
        )
        self.assertIsNone(incomplete["holdings"][0]["quantity"])
        self.assertTrue(incomplete["holdings"][0]["needs_review"])

        cases = [
            {**valid, "extra": True},
            {
                "holdings": valid["holdings"] * 2,
                "needs_review": [],
            },
            {
                "holdings": [{**valid["holdings"][0], "quantity": "100"}],
                "needs_review": [],
            },
            {
                "holdings": [{**valid["holdings"][0], "cost_price": -1}],
                "needs_review": [],
            },
            {
                "holdings": [
                    {
                        "code": f"{index:06d}",
                        "name": "样例",
                        "quantity": 1,
                        "cost_price": 1,
                        "needs_review": False,
                    }
                    for index in range(31)
                ],
                "needs_review": [],
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    _normalize_model_payload(payload)

    def test_upstream_errors_are_sanitized(self):
        image = _png_data_url()

        def unauthorized(url, *args, **kwargs):
            raise urlerror.HTTPError(url, 401, "secret-key-leaked", {}, None)

        assistant = HoldingOCRAssistant(self.repository, unauthorized)
        with self.assertRaisesRegex(ValueError, "鉴权失败") as captured:
            assistant.recognize(image, "hidden-key", BASE_URL)
        self.assertNotIn("hidden-key", str(captured.exception))
        self.assertNotIn("secret-key-leaked", str(captured.exception))

        response_error = HoldingOCRAssistant(
            self.repository,
            lambda *args, **kwargs: {
                "error": {"code": "DataInspectionFailed", "message": "secret"}
            },
        )
        with self.assertRaisesRegex(ValueError, "内容安全检查") as captured:
            response_error.recognize(_png_data_url(shade=80), "hidden-key", BASE_URL)
        self.assertNotIn("secret", str(captured.exception))

    def test_response_rejects_duplicate_json_keys_and_nonstandard_numbers(self):
        responses = [
            '{"holdings":[],"holdings":[],"needs_review":[]}',
            '{"holdings":[{"code":"600519","name":"样例",'
            '"quantity":NaN,"cost_price":1,"needs_review":false}],'
            '"needs_review":[]}',
        ]
        for index, content in enumerate(responses, 1):
            assistant = HoldingOCRAssistant(
                self.repository,
                lambda *args, content=content, **kwargs: {
                    "choices": [{"message": {"content": content}}]
                },
            )
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, "严格 JSON"):
                    assistant.recognize(
                        _png_data_url(shade=100 + index), "key", BASE_URL
                    )


if __name__ == "__main__":
    unittest.main()
