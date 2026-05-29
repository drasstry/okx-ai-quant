from dataclasses import dataclass
from time import sleep
from typing import Any

from okx_ai_quant.config import Settings


@dataclass(kw_only=True)
class OkxClient:
    settings: Settings
    flag: str
    market_api: Any
    public_api: Any | None = None
    trade_api: Any | None = None
    account_api: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "OkxClient":
        import okx.Account as Account
        import okx.MarketData as MarketData
        import okx.PublicData as PublicData
        import okx.Trade as Trade

        flag = settings.okx_flag
        credentials = settings.okx_credentials
        api_kwargs = {
            "api_key": credentials.api_key,
            "api_secret_key": credentials.api_secret,
            "passphrase": credentials.passphrase,
            "flag": flag,
        }
        return cls(
            settings=settings,
            flag=flag,
            market_api=MarketData.MarketAPI(**api_kwargs),
            public_api=PublicData.PublicAPI(**api_kwargs),
            trade_api=Trade.TradeAPI(**api_kwargs),
            account_api=Account.AccountAPI(**api_kwargs),
        )

    def get_candles(self, symbol: str, bar: str, limit: int) -> list[Any]:
        response = self._call_with_retries(
            "get_candles",
            lambda: self.market_api.get_candlesticks(
                instId=symbol,
                bar=bar,
                limit=str(limit),
            ),
        )
        return self._response_data(response, "candle")

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        response = self._call_with_retries(
            "get_ticker",
            lambda: self.market_api.get_ticker(instId=symbol),
        )
        return self._first_response_row(response, "ticker")

    def get_instruments(self, inst_type: str) -> list[dict[str, Any]]:
        if self.public_api is None:
            raise RuntimeError("OKX public data API is not configured")
        response = self._call_with_retries(
            "get_instruments",
            lambda: self.public_api.get_instruments(instType=inst_type),
        )
        rows = self._response_data(response, "instrument")
        return [row for row in rows if isinstance(row, dict)]

    def get_funding_rate(self, symbol: str) -> float:
        if self.public_api is None:
            raise RuntimeError("OKX public data API is not configured")
        response = self._call_with_retries(
            "get_funding_rate",
            lambda: self.public_api.get_funding_rate(instId=symbol),
        )
        row = self._first_response_row(response, "funding rate")
        try:
            return float(row.get("fundingRate") or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"OKX API response has invalid fundingRate for {symbol}") from exc

    def get_balance(self, currency: str | None = None) -> dict[str, Any]:
        if self.account_api is None:
            raise RuntimeError("OKX account API is not configured")
        kwargs = {"ccy": currency} if currency else {}
        return self._call_with_retries(
            "get_balance",
            lambda: self.account_api.get_account_balance(**kwargs),
        )

    def get_positions(
        self,
        *,
        inst_type: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        if self.account_api is None:
            raise RuntimeError("OKX account API is not configured")
        kwargs = {}
        if inst_type:
            kwargs["instType"] = inst_type
        if symbol:
            kwargs["instId"] = symbol
        return self._call_with_retries(
            "get_positions",
            lambda: self.account_api.get_positions(**kwargs),
        )

    def get_order(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if self.trade_api is None:
            raise RuntimeError("OKX trade API is not configured")
        kwargs = {"instId": symbol}
        if order_id:
            kwargs["ordId"] = order_id
        if client_order_id:
            kwargs["clOrdId"] = client_order_id
        return self._call_with_retries(
            "get_order",
            lambda: self.trade_api.get_order(**kwargs),
        )

    def get_pending_orders(self, symbol: str | None = None) -> dict[str, Any]:
        if self.trade_api is None:
            raise RuntimeError("OKX trade API is not configured")
        kwargs = {}
        if symbol:
            kwargs["instId"] = symbol
        return self._call_with_retries(
            "get_pending_orders",
            lambda: self.trade_api.get_order_list(**kwargs),
        )

    def set_leverage(
        self,
        symbol: str,
        *,
        leverage: int,
        margin_mode: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        """Set OKX account leverage for a futures/swap instrument before entry.

        OKX keeps leverage as account-side instrument state. Passing
        ``MAX_LEVERAGE`` only to the local risk model is not enough; it must be
        written through ``/api/v5/account/set-leverage`` before placing orders.
        """
        if self.account_api is None:
            raise RuntimeError("OKX account API is not configured")
        kwargs = {
            "instId": symbol,
            "lever": str(leverage),
            "mgnMode": margin_mode,
        }
        if position_side:
            kwargs["posSide"] = position_side
        return self._call_with_retries(
            "set_leverage",
            lambda: self.account_api.set_leverage(**kwargs),
        )

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if self.trade_api is None:
            raise RuntimeError("OKX trade API is not configured")
        kwargs = {"instId": symbol}
        if order_id:
            kwargs["ordId"] = order_id
        if client_order_id:
            kwargs["clOrdId"] = client_order_id
        return self._call_with_retries(
            "cancel_order",
            lambda: self.trade_api.cancel_order(**kwargs),
        )

    def place_order(self, **kwargs) -> dict[str, Any]:
        if self.trade_api is None:
            raise RuntimeError("OKX trade API is not configured")
        return self._call_with_retries(
            "place_order",
            lambda: self.trade_api.place_order(**kwargs),
        )

    def sync_server_time(self) -> int:
        """Best-effort server-time sync hook for the bot loop.

        The python-okx SDK signs requests internally, so the adapter does not
        need to maintain a local time offset. Keeping this method lets the bot
        share the same orchestration shape as direct REST clients.
        """
        return 0

    def check_connectivity(
        self,
        *,
        attempts: int = 5,
        required_successes: int = 5,
        symbol: str = "BTC-USDT-SWAP",
    ) -> tuple[bool, str]:
        attempts = max(1, attempts)
        required_successes = min(max(1, required_successes), attempts)
        successes = 0
        errors: list[str] = []
        for _ in range(attempts):
            try:
                response = self.market_api.get_ticker(instId=symbol)
                self._raise_for_error(response)
                successes += 1
            except Exception as exc:  # noqa: BLE001 - network boundary.
                errors.append(_compact_error(exc))

        if successes >= required_successes:
            return True, f"OKX connectivity healthy ({successes}/{attempts})"

        sample = "; ".join(errors[-2:]) if errors else "no successful OKX ticker response"
        return False, f"OKX connectivity unhealthy ({successes}/{attempts}): {sample}"

    @staticmethod
    def _raise_for_error(response: dict[str, Any]) -> None:
        code = response.get("code")
        if code != "0":
            msg = response.get("msg", "")
            raise RuntimeError(f"OKX API error {code}: {msg}")

    def _call_with_retries(
        self,
        operation: str,
        callback,
        *,
        attempts: int = 3,
        base_delay_seconds: float = 1.0,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        delay = base_delay_seconds
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = callback()
                self._raise_for_error(response)
                return response
            except Exception as exc:  # noqa: BLE001 - OKX/network boundary is intentionally broad.
                last_error = exc
                if attempt >= attempts or not _is_transient_okx_error(exc):
                    raise
                sleep(delay)
                delay *= 2
        raise RuntimeError(f"OKX {operation} failed after retries: {last_error}")

    @staticmethod
    def _response_data(response: dict[str, Any], data_name: str) -> list[Any]:
        data = response.get("data")
        if not isinstance(data, list):
            raise RuntimeError(f"OKX API response missing {data_name} data")
        return data

    @classmethod
    def _first_response_row(cls, response: dict[str, Any], data_name: str) -> dict[str, Any]:
        data = cls._response_data(response, data_name)
        if not data:
            raise RuntimeError(f"OKX API response missing {data_name} data")
        row = data[0]
        if not isinstance(row, dict):
            raise RuntimeError(f"OKX API response missing {data_name} data")
        return row


def _is_transient_okx_error(exc: Exception) -> bool:
    message = str(exc).lower()
    transient_markers = (
        "okx api error 1",
        "okx api error 50013",
        "all operations failed",
        "systems are busy",
        "connection",
        "connecterror",
        "eof occurred",
        "handshake operation timed out",
        "remote protocol error",
        "ssl",
        "temporarily",
        "timed out",
        "timeout",
        "too many requests",
    )
    return isinstance(exc, (TimeoutError, OSError)) or any(
        marker in message for marker in transient_markers
    )


def _compact_error(exc: Exception, *, limit: int = 180) -> str:
    message = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return message[:limit]
