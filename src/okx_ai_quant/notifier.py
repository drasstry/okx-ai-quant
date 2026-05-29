import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

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
    def __init__(self, *, bot_token: str, chat_id: str, proxy_url: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy_url = proxy_url.strip()

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
            with _open_telegram_request(request, timeout=20, proxy_url=self.proxy_url) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, socket.timeout, OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise NotificationError(f"Telegram send failed: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NotificationError(f"Telegram send returned invalid JSON: {exc}") from exc

        if not raw.get("ok"):
            raise NotificationError(f"Telegram send failed: {raw}")


@dataclass(frozen=True, kw_only=True)
class TelegramUpdate:
    update_id: int
    chat_id: str
    text: str


class TelegramBotClient:
    def __init__(self, *, bot_token: str, chat_id: str, proxy_url: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy_url = proxy_url.strip()

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[TelegramUpdate]:
        if not self.bot_token or not self.chat_id:
            raise NotificationError("Telegram polling requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

        params = {"timeout": str(timeout), "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = str(offset)
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method="GET")
        try:
            with _open_telegram_request(
                request,
                timeout=timeout + 5,
                proxy_url=self.proxy_url,
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, socket.timeout, OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise NotificationError(f"Telegram polling failed: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NotificationError(f"Telegram polling returned invalid JSON: {exc}") from exc

        if not raw.get("ok"):
            raise NotificationError(f"Telegram polling failed: {raw}")

        updates: list[TelegramUpdate] = []
        for row in raw.get("result") or []:
            message = row.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            text = str(message.get("text") or "").strip()
            update_id = row.get("update_id")
            if chat_id != str(self.chat_id) or not text or not isinstance(update_id, int):
                continue
            updates.append(TelegramUpdate(update_id=update_id, chat_id=chat_id, text=text))
        return updates

    def send(self, message: str) -> None:
        TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            proxy_url=self.proxy_url,
        ).send(message)


def build_notifier(settings: Settings) -> Notifier:
    name = settings.NOTIFIER.strip().lower()
    if name == "telegram":
        return TelegramNotifier(
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            proxy_url=settings.TELEGRAM_PROXY_URL,
        )
    if name == "none":
        return NullNotifier()
    return ConsoleNotifier()


def _open_telegram_request(request: urllib.request.Request, *, timeout: int, proxy_url: str):
    if not proxy_url:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    ).open(request, timeout=timeout)
