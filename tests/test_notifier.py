import urllib.error
from contextlib import contextmanager

import pytest

from okx_ai_quant.notifier import NotificationError, TelegramBotClient, TelegramNotifier


def test_telegram_send_wraps_timeout_errors(monkeypatch):
    def raise_timeout(_request, timeout):
        raise TimeoutError("read timed out")

    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")

    with pytest.raises(NotificationError, match="Telegram send failed"):
        notifier.send("hello")


def test_telegram_polling_wraps_url_errors(monkeypatch):
    def raise_url_error(_request, timeout):
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)
    client = TelegramBotClient(bot_token="token", chat_id="chat")

    with pytest.raises(NotificationError, match="Telegram polling failed"):
        client.get_updates()


def test_telegram_notifier_can_use_explicit_proxy(monkeypatch):
    captured = {}

    @contextmanager
    def fake_open(_request, timeout):
        captured["timeout"] = timeout

        class Response:
            def read(self):
                return b'{"ok": true}'

        yield Response()

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            return fake_open(request, timeout)

    def build_opener(handler):
        captured["handler"] = handler
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    notifier = TelegramNotifier(
        bot_token="token",
        chat_id="chat",
        proxy_url="http://172.20.160.1:7897",
    )

    notifier.send("hello")

    assert captured["url"] == "https://api.telegram.org/bottoken/sendMessage"
    assert captured["timeout"] == 20
