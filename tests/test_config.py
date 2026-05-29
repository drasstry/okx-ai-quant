import pytest

from okx_ai_quant.config import DEFAULT_SYMBOLS, MarginMode, Settings, TradingMode


SETTINGS_ENV_VARS = (
    "TRADING_MODE",
    "ALLOW_LIVE_TRADING",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
    "OKX_PROJECT_ID",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
    "OKX_DEMO_PROJECT_ID",
    "OKX_LIVE_API_KEY",
    "OKX_LIVE_API_SECRET",
    "OKX_LIVE_API_PASSPHRASE",
    "OKX_LIVE_PROJECT_ID",
    "LLM_ENABLED",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "ARK_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "GEMINI_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY",
    "DB_PATH",
    "SYMBOLS",
    "ENABLE_TRADING",
    "POLL_INTERVAL_SECONDS",
    "APP_TIMEZONE",
    "REPORT_TIMES",
    "ORDER_STALE_SECONDS",
    "MANAGE_EXISTING_POSITIONS",
    "OKX_ENTRY_HEALTHCHECK_ENABLED",
    "OKX_ENTRY_HEALTHCHECK_ATTEMPTS",
    "OKX_ENTRY_HEALTHCHECK_MIN_SUCCESSES",
    "NOTIFIER",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_PROXY_URL",
    "MARKET_CANDLE_LIMIT",
    "STRATEGY_NAME",
    "REFERENCE_CAPITAL_USDT",
    "MAX_RISK_PER_TRADE",
    "MAX_DAILY_LOSS",
    "MAX_CONSECUTIVE_LOSSES",
    "MAX_OPEN_POSITIONS",
    "MAX_LEVERAGE",
    "MARGIN_MODE",
    "FEE_RATE_PER_SIDE",
    "SLIPPAGE_RATE",
    "MIN_EXPECTED_MOVE",
)


@pytest.fixture(autouse=True)
def clear_settings_env(monkeypatch):
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides):
    return Settings(_env_file=None, **overrides)


def test_demo_mode_uses_okx_flag_1():
    settings = make_settings(TRADING_MODE="demo")

    assert settings.trading_mode == TradingMode.DEMO
    assert settings.okx_flag == "1"


def test_live_mode_requires_explicit_allow_live():
    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING=true"):
        make_settings(TRADING_MODE="live")


def test_symbol_list_is_parsed_from_csv():
    settings = make_settings(TRADING_MODE="demo", SYMBOLS="BTC-USDT-SWAP, ETH-USDT-SWAP")

    assert settings.symbols == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def test_default_symbols_include_mainstream_swaps():
    settings = make_settings()

    assert settings.symbols == list(DEFAULT_SYMBOLS)
    assert "SOL-USDT-SWAP" in settings.symbols
    assert "INJ-USDT-SWAP" in settings.symbols


def test_max_leverage_can_be_configured_above_two():
    settings = make_settings(TRADING_MODE="demo", MAX_LEVERAGE=5)

    assert settings.MAX_LEVERAGE == 5


def test_default_margin_and_position_limits_are_demo_friendly():
    settings = make_settings()

    assert settings.MARGIN_MODE == MarginMode.CROSS
    assert settings.MAX_OPEN_POSITIONS == 5
    assert settings.OKX_ENTRY_HEALTHCHECK_ENABLED is True
    assert settings.OKX_ENTRY_HEALTHCHECK_ATTEMPTS == 5
    assert settings.OKX_ENTRY_HEALTHCHECK_MIN_SUCCESSES == 5


def test_entry_healthcheck_min_successes_cannot_exceed_attempts():
    with pytest.raises(ValueError, match="OKX_ENTRY_HEALTHCHECK_MIN_SUCCESSES"):
        make_settings(
            OKX_ENTRY_HEALTHCHECK_ATTEMPTS=2,
            OKX_ENTRY_HEALTHCHECK_MIN_SUCCESSES=3,
        )


def test_default_fee_rate_per_side_is_001():
    settings = make_settings()

    assert settings.FEE_RATE_PER_SIDE == 0.001


def test_bot_defaults_to_observe_mode_and_report_times_parse():
    settings = make_settings(REPORT_TIMES="00:00, 08:00,20:00")

    assert settings.ENABLE_TRADING is False
    assert settings.POLL_INTERVAL_SECONDS == 300
    assert settings.STRATEGY_NAME == "ema-rsi-atr"
    assert settings.report_times == ["00:00", "08:00", "20:00"]


def test_demo_credentials_prefer_demo_specific_keys_over_legacy_keys():
    settings = make_settings(
        TRADING_MODE="demo",
        OKX_API_KEY="legacy-key",
        OKX_API_SECRET="legacy-secret",
        OKX_API_PASSPHRASE="legacy-passphrase",
        OKX_DEMO_API_KEY="demo-key",
        OKX_DEMO_API_SECRET="demo-secret",
        OKX_DEMO_API_PASSPHRASE="demo-passphrase",
        OKX_DEMO_PROJECT_ID="demo-project",
    )

    credentials = settings.okx_credentials

    assert credentials.api_key == "demo-key"
    assert credentials.api_secret == "demo-secret"
    assert credentials.passphrase == "demo-passphrase"
    assert credentials.project_id == "demo-project"


def test_demo_credentials_fall_back_to_legacy_keys():
    settings = make_settings(
        TRADING_MODE="demo",
        OKX_API_KEY="legacy-key",
        OKX_API_SECRET="legacy-secret",
        OKX_API_PASSPHRASE="legacy-passphrase",
    )

    credentials = settings.okx_credentials

    assert credentials.api_key == "legacy-key"
    assert credentials.api_secret == "legacy-secret"
    assert credentials.passphrase == "legacy-passphrase"


def test_live_mode_requires_live_credentials_when_enabled():
    with pytest.raises(ValueError, match="OKX_LIVE_API_KEY"):
        make_settings(TRADING_MODE="live", ALLOW_LIVE_TRADING=True)


def test_live_credentials_are_selected_for_live_mode():
    settings = make_settings(
        TRADING_MODE="live",
        ALLOW_LIVE_TRADING=True,
        OKX_LIVE_API_KEY="live-key",
        OKX_LIVE_API_SECRET="live-secret",
        OKX_LIVE_API_PASSPHRASE="live-passphrase",
        OKX_LIVE_PROJECT_ID="live-project",
    )

    credentials = settings.okx_credentials

    assert settings.okx_flag == "0"
    assert credentials.api_key == "live-key"
    assert credentials.api_secret == "live-secret"
    assert credentials.passphrase == "live-passphrase"
    assert credentials.project_id == "live-project"


def test_llm_defaults_to_doubao_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-key")

    settings = make_settings()

    assert settings.LLM_ENABLED is True
    assert settings.LLM_BASE_URL == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.LLM_API_KEY == "ark-key"
    assert settings.LLM_MODEL == "doubao-seed-2-0-pro-260215"


def test_llm_api_key_can_use_provider_specific_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    settings = make_settings()

    assert settings.LLM_API_KEY == "deepseek-key"
