from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from monitoring.config import MonitorConfig


@dataclass
class NotificationResult:
    channel: str
    success: bool
    error: str = ""


class ConsoleNotifier:
    channel = "console"

    def send(self, title: str, content: str) -> NotificationResult:
        print("\n%s\n%s\n" % (title, content))
        return NotificationResult(self.channel, True)


class WeComWebhookNotifier:
    channel = "wecom"

    def __init__(self, webhook_url: str, timeout: int = 10):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, title: str, content: str) -> NotificationResult:
        body = {
            "msgtype": "markdown",
            "markdown": {"content": "**%s**\n\n%s" % (title, content)},
        }
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("errcode") not in (None, 0):
                return NotificationResult(
                    self.channel, False, "企业微信返回错误码 %s" % payload.get("errcode")
                )
            return NotificationResult(self.channel, True)
        except urllib.error.HTTPError as exc:
            return NotificationResult(self.channel, False, "HTTP %s" % exc.code)
        except Exception as exc:
            return NotificationResult(self.channel, False, type(exc).__name__)


class ServerChanNotifier:
    channel = "serverchan"

    def __init__(self, sendkey: str, timeout: int = 10):
        self.sendkey = sendkey
        self.timeout = timeout

    def send(self, title: str, content: str) -> NotificationResult:
        url = "https://sctapi.ftqq.com/%s.send" % urllib.parse.quote(
            self.sendkey, safe=""
        )
        data = urllib.parse.urlencode({"title": title, "desp": content}).encode(
            "utf-8"
        )
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            code = payload.get("code")
            if code not in (None, 0):
                return NotificationResult(
                    self.channel, False, "Server酱返回错误码 %s" % code
                )
            return NotificationResult(self.channel, True)
        except urllib.error.HTTPError as exc:
            return NotificationResult(self.channel, False, "HTTP %s" % exc.code)
        except Exception as exc:
            return NotificationResult(self.channel, False, type(exc).__name__)


class CompositeNotifier:
    def __init__(self, notifiers: list[object]):
        self.notifiers = notifiers

    def send(self, title: str, content: str) -> list[NotificationResult]:
        return [notifier.send(title, content) for notifier in self.notifiers]

    def send_to_channel(
        self, channel: str, title: str, content: str
    ) -> NotificationResult:
        notifier = next(
            (
                item
                for item in self.notifiers
                if getattr(item, "channel", "") == channel
            ),
            None,
        )
        if notifier is None:
            return NotificationResult(channel, False, "通知渠道未配置")
        return notifier.send(title, content)


def build_notifier(config: MonitorConfig) -> CompositeNotifier:
    notifiers: list[object] = [ConsoleNotifier()]
    if config.wecom_webhook_url:
        notifiers.append(
            WeComWebhookNotifier(
                config.wecom_webhook_url, config.notification_timeout_seconds
            )
        )
    if config.serverchan_sendkey:
        notifiers.append(
            ServerChanNotifier(
                config.serverchan_sendkey, config.notification_timeout_seconds
            )
        )
    return CompositeNotifier(notifiers)
