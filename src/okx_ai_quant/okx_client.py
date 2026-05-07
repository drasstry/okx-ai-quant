from dataclasses import dataclass
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
        response = self.market_api.get_candlesticks(
            instId=symbol,
            bar=bar,
            limit=str(limit),
        )
        self._raise_for_error(response)
        return self._response_data(response, "candle")

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        response = self.market_api.get_ticker(instId=symbol)
        self._raise_for_error(response)
        return self._first_response_row(response, "ticker")

    def get_instruments(self, inst_type: str) -> list[dict[str, Any]]:
        if self.public_api is None:
            raise RuntimeError("OKX public data API is not configured")
        response = self.public_api.get_instruments(instType=inst_type)
        self._raise_for_error(response)
        rows = self._response_data(response, "instrument")
        return [row for row in rows if isinstance(row, dict)]

    def get_balance(self, currency: str | None = None) -> dict[str, Any]:
        if self.account_api is None:
            raise RuntimeError("OKX account API is not configured")
        kwargs = {"ccy": currency} if currency else {}
        response = self.account_api.get_account_balance(**kwargs)
        self._raise_for_error(response)
        return response

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
        response = self.account_api.get_positions(**kwargs)
        self._raise_for_error(response)
        return response

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
        response = self.trade_api.get_order(**kwargs)
        self._raise_for_error(response)
        return response

    def get_pending_orders(self, symbol: str | None = None) -> dict[str, Any]:
        if self.trade_api is None:
            raise RuntimeError("OKX trade API is not configured")
        kwargs = {}
        if symbol:
            kwargs["instId"] = symbol
        response = self.trade_api.get_order_list(**kwargs)
        self._raise_for_error(response)
        return response

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
        response = self.trade_api.cancel_order(**kwargs)
        self._raise_for_error(response)
        return response

    def sync_server_time(self) -> int:
        """Best-effort server-time sync hook for the bot loop.

        The python-okx SDK signs requests internally, so the adapter does not
        need to maintain a local time offset. Keeping this method lets the bot
        share the same orchestration shape as direct REST clients.
        """
        return 0

    @staticmethod
    def _raise_for_error(response: dict[str, Any]) -> None:
        code = response.get("code")
        if code != "0":
            msg = response.get("msg", "")
            raise RuntimeError(f"OKX API error {code}: {msg}")

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
