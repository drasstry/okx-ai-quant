import sys
import types
from datetime import UTC, datetime

import pytest

from okx_ai_quant.config import Settings, TradingMode
from okx_ai_quant.market_data import normalize_candles, normalize_ticker
from okx_ai_quant.okx_client import OkxClient


class FakeMarketApi:
    def __init__(self):
        self.calls = []
        self.candles_response = {"code": "0", "data": [["newer"], ["older"]]}
        self.ticker_response = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]}

    def get_candlesticks(self, **kwargs):
        self.calls.append(("get_candlesticks", kwargs))
        return self.candles_response

    def get_ticker(self, **kwargs):
        self.calls.append(("get_ticker", kwargs))
        return self.ticker_response


class FakePublicApi:
    def __init__(self):
        self.calls = []
        self.instruments_response = {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "state": "live"}],
        }

    def get_instruments(self, **kwargs):
        self.calls.append(("get_instruments", kwargs))
        return self.instruments_response


class FakeAccountApi:
    def __init__(self):
        self.calls = []
        self.balance_response = {"code": "0", "data": []}
        self.positions_response = {
            "code": "0",
            "data": [{"instId": "BTC-USDT-SWAP", "pos": "1", "avgPx": "100"}],
        }

    def get_account_balance(self, **kwargs):
        self.calls.append(("get_account_balance", kwargs))
        return self.balance_response

    def get_positions(self, **kwargs):
        self.calls.append(("get_positions", kwargs))
        return self.positions_response


def test_client_uses_selected_demo_credentials_and_flag(monkeypatch):
    created = {}

    class FakeApi:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeMarketApiWithCapture(FakeApi):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["market"] = self

    fake_okx = types.ModuleType("okx")
    fake_account = types.ModuleType("okx.Account")
    fake_market = types.ModuleType("okx.MarketData")
    fake_public = types.ModuleType("okx.PublicData")
    fake_trade = types.ModuleType("okx.Trade")
    fake_account.AccountAPI = FakeApi
    fake_market.MarketAPI = FakeMarketApiWithCapture
    fake_public.PublicAPI = FakeApi
    fake_trade.TradeAPI = FakeApi
    fake_okx.Account = fake_account
    fake_okx.MarketData = fake_market
    fake_okx.PublicData = fake_public
    fake_okx.Trade = fake_trade

    monkeypatch.setitem(sys.modules, "okx", fake_okx)
    monkeypatch.setitem(sys.modules, "okx.Account", fake_account)
    monkeypatch.setitem(sys.modules, "okx.MarketData", fake_market)
    monkeypatch.setitem(sys.modules, "okx.PublicData", fake_public)
    monkeypatch.setitem(sys.modules, "okx.Trade", fake_trade)

    settings = Settings(
        _env_file=None,
        TRADING_MODE=TradingMode.DEMO,
        OKX_DEMO_API_KEY="demo-key",
        OKX_DEMO_API_SECRET="demo-secret",
        OKX_DEMO_API_PASSPHRASE="demo-passphrase",
    )
    client = OkxClient.from_settings(settings)

    assert client.settings is settings
    assert client.flag == "1"
    assert created["market"].kwargs["flag"] == "1"
    assert created["market"].kwargs["api_key"] == "demo-key"
    assert created["market"].kwargs["api_secret_key"] == "demo-secret"
    assert created["market"].kwargs["passphrase"] == "demo-passphrase"


def test_get_candles_passes_okx_params_and_returns_data():
    market_api = FakeMarketApi()
    client = OkxClient(settings=Settings(), flag="1", market_api=market_api)

    data = client.get_candles("BTC-USDT-SWAP", "1m", 100)

    assert data == [["newer"], ["older"]]
    assert market_api.calls == [
        ("get_candlesticks", {"instId": "BTC-USDT-SWAP", "bar": "1m", "limit": "100"})
    ]


def test_get_ticker_returns_first_row():
    market_api = FakeMarketApi()
    market_api.ticker_response = {
        "code": "0",
        "data": [{"instId": "BTC-USDT-SWAP", "last": "101.5"}, {"instId": "ETH-USDT-SWAP"}],
    }
    client = OkxClient(settings=Settings(), flag="1", market_api=market_api)

    row = client.get_ticker("BTC-USDT-SWAP")

    assert row == {"instId": "BTC-USDT-SWAP", "last": "101.5"}
    assert market_api.calls == [("get_ticker", {"instId": "BTC-USDT-SWAP"})]


def test_get_instruments_returns_public_instrument_rows():
    public_api = FakePublicApi()
    client = OkxClient(settings=Settings(), flag="1", market_api=FakeMarketApi(), public_api=public_api)

    rows = client.get_instruments("SWAP")

    assert rows == [{"instId": "BTC-USDT-SWAP", "state": "live"}]
    assert public_api.calls == [("get_instruments", {"instType": "SWAP"})]


def test_get_positions_passes_inst_type_and_symbol():
    account_api = FakeAccountApi()
    client = OkxClient(
        settings=Settings(),
        flag="1",
        market_api=FakeMarketApi(),
        account_api=account_api,
    )

    response = client.get_positions(inst_type="SWAP", symbol="BTC-USDT-SWAP")

    assert response == account_api.positions_response
    assert account_api.calls == [
        ("get_positions", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
    ]


@pytest.mark.parametrize("response", [{"code": "0"}, {"code": "0", "data": None}])
def test_get_candles_malformed_success_response_raises_runtime_error(response):
    market_api = FakeMarketApi()
    market_api.candles_response = response
    client = OkxClient(settings=Settings(), flag="1", market_api=market_api)

    with pytest.raises(RuntimeError, match="OKX API response missing candle data"):
        client.get_candles("BTC-USDT-SWAP", "1m", 100)


def test_get_ticker_empty_data_raises_runtime_error():
    market_api = FakeMarketApi()
    market_api.ticker_response = {"code": "0", "data": []}
    client = OkxClient(settings=Settings(), flag="1", market_api=market_api)

    with pytest.raises(RuntimeError, match="OKX API response missing ticker data"):
        client.get_ticker("BTC-USDT-SWAP")


def test_non_zero_okx_response_raises_runtime_error():
    market_api = FakeMarketApi()
    market_api.candles_response = {"code": "51000", "msg": "Parameter error", "data": []}
    client = OkxClient(settings=Settings(), flag="1", market_api=market_api)

    with pytest.raises(RuntimeError, match="51000.*Parameter error"):
        client.get_candles("BTC-USDT-SWAP", "1m", 100)


def test_normalize_candles_returns_oldest_first_with_close_and_time():
    rows = [
        ["1714550460000", "101", "105", "100", "104.5", "12.5"],
        ["1714550400000", "100", "103", "99", "101.25", "10.5"],
    ]

    candles = normalize_candles("BTC-USDT-SWAP", "1m", rows)

    assert [candle.close for candle in candles] == [101.25, 104.5]
    assert candles[0].timestamp == datetime(2024, 5, 1, 8, 0, tzinfo=UTC)
    assert candles[0].timestamp.tzinfo is UTC
    assert candles[0].symbol == "BTC-USDT-SWAP"
    assert candles[0].timeframe == "1m"


def test_normalize_ticker_maps_prices():
    ticker = normalize_ticker(
        {
            "instId": "BTC-USDT-SWAP",
            "last": "101.5",
            "bidPx": "101.4",
            "askPx": "101.6",
            "ts": "1714550400000",
            "vol24h": "12345.6",
        }
    )

    assert ticker.symbol == "BTC-USDT-SWAP"
    assert ticker.last == 101.5
    assert ticker.bid == 101.4
    assert ticker.ask == 101.6
    assert ticker.volume_24h == 12345.6
    assert ticker.timestamp == datetime(2024, 5, 1, 8, 0, tzinfo=UTC)
    assert ticker.timestamp.tzinfo is UTC


def test_normalize_ticker_treats_missing_bid_ask_as_none():
    ticker = normalize_ticker(
        {
            "instId": "BTC-USDT-SWAP",
            "last": "101.5",
            "ts": "1714550400000",
            "vol24h": "12345.6",
        }
    )

    assert ticker.bid is None
    assert ticker.ask is None


def test_normalize_ticker_treats_empty_bid_ask_as_none():
    ticker = normalize_ticker(
        {
            "instId": "BTC-USDT-SWAP",
            "last": "101.5",
            "bidPx": "",
            "askPx": "",
            "ts": "1714550400000",
            "vol24h": "12345.6",
        }
    )

    assert ticker.bid is None
    assert ticker.ask is None


def test_normalize_ticker_treats_none_bid_ask_as_none():
    ticker = normalize_ticker(
        {
            "instId": "BTC-USDT-SWAP",
            "last": "101.5",
            "bidPx": None,
            "askPx": None,
            "ts": "1714550400000",
            "vol24h": "12345.6",
        }
    )

    assert ticker.bid is None
    assert ticker.ask is None
