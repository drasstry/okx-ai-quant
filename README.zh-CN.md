# OKX AI Quant

> 面向 OKX 的模拟盘优先、AI 辅助量化交易机器人项目。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-pytest-green)
![Trading](https://img.shields.io/badge/trading-demo--first-orange)
![Status](https://img.shields.io/badge/status-MVP-yellow)

中文 | [English](README.md)

OKX AI Quant 是一个小而清晰、可测试、可扩展的 Python 项目，用来跑通 OKX 行情、规则化量化策略、风控、双语 AI 解读、SQLite 记录、Telegram/控制台通知，以及命令行交互流程。

它适合想学习、验证、迭代加密货币量化交易想法的人，不需要一上来就理解庞大的交易框架。

> 本项目不构成投资建议，只是工程学习项目。连接任何账户前，请先阅读 [RISK.md](RISK.md)。

---

## 为什么做这个项目？

很多交易机器人要么太简单，无法真实迭代；要么太庞大，新手很难读懂。

OKX AI Quant 走中间路线：

- **容易读懂**：模块小，职责清晰，普通 Python
- **贴近真实**：使用 OKX Python SDK 和真实交易所行情
- **方便审计**：策略、风控、执行、存储、报告分离
- **模拟盘优先**：先在 OKX Demo Trading 中验证，再考虑实盘
- **容易扩展**：通过策略工厂新增策略，不需要重写运行主流程

核心原则很简单：

> 策略负责发现机会，风控决定是否允许交易。

---

## 当前能做什么？

- 获取 OKX ticker 和 K 线数据
- 标准化 `1H` 和 `4H` K 线
- 运行 6 个内置量化策略
- 支持 `LONG`、`SHORT`、`HOLD` 信号
- 估算手续费、滑点和最小预期波动
- 通过统一风控守卫拒绝不安全交易
- 生成中英双语交易分析，支持本地确定性 fallback 或 OpenAI-compatible 大模型解读
- 用 SQLite 记录 K 线、信号、风控决策、订单、成交、持仓、退出、余额、报告和分析
- 管理本机器人记录的持仓，支持止损、止盈、反向信号、超时和风险退出
- 每轮查询 OKX 账户资产和真实 SWAP 合约持仓，并和本地状态对账
- 支持 CLI 参数、交互式向导、菜单式控制台
- 支持控制台或 Telegram 推送 AI 报告
- 当 OKX 账户模式和 API 权限允许时，提交 OKX 模拟盘订单
- 隔离 OKX 模拟盘和实盘 API Key，实盘路径必须显式开启

---

## 内置策略

| CLI 名称 | 类型 | 关注什么 |
| --- | --- | --- |
| `ema-rsi-atr` | 趋势跟随 | `4H` 和 `1H` EMA 趋势同向，RSI 确认，ATR 止损/止盈 |
| `rsi-bollinger-reversion` | 均值回归 | 价格接近 Bollinger 上下轨，同时 RSI 超买/超卖 |
| `donchian-breakout` | 突破 | 价格突破最近通道高/低点，并由 `4H` 趋势确认 |
| `ema-momentum` | 动量 | EMA 方向和近期价格动量同向 |
| `multi-timeframe-trend` | 多周期趋势 | `4H` 和 `1H` EMA 多头/空头排列同向，再结合 RSI 和 ATR 过滤 |
| `volatility-adjusted-momentum` | 波动率调整动量 | 近期动量必须超过 ATR 噪音阈值才入场 |

所有策略只会输出：

- `LONG`
- `SHORT`
- `HOLD`

即使策略输出 `LONG` 或 `SHORT`，也必须通过风控后才会提交订单。

对于默认的 `*-USDT-SWAP` 合约，`LONG` 会映射为 OKX `buy`，`SHORT` 会映射为 OKX `sell`，并使用 isolated margin 模式。实盘前必须先在 OKX 模拟盘确认账户模式、持仓模式、合约面值、精度、最小下单量和保证金设置。

---

## 架构

```text
OKX API
  -> account.py          解析账户余额和权益
  -> market_data.py      标准化 K 线和 ticker
  -> strategy.py         生成 LONG / SHORT / HOLD 信号
  -> cost.py             估算手续费和滑点阈值
  -> risk.py             拒绝不安全交易
  -> analysis.py         生成中英双语交易解释
  -> llm.py              可选 OpenAI-compatible 大模型解读
  -> execution.py        在风控后提交 demo/live 订单
  -> bot.py              多币种持续观察/交易循环
  -> notifier.py         console / Telegram / null 通知后端
  -> storage.py          持久化到 SQLite
  -> cli.py              命令行入口和交互菜单
```

---

## 快速开始

### 1. 安装依赖

你需要：

- Python 3.11+
- Git
- [`uv`](https://docs.astral.sh/uv/)
- OKX 账户
- OKX 模拟盘 API Key
- 可选：用于更自然双语解读的大模型 API Key

```bash
git clone https://github.com/<your-name>/okx-ai-quant.git
cd okx-ai-quant
uv sync
```

### 2. 创建 `.env`

```bash
cp .env.example .env
```

填入 OKX 模拟盘 API 信息，并按需配置大模型 Key：

```dotenv
TRADING_MODE=demo
ALLOW_LIVE_TRADING=false

# OKX 模拟盘 API Key，日常开发建议使用这一组。
OKX_DEMO_API_KEY=your_demo_api_key
OKX_DEMO_API_SECRET=your_demo_api_secret
OKX_DEMO_API_PASSPHRASE=your_demo_api_passphrase
OKX_DEMO_PROJECT_ID=

# OKX 实盘 API Key。除非你明确开启实盘，否则保持为空。
OKX_LIVE_API_KEY=
OKX_LIVE_API_SECRET=
OKX_LIVE_API_PASSPHRASE=
OKX_LIVE_PROJECT_ID=

# OpenAI-compatible 大模型解读。默认使用豆包 / 火山方舟。
LLM_ENABLED=true
LLM_PROVIDER=doubao
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=your_ark_api_key
# 豆包也可以设置 ARK_API_KEY=your_ark_api_key
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

不要提交 `.env`。

`OKX_API_KEY`、`OKX_API_SECRET`、`OKX_API_PASSPHRASE` 仍作为旧版兼容字段支持，默认当作模拟盘 Key；新配置建议使用 `OKX_DEMO_*`。

### 3. 运行测试

```bash
uv run python -m pytest -q
uv run ruff check .
```

---

## 启动说明

最安全的启动路径是：

1. 先观察，不下单
2. 确认行情、信号、风控决策和报告都正常
3. 再开启模拟盘下单
4. 模拟盘持续验证后，才考虑用极小资金进入实盘

### 交互菜单

推荐从菜单开始：

```bash
uv run okx-ai-quant menu
```

菜单启动后会先让你选择中文或英文，然后展示当前模式、是否允许下单、策略、轮询间隔、报告时间、交易对池和通知方式。

选择 `Start continuous bot` / `启动持续机器人` 时，菜单会让你确认或覆盖本次启动的重要参数：

- 交易模式：`demo` 或 `live`
- 是否启用下单
- 使用哪个策略
- 交易对白名单
- 轮询间隔
- 报告推送时间
- 通知方式
- 最大杠杆
- 参考本金
- 单笔最大风险
- 日内最大亏损
- 挂单超时取消时间
- 持仓监控和平仓参数

持续 bot 默认使用 `.env` 中的 `STRATEGY_NAME`：

```dotenv
STRATEGY_NAME=ema-rsi-atr
```

支持的策略值：

```text
ema-rsi-atr
rsi-bollinger-reversion
donchian-breakout
ema-momentum
multi-timeframe-trend
volatility-adjusted-momentum
```

### 观察模式

观察模式会扫描所有配置的交易对并写入 SQLite，但不会提交订单。

保持 `.env`：

```dotenv
TRADING_MODE=demo
ENABLE_TRADING=false
```

运行一次完整 bot 周期：

```bash
uv run okx-ai-quant bot --once --mode demo
```

持续运行：

```bash
uv run okx-ai-quant bot --mode demo
```

### 模拟盘交易模式

模拟盘交易模式会把通过风控的信号提交到 OKX Demo Trading。

设置：

```dotenv
TRADING_MODE=demo
ENABLE_TRADING=true
OKX_DEMO_API_KEY=your_demo_api_key
OKX_DEMO_API_SECRET=your_demo_api_secret
OKX_DEMO_API_PASSPHRASE=your_demo_api_passphrase
```

然后运行：

```bash
uv run okx-ai-quant bot --mode demo
```

单币种临时下单可以使用：

```bash
uv run okx-ai-quant run-once \
  --symbol BTC-USDT-SWAP \
  --strategy ema-rsi-atr \
  --mode demo \
  --submit
```

### 实盘交易模式

实盘模式可以提交真实订单，需要同时打开两个安全门：

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
ENABLE_TRADING=true
OKX_LIVE_API_KEY=your_live_api_key
OKX_LIVE_API_SECRET=your_live_api_secret
OKX_LIVE_API_PASSPHRASE=your_live_api_passphrase
```

然后运行：

```bash
uv run okx-ai-quant bot --mode live
```

实盘模式会使用真实资金下单。请使用无提现权限的 API Key，开启 IP 白名单，从极小仓位开始，并先确认 OKX 账户模式、保证金模式、持仓模式、交易对权限和下单数量单位。

### 单次决策

用于快速检查某个币种和策略：

```bash
uv run okx-ai-quant run-once \
  --symbol BTC-USDT-SWAP \
  --strategy ema-rsi-atr \
  --mode demo \
  --leverage 1
```

默认情况下，`run-once` 会遵循 `.env` 中的 `ENABLE_TRADING`。如果想让单次命令在风控通过后提交订单，请加 `--submit`。

### bot 会做什么？

- 扫描 `SYMBOLS` 中的所有交易对
- 查询 OKX 资产和真实 SWAP 合约持仓，并同步本地状态
- 持久化 K 线、信号、风控决策、分析、订单、成交、余额和报告
- 当已有 bot 挂单未完成时，跳过新开仓
- 避免在同一根 K 线上重复执行同一信号
- 查询已追踪订单、记录成交、更新持仓、取消过期 bot 挂单
- 开新仓前先监控本机器人记录的开放持仓
- 触发止损、止盈、反向信号、超时或风险退出时提交 reduce-only 平仓单
- 记录已平仓 PnL 和退出归因，方便复盘
- 通过配置的 notifier 推送报告

持仓对账会把 OKX 作为真实来源，同步真实合约仓位的方向、数量和均价。
如果本地已经有对应持仓，则会保留原来的止损、止盈、超时和入场信号信息。
系统只会同步 `SYMBOLS` 白名单里的合约，所以不会自动接管无关的手动仓位。

### 立即发送 AI 报告

```bash
uv run okx-ai-quant report-now --mode demo
```

### 交互式向导

```bash
uv run okx-ai-quant wizard
```

向导会让你选择：

1. 模拟盘或实盘
2. 策略
3. 交易对
4. 杠杆
5. 最后确认是否运行

### Telegram AI 报告

报告会包含当天指标和已存储的大模型交易分析。配置 Telegram 推送：

```dotenv
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
LLM_ENABLED=true
LLM_API_KEY=your_llm_key
```

立即发送：

```bash
uv run okx-ai-quant report-now --mode demo
```

持续 bot 会在 `REPORT_TIMES` 指定的时间自动推送报告。
每个时间点会独立记录发送状态，所以 `00:00,08:00,12:00,20:00`
每天最多可以发送四次。如果 bot 循环晚了几分钟醒来，会对错过的时间点补发一次，并记录该时间点已发送。

也可以让 Telegram 主动触发报告：

```bash
uv run okx-ai-quant telegram-listen --mode demo
```

支持的 Telegram 消息：

- `/report`、`/daily`、`/summary`
- `日报`、`报告`、`总结`、`分析`
- `/status`

`/status` 会优先显示持续 bot 写入的真实运行状态。如果还没有持续 bot
运行态，就回退显示 Telegram listener 自己读取到的 `.env` 配置。

---

## 支持的交易对

默认交易池聚焦 OKX 主流 USDT 永续合约：

```text
BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, XRP-USDT-SWAP,
DOGE-USDT-SWAP, ADA-USDT-SWAP, AVAX-USDT-SWAP, LINK-USDT-SWAP,
DOT-USDT-SWAP, LTC-USDT-SWAP, BCH-USDT-SWAP, BNB-USDT-SWAP,
TRX-USDT-SWAP, TON-USDT-SWAP, UNI-USDT-SWAP, AAVE-USDT-SWAP,
OP-USDT-SWAP, ARB-USDT-SWAP, NEAR-USDT-SWAP, ATOM-USDT-SWAP,
ETC-USDT-SWAP, FIL-USDT-SWAP, INJ-USDT-SWAP
```

可以在 `.env` 覆盖：

```dotenv
SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP
```

只有白名单里的交易对能通过风控。

---

## OKX 模拟盘和实盘 Key

OKX 的模拟盘 API Key 和实盘 API Key 是隔离的，本项目也按这个逻辑隔离配置：

| 模式 | OKX flag | 使用的凭证 | 安全门 |
| --- | --- | --- | --- |
| `demo` | `1` | 优先 `OKX_DEMO_*`，没有则回退旧版 `OKX_API_*` | 默认模式 |
| `live` | `0` | 只使用 `OKX_LIVE_*` | 必须 `ALLOW_LIVE_TRADING=true` |

如果你明确要走实盘路径：

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
OKX_LIVE_API_KEY=your_live_api_key
OKX_LIVE_API_SECRET=your_live_api_secret
OKX_LIVE_API_PASSPHRASE=your_live_api_passphrase
```

然后在 CLI 里选择 live：

```bash
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-rsi-atr --mode live
```

对于持续 bot，实盘下单还需要 `ENABLE_TRADING=true`：

```dotenv
TRADING_MODE=live
ALLOW_LIVE_TRADING=true
ENABLE_TRADING=true
```

```bash
uv run okx-ai-quant bot --mode live
```

实盘模式会使用真实资金下单。请使用无提现权限的 API Key，开启 IP 白名单，从极小仓位开始，并先确认 OKX 账户模式、保证金模式、交易对权限和下单数量单位。

---

## 大模型解读配置

策略和风控是确定性的。大模型只负责把交易信号和风控结果解释得更清楚，不参与直接决策。如果没有配置大模型 Key，或者服务商调用失败，项目会自动回退到本地确定性解释。

默认配置使用豆包 / 火山方舟的 OpenAI-compatible endpoint：

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=doubao
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=your_ark_api_key
# 或者设置 ARK_API_KEY=your_ark_api_key
LLM_MODEL=doubao-seed-2-0-pro-260215
```

最小 SDK 调用示例：

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
                {"type": "input_text", "text": "解释一下这个交易信号。"},
            ],
        }
    ],
)
print(response)
```

常见 OpenAI-compatible 服务商配置示例：

| 服务商 | `LLM_BASE_URL` | 示例模型 | API Key 环境变量 |
| --- | --- | --- | --- |
| 豆包 / 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-2-0-pro-260215` | `ARK_API_KEY` 或 `LLM_API_KEY` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | `OPENAI_API_KEY` 或 `LLM_API_KEY` |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | `DEEPSEEK_API_KEY` 或 `LLM_API_KEY` |
| 阿里千问 / 百炼 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `DASHSCOPE_API_KEY` 或 `LLM_API_KEY` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | `GEMINI_API_KEY` 或 `LLM_API_KEY` |
| Moonshot / Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | `MOONSHOT_API_KEY` 或 `LLM_API_KEY` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4/` | `glm-4-flash` | `ZHIPU_API_KEY` 或 `LLM_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4.1-mini` | `OPENROUTER_API_KEY` 或 `LLM_API_KEY` |

使用非豆包服务时，把对应服务商的 Key 填进 `LLM_API_KEY`，并修改 `LLM_BASE_URL` 和 `LLM_MODEL` 即可。

---

## CLI 示例

```bash
# 查看帮助
uv run okx-ai-quant --help

# 菜单式控制台
uv run okx-ai-quant menu

# 单次 bot 观察/交易周期
uv run okx-ai-quant bot --once --mode demo

# 持续 bot 循环
uv run okx-ai-quant bot --mode demo

# 立即发送今日 AI 报告
uv run okx-ai-quant report-now --mode demo

# 趋势跟随
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-rsi-atr

# 均值回归
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy rsi-bollinger-reversion

# Donchian 突破
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy donchian-breakout

# EMA 动量
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy ema-momentum

# 更严格的多周期趋势
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy multi-timeframe-trend

# 过滤 ATR 噪音后的动量
uv run okx-ai-quant run-once --symbol BTC-USDT-SWAP --strategy volatility-adjusted-momentum
```

---

## 安全设计

当前保护机制：

- 默认 `TRADING_MODE=demo`
- 默认 `ALLOW_LIVE_TRADING=false`
- 实盘需要显式开启
- 最大杠杆限制为 `2x`
- 默认杠杆为 `1x`
- 交易对白名单
- 非 `LONG` / `SHORT` 信号会被拒绝
- 日内亏损、连续亏损、最大持仓数量限制
- 实盘模式必须 `ALLOW_LIVE_TRADING=true` 且完整配置 `OKX_LIVE_*`

OKX 永续/交割合约的下单数量必须使用合约张数和交易所精度信息。当前 sizing 仍是 MVP 路径：请先在模拟盘验证，并在使用真实资金前确认 OKX `sz` 的精确语义。

---

## 常见问题

### `50113 Invalid Sign`

OKX 私有接口鉴权失败。公共行情可能仍然能读取。

请检查：

- demo key 是否搭配 `TRADING_MODE=demo`
- live key 是否只搭配 `TRADING_MODE=live`
- API secret 是否完整复制
- passphrase 是否完全正确
- `.env` 是否有多余空格
- demo 模式下 API key 是否创建于 OKX Demo Trading
- IP 白名单是否包含当前机器

### `51010 current account mode`

OKX 已经接受鉴权，但当前账户模式不允许这个订单请求。

请检查 OKX 账户设置，例如模拟盘/实盘、交易模式、保证金模式、衍生品权限和交易对可用性。

### 没有下单

这不一定是错误。常见原因：

- 策略输出 `HOLD`
- 预期波动低于成本阈值
- 风控拒绝
- OKX 账户模式拒绝订单
- live execution guard 阻断实盘执行

---

## 项目结构

```text
okx-ai-quant/
  src/okx_ai_quant/
    account.py       # OKX 余额解析
    analysis.py      # 中英双语交易解释
    bot.py           # 多币种持续 bot 循环
    cli.py           # CLI 入口、向导和菜单
    config.py        # .env 配置和实盘安全门
    cost.py          # 手续费、滑点、预期波动阈值
    execution.py     # OKX 下单封装
    indicators.py    # EMA / RSI / ATR
    llm.py           # 可选 OpenAI-compatible 解读客户端
    market_data.py   # OKX 行情标准化
    notifier.py      # console / Telegram / null 通知后端
    okx_client.py    # python-okx adapter
    reports.py       # 日报渲染
    risk.py          # 风控守卫
    runner.py        # 单周期交易编排
    storage.py       # SQLite 存储
    strategy.py      # 内置策略和策略工厂
  tests/
  README.md
  README.zh-CN.md
  RISK.md
  .env.example
```

---

## 路线图

近期：

- 增加只读 smoke-test 命令
- 增加历史回测
- 增加模拟交易报告和权益曲线
- 支持按策略配置参数
- 增强资金费率套利和配对交易策略
- 基于交易对元数据改进 OKX 下单 sizing

进行有意义的实盘交易前必须完成：

- 验证正确 OKX 合约张数换算
- 交易所精度和最小下单量检查
- 订单状态对账
- 持仓跟踪
- 一键熔断 / kill switch
- 告警系统
- 连续数周模拟盘 forward testing
- 独立代码和风控审查

---

## 贡献

欢迎提交 PR，尤其是测试、风控、回测、文档、更安全的执行逻辑和策略研究。

请尽量保持项目对新手友好。如果功能让系统更难理解，请写清楚文档并补充测试。

---

## 免责声明

本项目不构成投资建议，也不保证收益。加密货币交易风险极高。自动化系统可能因为策略假设错误、代码 bug、交易所故障、网络延迟、滑点、爆仓或配置错误而快速亏损。

请先使用模拟盘。除非你理解执行链路中的每一行代码，否则不要接入真实资金。
