import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from okx_ai_quant.config import Settings


class NotificationError(RuntimeError):
    pass


class Notifier(ABC):
    @abstractmethod
    def send(self, message: str) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, message: str) -> None:
        print(message)


class NullNotifier(Notifier):
    def send(self, message: str) -> None:
        return


class TelegramNotifier(Notifier):
    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        if not self.bot_token or not self.chat_id:
            raise NotificationError("Telegram notifier requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": message[:3900],
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise NotificationError(f"Telegram send failed: {exc}") from exc

        if not raw.get("ok"):
            raise NotificationError(f"Telegram send failed: {raw}")


def build_notifier(settings: Settings) -> Notifier:
    name = settings.NOTIFIER.strip().lower()
    if name == "telegram":
        return TelegramNotifier(
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
        )
    if name == "none":
        return NullNotifier()
    return ConsoleNotifier()
