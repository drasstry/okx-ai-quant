# OKX AI Quant

> A demo-first, AI-assisted quant trading starter kit for OKX.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-pytest-green)
![Trading](https://img.shields.io/badge/trading-demo--first-orange)
![Status](https://img.shields.io/badge/status-MVP-yellow)

[中文文档](README.zh-CN.md) | English

OKX AI Quant is a small, readable, testable Python project that connects OKX market data, rule-based quant strategies, risk controls, bilingual AI-style explanations, SQLite journaling, and a command-line workflow.

It is designed for builders who want to learn and iterate on crypto quant trading without starting from a huge framework.

> This is not financial advice. This is an engineering and learning project. Read [RISK.md](RISK.md) before connecting any account.

---

## Why this project?

Most trading bots are either too simple to be useful or too large to understand.

OKX AI Quant aims for the middle path:

- **Readable**: small modules, plain Python, clear responsibilities
- **Practical**: uses the OKX Python SDK and real exchange data
- **Auditable**: strategy, risk, execution, storage, and reporting are separated
- **Demo-first**: built for OKX demo trading before any live-capital experiments
- **Extensible**: add new strategies through a strategy factory

The core idea is simple:

> Strategy decides what looks interesting. Risk decides what is allowed.

---

## What it can do today

- Fetch OKX ticker and candle data
- Normalize `1H` and `4H` candles
- Run 6 built-in quant strategies
- Estimate fee, slippage, and minimum expected move
- Reject unsafe trades through a shared risk guard
- Generate bilingual trade analysis with deterministic fallback or an OpenAI-compatible LLM
- Store candles, signals, risk decisions, orders, fills, positions, exits, balances, reports, and analyses in SQLite
- Manage bot-recorded positions with stop-loss, take-profit, reverse-signal, timeout, and risk exits
- Reconcile OKX account balances and real SWAP positions on each bot cycle
- Run from CLI flags, an interactive wizard, or a menu-style control console
- Push reports through console or Telegram notifications
- Submit OKX demo orders when the account mode and API permissions allow it
- Keep demo and live OKX API keys isolated, with live mode behind an explicit opt-in

---

## Built-in strategies

| CLI name | Type | What it looks for |
| --- | --- | --- |
| `ema-rsi-atr` | Trend following | `4H` and `1H` EMA trend alignment, RSI confirmation, ATR stop/target |
| `rsi-bollinger-reversion` | Mean reversion | Price near Bollinger band extremes plus RSI overbought/oversold |
| `donchian-breakout` | Breakout | Price breaks recent channel high/low with `4H` trend confirmation |
| `ema-momentum` | Momentum | EMA direction and recent price momentum agree |
| `multi-timeframe-trend` | Trend following | `4H` and `1H` EMA stacks align with RSI and ATR-based stop/target |
| `volatility-adjusted-momentum` | Momentum | Recent momentum must clear ATR noise before entering |

Every strategy returns one of:

- `LONG`
- `SHORT`
- `HOLD`

A `LONG` or `SHORT` signal still needs to pass risk checks before an order is submitted.

For the default `*-USDT-SWAP` instruments, `LONG` maps to OKX `buy` and `SHORT` maps to OKX `sell` through isolated margin mode. Before live trading, verify account mode, position mode, contract size, precision, minimum order, and margin settings in OKX demo first.

---

## Architecture

```text
OKX API
  -> account.py          parse account balances and equity
  -> market_data.py      normalize candles and ticker data
  -> strategy.py         generate LONG / SHORT / HOLD signals
  -> cost.py             estimate fee + slippage threshold
  -> risk.py             reject unsafe trades
  -> analysis.py         explain the decision in English and Chinese
  -> llm.py              optional OpenAI-compatible LLM analysis
  -> execution.py        submit approved demo/live orders after guardrails
  -> bot.py              long-running multi-symbol observe/trading loop
  -> notifier.py         console / Telegram / null notifications
  -> storage.py          persist everything in SQLite
  -> cli.py              user-facing command line entry
```

---

## Quick start

### 1. Install dependencies

Requirements:

- Python 3.11+
- Git
- [`uv`](https://docs.astral.sh/uv/)
- OKX account
- OKX demo trading API key
- Optional LLM API key for richer bilingual analysis

```bash
git clone https://github.com/<your-name>/okx-ai-quant.git
cd okx-ai-quant
uv sync
```

### 2. Create `.env`

```bash
cp .env.example .env
```

Fill in your OKX demo credentials and, optionally, an LLM key:

```dotenv
TRADING_MODE=demo
ALLOW_LIVE_TRADING=false

# OKX demo trading credentials. Use these for normal development.
OKX_DEMO_API_KEY=your_demo_api_key
OKX_DEMO_API_SECRET=your_demo_api_secret
OKX_DEMO_API_PASSPHRASE=your_demo_api_passphrase
OKX_DEMO_PROJECT_ID=

# OKX live trading credentials. Leave empty until you intentionally enable live mode.
OKX_LIVE_API_KEY=
OKX_LIVE_API_SECRET=
OKX_LIVE_API_PASSPHRASE=
OKX_LIVE_PROJECT_ID=

# OpenAI-compatible LLM analysis. Default provider: Doubao / Volcengine Ark.
LLM_ENABLED=true
LLM_PROVIDER=doubao
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=your_ark_api_key
# You can also set ARK_API_KEY instead of LLM_API_KEY for Doubao.
LLM_MODEL=doubao-seed-2-0-pro-260215

DB_PATH=data/okx_ai_quant.sqlite3
SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,ADA-USDT-SWAP,AVAX-USDT-SWAP,LINK-USDT-SWAP,DOT-USDT-SWAP,LTC-USDT-SWAP,BCH-USDT-SWAP,BNB-USDT-SWAP,TRX-USDT-SWAP,TON-USDT-SWAP,UNI-USDT-SWAP,AAVE-USDT-SWAP,OP-USDT-SWAP,ARB-USDT-SWAP,NEAR-USDT-SWAP,ATOM-USDT-SWAP,ETC-USDT-SWAP,FIL-USDT-SWAP,INJ-USDT-SWAP
ENABLE_TRADING=false
POLL_INTERVAL_SECONDS=300
APP_TIMEZONE=Asia/Shanghai
REPORT_TIMES=00:00,08:00,12:00,20:00
ORDER_STALE_SECONDS=900
MANAGE_EXISTING_POSITIONS=true
POSITION_TIMEOUT_HOURS=72
EXIT_ON_REVERSE_SIGNAL=true
NOTIFIER=console
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
STRATEGY_NAME=ema-rsi-atr
REFERENCE_CAPITAL_USDT=1000
MAX_RISK_PER_TRADE=0.01
MAX_DAILY_LOSS=0.02
MAX_CONSECUTIVE_LOSSES=3
MAX_LEVERAGE=1
FEE_RATE_PER_SIDE=0.001
SLIPPAGE_RATE=0.001
MIN_EXPECTED_MOVE=0.006
```

Never commit `.env`.

`OKX_API_KEY`, `OKX_API_SECRET`, and `OKX_API_PASSPHRASE` are still supported as backward-compatible demo credentials, but new setups should prefer `OKX_DEMO_*`.

### 3. Run tests

```bash
uv run python -m pytest -q
uv run ruff check .
```

---

## Startup Guide

The safest startup path is:

1. run observe mode first
2. confirm market data, signals, risk decisions, and reports look sane
3. enable demo order submission
4. only then consider live mode with tiny capital

### Interactive menu

Use this when you want a guided local control console:

```bash
uv run okx-ai-quant menu
```

The menu first lets you choose Chinese or English. It then shows the current mode, whether order submission is enabled, the configured symbol pool, and actions for one bot cycle, AI report delivery, custom strategy run, or continuous bot mode.

When you choose `Start continuous bot`, the menu asks you to confirm or override the important runtime options for this launch:

- trading mode: `demo` or `live`
- whether order submission is enabled
- strategy
- symbol whitelist
- poll interval
- report times
- notifier
- leverage cap
- reference capital
- max risk per trade
- max daily loss
- stale-order cancellation time
- position monitoring and exit settings

The continuous bot uses `STRATEGY_NAME` from `.env` as the default strategy:

```dotenv
STRATEGY_NAME=ema-rsi-atr
```

Supported values are `ema-rsi-atr`, `rsi-bollinger-reversion`, `donchian-breakout`, `ema-momentum`, `multi-timeframe-trend`, and `volatility-adjusted-momentum`.

### Observe mode

Observe mode scans all configured symbols and writes data to SQLite, but does not submit orders.

Keep this in `.env`:

```dotenv
TRADING_MODE=demo
ENABLE_TRADING=false
```

Run one full bot cycle:

```bash
uv run okx-ai-quant bot --once --mode demo
```

Run continuously:

```bash
uv run okx-ai-quant bot --mode demo
```

### Demo trading mode

Demo trading mode can submit approved orders to OKX Demo Trading.

Set:

```dotenv
TRADING_MODE=demo
ENABLE_TRADING=true
OKX_DEMO_API_KEY=your_demo_api_key
OKX_DEMO_API_SECRET=your_demo_api_secret
OKX_DEMO_API_PASSPHRASE=your_demo_api_passphrase
```

Then run:

```bash
uv run okx-ai-quant bot --mode demo
```

For a single symbol one-off submit:

```bash
uv run okx-ai-quant run-once \
  --symbol BTC-USDT-SWAP \
  --strategy ema-rsi-atr \
  --mode demo \
  --submit
```

### Live trading mode

Live mode can place real orders. It requires two independent gates:

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
ENABLE_TRADING=true
OKX_LIVE_API_KEY=your_live_api_key
OKX_LIVE_API_SECRET=your_live_api_secret
OKX_LIVE_API_PASSPHRASE=your_live_api_passphrase
```

Then run:

```bash
uv run okx-ai-quant bot --mode live
```

Before live use, verify OKX account mode, position mode, contract size, precision, minimum order size, margin mode, and API permissions in demo first.

### One decision cycle

Use this for quick strategy checks:

```bash
uv run okx-ai-quant run-once \
  --symbol BTC-USDT-SWAP \
  --strategy ema-rsi-atr \
  --mode demo \
  --leverage 1
```

By default, `run-once` follows `ENABLE_TRADING`. If you want to submit an approved order from a one-off command, pass `--submit`.

### What the bot does

- scan every symbol in `SYMBOLS`
- query OKX balances and real SWAP positions, then reconcile local state
- persist candles, signals, risk decisions, analyses, orders, balances, fills, and reports
- skip new orders when a bot order is already pending
- avoid repeating the same signal on the same candle
- query tracked orders, record fills, update positions, and cancel stale bot orders
- monitor bot-recorded open positions before opening new ones
- submit reduce-only close orders for stop-loss, take-profit, reverse-signal, timeout, or risk exits
- record closed-position PnL and exit attribution for review
- send reports through the configured notifier

Position reconciliation treats OKX as the source of truth for real SWAP
quantity, side, and average entry price. Local stop-loss, take-profit, timeout,
and entry-signal metadata are preserved when a matching local position exists.
Only symbols in `SYMBOLS` are reconciled, so unrelated manual positions are not
automatically adopted by the bot.

### Send a report now

```bash
uv run okx-ai-quant report-now --mode demo
```

### Interactive wizard

```bash
uv run okx-ai-quant wizard
```

The wizard lets you choose:

1. demo or live mode
2. strategy
3. symbol
4. leverage
5. final confirmation before running

### Telegram AI reports

Reports include the day's metrics and any stored LLM-backed trade analyses. To push reports to Telegram:

```dotenv
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
LLM_ENABLED=true
LLM_API_KEY=your_llm_key
```

Then run:

```bash
uv run okx-ai-quant report-now --mode demo
```

Scheduled reports are sent by the long-running bot at `REPORT_TIMES`.
Each configured time is tracked independently, so `00:00,08:00,12:00,20:00`
can send up to four reports per day. If the bot loop wakes a few minutes late,
it sends the missed slot once and records it as sent.

You can also let Telegram trigger reports on demand:

```bash
uv run okx-ai-quant telegram-listen --mode demo
```

Supported Telegram messages:

- `/report`, `/daily`, `/summary`
- `日报`, `报告`, `总结`, `分析`
- `/status`

`/status` prefers the active bot runtime state written by the long-running
bot. If no active runtime state exists yet, it falls back to the Telegram
listener's own `.env` configuration.

---

## Supported Symbols

The default symbol pool focuses on mainstream OKX USDT perpetual swaps:

```text
BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, XRP-USDT-SWAP,
DOGE-USDT-SWAP, ADA-USDT-SWAP, AVAX-USDT-SWAP, LINK-USDT-SWAP,
DOT-USDT-SWAP, LTC-USDT-SWAP, BCH-USDT-SWAP, BNB-USDT-SWAP,
TRX-USDT-SWAP, TON-USDT-SWAP, UNI-USDT-SWAP, AAVE-USDT-SWAP,
OP-USDT-SWAP, ARB-USDT-SWAP, NEAR-USDT-SWAP, ATOM-USDT-SWAP,
ETC-USDT-SWAP, FIL-USDT-SWAP, INJ-USDT-SWAP
```

Override the list in `.env`:

```dotenv
SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP
```

Only symbols in this whitelist can pass the risk guard.

---

## Demo and live OKX keys

OKX separates demo API keys from live API keys. This project mirrors that separation:

| Mode | OKX flag | Credentials used | Safety gate |
| --- | --- | --- | --- |
| `demo` | `1` | `OKX_DEMO_*`, then legacy `OKX_API_*` fallback | Default mode |
| `live` | `0` | `OKX_LIVE_*` only | Requires `ALLOW_LIVE_TRADING=true` |

To intentionally run the live path:

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
OKX_LIVE_API_KEY=your_live_api_key
OKX_LIVE_API_SECRET=your_live_api_secret
OKX_LIVE_API_PASSPHRASE=your_live_api_passphrase
```

Then choose live mode in the CLI:

```bash
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-rsi-atr --mode live
```

For the long-running bot, live order submission requires both live mode and `ENABLE_TRADING=true`:

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
ENABLE_TRADING=true
```

```bash
uv run okx-ai-quant bot --mode live
```

Live mode can place real orders. Use API keys without withdrawal permission, enable IP allowlists, start with tiny size, and verify OKX account mode, margin mode, instrument permissions, and order size units first.

---

## LLM analysis

The trading strategy and risk guard are deterministic. The LLM is only used to explain the result in clearer English and Chinese. If no LLM key is configured, or if the provider fails, the project falls back to a deterministic local explanation.

Default configuration uses Doubao through Volcengine Ark's OpenAI-compatible endpoint:

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=doubao
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=your_ark_api_key
# Or set ARK_API_KEY=your_ark_api_key
LLM_MODEL=doubao-seed-2-0-pro-260215
```

Equivalent minimal SDK call:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=os.getenv("ARK_API_KEY"),
)

response = client.responses.create(
    model="doubao-seed-2-0-pro-260215",
    input=[
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Explain this trading signal."},
            ],
        }
    ],
)
print(response)
```

Popular OpenAI-compatible provider examples:

| Provider | `LLM_BASE_URL` | Example model | API key env |
| --- | --- | --- | --- |
| Doubao / Volcengine Ark | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-2-0-pro-260215` | `ARK_API_KEY` or `LLM_API_KEY` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | `OPENAI_API_KEY` or `LLM_API_KEY` |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | `DEEPSEEK_API_KEY` or `LLM_API_KEY` |
| Alibaba Qwen / DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `DASHSCOPE_API_KEY` or `LLM_API_KEY` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | `GEMINI_API_KEY` or `LLM_API_KEY` |
| Moonshot / Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | `MOONSHOT_API_KEY` or `LLM_API_KEY` |
| Zhipu GLM | `https://open.bigmodel.cn/api/paas/v4/` | `glm-4-flash` | `ZHIPU_API_KEY` or `LLM_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4.1-mini` | `OPENROUTER_API_KEY` or `LLM_API_KEY` |

For non-Doubao providers, copy that provider's key into `LLM_API_KEY`, set `LLM_BASE_URL`, and set `LLM_MODEL`.

---

## CLI examples

```bash
# Show help
uv run okx-ai-quant --help

# Interactive control console
uv run okx-ai-quant menu

# One observe/trading bot cycle
uv run okx-ai-quant bot --once --mode demo

# Continuous bot loop
uv run okx-ai-quant bot --mode demo

# Send today's AI report now
uv run okx-ai-quant report-now --mode demo

# Trend following
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-rsi-atr

# Mean reversion
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy rsi-bollinger-reversion

# Donchian breakout
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy donchian-breakout

# EMA momentum
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-momentum

# Stricter multi-timeframe trend
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy multi-timeframe-trend

# Momentum filtered by ATR noise
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy volatility-adjusted-momentum
```

---

## Safety model

Current guardrails:

- `TRADING_MODE=demo` by default
- `ALLOW_LIVE_TRADING=false` by default
- live mode requires explicit opt-in
- max leverage is capped at `2x`
- default leverage is `1x`
- symbols are whitelisted
- non-`LONG`/`SHORT` signals are rejected
- daily loss, consecutive loss, and open-position limits are enforced
- live mode requires `ALLOW_LIVE_TRADING=true` and complete `OKX_LIVE_*` credentials

For OKX swaps, order size must use contract units and instrument metadata. Treat the current sizing as an MVP path: verify it in demo first and confirm the exact OKX `sz` semantics before using real funds.

---

## Troubleshooting

### `50113 Invalid Sign`

Private OKX authentication failed. Public market data may still work.

Check:

- demo key is used with `TRADING_MODE=demo`
- live key is used only with `TRADING_MODE=live`
- API secret is copied completely
- passphrase is exactly correct
- `.env` has no extra whitespace
- API key was created from OKX Demo Trading if you run demo mode
- IP allowlist includes the current machine

### `51010 current account mode`

OKX accepted authentication, but the account mode does not allow this order request.

Check OKX account settings such as demo/live mode, trading mode, margin mode, derivatives permissions, and instrument availability.

### No order was submitted

That can be normal. Common reasons:

- strategy returned `HOLD`
- expected move was below cost threshold
- risk guard rejected the signal
- OKX account mode rejected the order
- live execution guard blocked the order

---

## Project structure

```text
okx-ai-quant/
  src/okx_ai_quant/
    account.py       # OKX balance parsing
    analysis.py      # bilingual decision explanation
    bot.py           # long-running multi-symbol bot loop
    cli.py           # CLI entry and wizard
    config.py        # .env settings and live-mode safety gate
    cost.py          # fee, slippage, expected-move threshold
    execution.py     # OKX order submission wrapper
    indicators.py    # EMA / RSI / ATR
    llm.py           # optional OpenAI-compatible analysis client
    market_data.py   # OKX market data normalization
    notifier.py      # console / Telegram / null notification backends
    okx_client.py    # python-okx adapter
    reports.py       # daily report rendering
    risk.py          # risk guard
    runner.py        # one-cycle orchestration
    storage.py       # SQLite persistence
    strategy.py      # built-in strategies and strategy factory
  tests/
  README.md
  README.zh-CN.md
  RISK.md
  .env.example
```

---

## Roadmap

Near-term:

- add a read-only smoke-test command
- add historical backtesting
- add paper-trading reports and equity curve charts
- add per-strategy configuration
- improve OKX order sizing with instrument metadata

Before meaningful live trading:

- verify correct OKX contract sizing
- add exchange precision and minimum-order checks
- add order reconciliation
- add position tracking
- add kill switch
- add alerting
- run weeks of demo forward testing
- independent code and risk review

---

## Contributing

PRs are welcome, especially for tests, risk controls, backtesting, documentation, safer execution logic, and strategy research.

Please keep the project beginner-friendly. If a feature makes the system harder to understand, document it clearly and add tests.

---

## Disclaimer

This project is not investment advice and does not guarantee profit. Crypto trading is risky. Automated systems can lose money quickly because of bad strategy assumptions, bugs, exchange outages, latency, slippage, liquidation, or incorrect configuration.

Use demo trading first. Do not connect real capital until you understand every line of the execution path.
