from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYMBOLS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "ADA-USDT-SWAP",
    "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP",
    "DOT-USDT-SWAP",
    "LTC-USDT-SWAP",
    "BCH-USDT-SWAP",
    "BNB-USDT-SWAP",
    "TRX-USDT-SWAP",
    "TON-USDT-SWAP",
    "UNI-USDT-SWAP",
    "AAVE-USDT-SWAP",
    "OP-USDT-SWAP",
    "ARB-USDT-SWAP",
    "NEAR-USDT-SWAP",
    "ATOM-USDT-SWAP",
    "ETC-USDT-SWAP",
    "FIL-USDT-SWAP",
    "INJ-USDT-SWAP",
)


class TradingMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True)
class OkxCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    project_id: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    TRADING_MODE: TradingMode = TradingMode.DEMO
    ALLOW_LIVE_TRADING: bool = False

    # Backward-compatible demo credentials. Prefer OKX_DEMO_* for new installs.
    OKX_API_KEY: str = ""
    OKX_API_SECRET: str = ""
    OKX_API_PASSPHRASE: str = ""
    OKX_PROJECT_ID: str = ""

    OKX_DEMO_API_KEY: str = ""
    OKX_DEMO_API_SECRET: str = ""
    OKX_DEMO_API_PASSPHRASE: str = ""
    OKX_DEMO_PROJECT_ID: str = ""

    OKX_LIVE_API_KEY: str = ""
    OKX_LIVE_API_SECRET: str = ""
    OKX_LIVE_API_PASSPHRASE: str = ""
    OKX_LIVE_PROJECT_ID: str = ""

    LLM_ENABLED: bool = True
    LLM_PROVIDER: str = "doubao"
    LLM_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    LLM_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_API_KEY",
            "ARK_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "DASHSCOPE_API_KEY",
            "GEMINI_API_KEY",
            "MOONSHOT_API_KEY",
            "ZHIPU_API_KEY",
            "OPENROUTER_API_KEY",
        ),
        description=(
            "API key for the configured LLM_PROVIDER. The provider-specific "
            "aliases (OPENAI_API_KEY, DEEPSEEK_API_KEY, ...) exist for "
            "convenience; ALWAYS make sure the alias you rely on actually "
            "matches ``LLM_BASE_URL`` to avoid sending credentials to the "
            "wrong host."
        ),
    )
    LLM_MODEL: str = "doubao-seed-2-0-pro-260215"

    DB_PATH: Path = Path("data/okx_ai_quant.sqlite3")
    SYMBOLS: str = ",".join(DEFAULT_SYMBOLS)

    ENABLE_TRADING: bool = False
    POLL_INTERVAL_SECONDS: int = Field(default=300, ge=5)
    APP_TIMEZONE: str = "Asia/Shanghai"
    REPORT_TIMES: str = "00:00,08:00,12:00,20:00"
    ORDER_STALE_SECONDS: int = Field(default=900, ge=30)
    MANAGE_EXISTING_POSITIONS: bool = False
    NOTIFIER: str = "console"
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    MARKET_CANDLE_LIMIT: int = Field(default=100, ge=30)
    STRATEGY_NAME: str = "ema-rsi-atr"

    REFERENCE_CAPITAL_USDT: float = Field(default=1000.0, gt=0)
    MAX_RISK_PER_TRADE: float = Field(default=0.01, gt=0, le=0.02)
    MAX_DAILY_LOSS: float = Field(default=0.02, gt=0, le=0.10)
    MAX_CONSECUTIVE_LOSSES: int = Field(default=3, ge=1, le=10)
    MAX_LEVERAGE: int = Field(default=1, ge=1)
    FEE_RATE_PER_SIDE: float = Field(default=0.001, ge=0)
    SLIPPAGE_RATE: float = Field(default=0.001, ge=0)
    MIN_EXPECTED_MOVE: float = Field(default=0.006, gt=0)

    @field_validator("MAX_LEVERAGE")
    @classmethod
    def max_leverage_must_not_exceed_two(cls, value: int) -> int:
        if value > 2:
            raise ValueError("MAX_LEVERAGE must be <= 2")
        return value

    @model_validator(mode="after")
    def require_explicit_live_trading_opt_in(self) -> "Settings":
        if self.TRADING_MODE == TradingMode.LIVE:
            if not self.ALLOW_LIVE_TRADING:
                raise ValueError("ALLOW_LIVE_TRADING=true is required for live trading")
            missing = [
                name
                for name, value in (
                    ("OKX_LIVE_API_KEY", self.OKX_LIVE_API_KEY),
                    ("OKX_LIVE_API_SECRET", self.OKX_LIVE_API_SECRET),
                    ("OKX_LIVE_API_PASSPHRASE", self.OKX_LIVE_API_PASSPHRASE),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"Live trading requires: {', '.join(missing)}")
        return self

    @property
    def trading_mode(self) -> TradingMode:
        return self.TRADING_MODE

    @property
    def okx_flag(self) -> str:
        return "1" if self.TRADING_MODE == TradingMode.DEMO else "0"

    @property
    def okx_credentials(self) -> OkxCredentials:
        if self.TRADING_MODE == TradingMode.LIVE:
            return OkxCredentials(
                api_key=self.OKX_LIVE_API_KEY,
                api_secret=self.OKX_LIVE_API_SECRET,
                passphrase=self.OKX_LIVE_API_PASSPHRASE,
                project_id=self.OKX_LIVE_PROJECT_ID,
            )

        return OkxCredentials(
            api_key=self.OKX_DEMO_API_KEY or self.OKX_API_KEY,
            api_secret=self.OKX_DEMO_API_SECRET or self.OKX_API_SECRET,
            passphrase=self.OKX_DEMO_API_PASSPHRASE or self.OKX_API_PASSPHRASE,
            project_id=self.OKX_DEMO_PROJECT_ID or self.OKX_PROJECT_ID,
        )

    @property
    def symbols(self) -> list[str]:
        return [symbol.strip() for symbol in self.SYMBOLS.split(",") if symbol.strip()]

    @property
    def report_times(self) -> list[str]:
        return [value.strip() for value in self.REPORT_TIMES.split(",") if value.strip()]
