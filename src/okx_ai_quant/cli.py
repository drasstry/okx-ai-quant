import argparse
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from okx_ai_quant.analysis import TradeAnalysisGenerator
from okx_ai_quant.bot import TradingBot
from okx_ai_quant.config import Settings, TradingMode
from okx_ai_quant.cost import CostModel
from okx_ai_quant.execution import ExecutionEngine
from okx_ai_quant.llm import OpenAICompatibleLLMClient
from okx_ai_quant.notifier import NotificationError, TelegramBotClient, build_notifier
from okx_ai_quant.okx_client import OkxClient
from okx_ai_quant.risk import RiskGuard
from okx_ai_quant.runner import Runner
from okx_ai_quant.storage import SQLiteStorage
from okx_ai_quant.strategy import AVAILABLE_STRATEGIES, create_strategy


def build_runner(
    *,
    mode: str | None = None,
    strategy_name: str | None = None,
    symbol: str | None = None,
    leverage: int | None = None,
    enable_trading: bool | None = None,
    settings_overrides: dict | None = None,
) -> Runner:
    overrides = dict(settings_overrides or {})
    if mode is not None:
        overrides["TRADING_MODE"] = mode
    if leverage is not None:
        overrides["MAX_LEVERAGE"] = leverage
    if enable_trading is not None:
        overrides["ENABLE_TRADING"] = enable_trading

    settings = Settings(**overrides)
    storage = SQLiteStorage(settings.DB_PATH)
    storage.initialize()

    client = OkxClient.from_settings(settings)
    cost_model = CostModel(
        fee_rate_per_side=settings.FEE_RATE_PER_SIDE,
        slippage_rate=settings.SLIPPAGE_RATE,
        min_expected_move=settings.MIN_EXPECTED_MOVE,
    )
    selected_strategy = strategy_name or settings.STRATEGY_NAME
    strategy = create_strategy(
        selected_strategy,
        min_expected_move=max(
            settings.MIN_EXPECTED_MOVE,
            cost_model.round_trip_cost_rate(),
        ),
    )
    symbols = [symbol] if symbol is not None else settings.symbols
    risk_guard = RiskGuard(
        symbols=symbols,
        reference_capital_usdt=settings.REFERENCE_CAPITAL_USDT,
        max_risk_per_trade=settings.MAX_RISK_PER_TRADE,
        max_daily_loss=settings.MAX_DAILY_LOSS,
        max_consecutive_losses=settings.MAX_CONSECUTIVE_LOSSES,
        max_positions=settings.MAX_OPEN_POSITIONS,
        leverage=settings.MAX_LEVERAGE,
    )

    return Runner(
        client=client,
        storage=storage,
        strategy=strategy,
        risk_guard=risk_guard,
        execution_engine=ExecutionEngine(client, margin_mode=settings.MARGIN_MODE),
        analysis_generator=TradeAnalysisGenerator(
            llm_client=OpenAICompatibleLLMClient.from_settings(settings)
        ),
        candle_limit=settings.MARKET_CANDLE_LIMIT,
        enable_trading=settings.ENABLE_TRADING,
    )


def build_bot(*, mode: str | None = None, settings_overrides: dict | None = None) -> TradingBot:
    overrides = dict(settings_overrides or {})
    if mode is not None:
        overrides["TRADING_MODE"] = mode
    settings = Settings(**overrides)
    runner = build_runner(
        settings_overrides=overrides,
        strategy_name=settings.STRATEGY_NAME,
        enable_trading=False,
    )
    return TradingBot(settings=settings, runner=runner, notifier=build_notifier(settings))


def run_telegram_listener(*, mode: str | None = None, timeout: int = 25) -> None:
    settings_overrides = {"NOTIFIER": "telegram"}
    if mode is not None:
        settings_overrides["TRADING_MODE"] = mode
    bot = build_bot(settings_overrides=settings_overrides)
    client = TelegramBotClient(
        bot_token=bot.settings.TELEGRAM_BOT_TOKEN,
        chat_id=bot.settings.TELEGRAM_CHAT_ID,
        proxy_url=bot.settings.TELEGRAM_PROXY_URL,
    )
    consecutive_failures = 0
    retry_delay = 5
    try:
        raw_offset = bot.runner.storage.get_state("telegram:last_update_id")
        offset = int(raw_offset) + 1 if raw_offset is not None else None
        print("OKX AI Quant Telegram listener started. Commands: /overview, /report, 概览, 总结, /status.")
        while True:
            try:
                updates = client.get_updates(offset=offset, timeout=timeout)
            except NotificationError as exc:
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 20 == 0:
                    print(
                        f"{exc} (consecutive={consecutive_failures}, "
                        f"next_retry={retry_delay}s)"
                    )
                time.sleep(retry_delay)
                retry_delay = min(300, retry_delay * 2)
                continue
            if consecutive_failures:
                print(f"Telegram polling recovered after {consecutive_failures} failure(s).")
            consecutive_failures = 0
            retry_delay = 5

            for update in updates:
                offset = update.update_id + 1
                bot.runner.storage.set_state(
                    "telegram:last_update_id",
                    str(update.update_id),
                    datetime.now(UTC),
                )
                _handle_telegram_text(bot, client, update.text)
    finally:
        _close_if_possible(bot.runner)


def _handle_telegram_text(bot: TradingBot, client: TelegramBotClient, text: str) -> None:
    normalized = text.strip().lower()
    command = normalized.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    if command in {"/overview", "/report", "/daily", "/summary"} or any(
        keyword in normalized for keyword in ("概览", "日报", "报告", "总结", "分析")
    ):
        bot.send_daily_report(force=True)
        return
    if command == "/status":
        client.send(_telegram_status_message(bot))
        return
    client.send("Supported commands: /overview, /report, 概览, 总结, /status")


def _telegram_status_message(bot: TradingBot) -> str:
    runtime_mode = bot.runner.storage.get_state("runtime:mode")
    runtime_trading = bot.runner.storage.get_state("runtime:trading_enabled")
    runtime_strategy = bot.runner.storage.get_state("runtime:strategy")
    runtime_symbols = bot.runner.storage.get_state("runtime:symbols")
    runtime_updated_at = bot.runner.storage.get_state("runtime:updated_at")

    if runtime_mode is not None:
        return (
            "OKX AI Quant status\n"
            "source=active bot runtime\n"
            f"mode={runtime_mode}\n"
            f"trading={runtime_trading}\n"
            f"strategy={runtime_strategy}\n"
            f"symbols={runtime_symbols}\n"
            f"updated_at={runtime_updated_at}\n"
            f"listener_trading={bot.settings.ENABLE_TRADING}"
        )

    return (
        "OKX AI Quant status\n"
        "source=telegram listener config\n"
        f"mode={bot.settings.TRADING_MODE}\n"
        f"trading={bot.settings.ENABLE_TRADING}\n"
        f"strategy={bot.settings.STRATEGY_NAME}\n"
        f"symbols={', '.join(bot.settings.symbols)}"
    )


def _prompt_choice(prompt: str, choices: Sequence[str], *, default: str | None = None) -> str:
    rendered_choices = "/".join(choices)
    while True:
        suffix = f" [{rendered_choices}]"
        if default is not None:
            suffix += f" default={default}"
        answer = input(f"{prompt}{suffix}: ").strip()
        if not answer and default is not None:
            return default
        if answer in choices:
            return answer
        print(f"Please choose one of: {', '.join(choices)}")


def _prompt_text(prompt: str, *, default: str | None = None) -> str:
    while True:
        suffix = f" [default={default}]" if default is not None else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("Please enter a value.")


def _prompt_int(prompt: str, *, default: int, minimum: int = 1, maximum: int = 2) -> int:
    while True:
        answer = input(f"{prompt} [{minimum}-{maximum}, default={default}]: ").strip()
        if not answer:
            return default
        try:
            value = int(answer)
        except ValueError:
            print("Please enter an integer.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Please enter an integer between {minimum} and {maximum}.")


def _prompt_float(
    prompt: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    while True:
        answer = input(f"{prompt} [{minimum:g}-{maximum:g}, default={default:g}]: ").strip()
        if not answer:
            return default
        try:
            value = float(answer)
        except ValueError:
            print("Please enter a number.")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Please enter a number between {minimum:g} and {maximum:g}.")


def _confirm(prompt: str, *, default: bool = False) -> bool:
    default_text = "yes" if default else "no"
    while True:
        answer = input(f"{prompt} [yes/no, default={default_text}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def _confirm_localized(prompt: str, *, default: bool = False, language: str = "en") -> bool:
    if language == "zh":
        default_text = "是" if default else "否"
        while True:
            answer = input(f"{prompt} [是/否，默认={default_text}]: ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes", "是", "对", "确认"}:
                return True
            if answer in {"n", "no", "否", "不", "取消"}:
                return False
            print("请输入 是 或 否。")
    return _confirm(prompt, default=default)


def run_wizard() -> int:
    settings = Settings()
    print("OKX AI Quant interactive wizard")
    mode = _prompt_choice(
        "Trading mode",
        [TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=settings.trading_mode.value,
    )
    strategy_name = _prompt_choice(
        "Strategy",
        AVAILABLE_STRATEGIES,
        default=AVAILABLE_STRATEGIES[0],
    )
    default_symbol = settings.symbols[0] if settings.symbols else "BTC-USDT-SWAP"
    symbol = _prompt_text("Symbol", default=default_symbol)
    leverage = _prompt_int("Leverage", default=settings.MAX_LEVERAGE, minimum=1, maximum=125)

    print(
        "\nSummary:\n"
        f"  mode: {mode}\n"
        f"  strategy: {strategy_name}\n"
        f"  symbol: {symbol}\n"
        f"  leverage: {leverage}x\n"
    )
    if mode == TradingMode.LIVE.value:
        print(
            "WARNING: live mode uses real OKX funds. It requires ALLOW_LIVE_TRADING=true "
            "and OKX_LIVE_* credentials."
        )
    if not _confirm("Run one strategy cycle now?", default=False):
        print("Canceled.")
        return 0

    runner = build_runner(
        mode=mode,
        strategy_name=strategy_name,
        symbol=symbol,
        leverage=leverage,
    )
    try:
        runner.run_once(symbol)
    finally:
        _close_if_possible(runner)
    return 0


def run_menu() -> int:
    language = _prompt_choice("Language / 语言", ["zh", "en"], default="zh")
    while True:
        settings = Settings()
        _print_menu_header(settings, language)
        print("")
        if language == "zh":
            print("1. 运行一轮观察/交易机器人")
            print("2. 立即发送 AI 报告")
            print("3. 运行一次自定义策略")
            print("4. 启动连续机器人")
            print("q. 退出")
            prompt = "请选择操作 [1/2/3/4/q]: "
        else:
            print("1. Run one observe/trading bot cycle")
            print("2. Send AI report now")
            print("3. Run one custom strategy cycle")
            print("4. Start continuous bot")
            print("q. Quit")
            prompt = "Choose an action [1/2/3/4/q]: "

        choice = input(prompt).strip().lower()
        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "1":
            bot = build_bot()
            try:
                bot.run_once()
            finally:
                _close_if_possible(bot.runner)
            continue
        if choice == "2":
            bot = build_bot()
            try:
                bot.send_daily_report(force=True)
            finally:
                _close_if_possible(bot.runner)
            continue
        if choice == "3":
            run_wizard()
            continue
        if choice == "4":
            overrides = _prompt_continuous_bot_overrides(settings, language=language)
            if overrides is None:
                continue
            try:
                runtime_settings = Settings(**overrides)
            except ValueError as exc:
                print(f"配置错误: {exc}" if language == "zh" else f"Configuration error: {exc}")
                continue
            print(_runtime_summary(runtime_settings, language=language))
            if not _confirm_localized(
                "使用以上配置启动连续机器人？"
                if language == "zh"
                else "Start continuous bot with these settings?",
                default=False,
                language=language,
            ):
                continue
            bot = build_bot(settings_overrides=overrides)
            try:
                bot.run_forever()
            finally:
                _close_if_possible(bot.runner)
            return 0
        print("请选择 1、2、3、4 或 q。" if language == "zh" else "Please choose 1, 2, 3, 4, or q.")


def _print_menu_header(settings: Settings, language: str) -> None:
    if language == "zh":
        print("\nOKX AI Quant 菜单")
        print(f"  模式: {settings.TRADING_MODE}")
        print(f"  是否提交订单: {settings.ENABLE_TRADING}")
        print(f"  策略: {settings.STRATEGY_NAME}")
        print(f"  轮询间隔: {settings.POLL_INTERVAL_SECONDS}s")
        print(f"  报告时间: {', '.join(settings.report_times)}")
        print(f"  币种: {', '.join(settings.symbols)}")
        print(f"  通知: {settings.NOTIFIER}")
        return

    print("\nOKX AI Quant menu")
    print(f"  mode: {settings.TRADING_MODE}")
    print(f"  trading enabled: {settings.ENABLE_TRADING}")
    print(f"  strategy: {settings.STRATEGY_NAME}")
    print(f"  poll interval: {settings.POLL_INTERVAL_SECONDS}s")
    print(f"  report times: {', '.join(settings.report_times)}")
    print(f"  symbols: {', '.join(settings.symbols)}")
    print(f"  notifier: {settings.NOTIFIER}")


def _prompt_continuous_bot_overrides(settings: Settings, *, language: str = "en") -> dict | None:
    print("\n配置连续机器人运行参数" if language == "zh" else "\nConfigure continuous bot runtime")
    mode = _prompt_choice(
        "交易模式" if language == "zh" else "Trading mode",
        [TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=settings.trading_mode.value,
    )
    enable_trading = _confirm_localized(
        "启用真实下单" if language == "zh" else "Enable order submission",
        default=settings.ENABLE_TRADING,
        language=language,
    )
    strategy_name = _prompt_choice(
        "策略" if language == "zh" else "Strategy",
        AVAILABLE_STRATEGIES,
        default=settings.STRATEGY_NAME if settings.STRATEGY_NAME in AVAILABLE_STRATEGIES else AVAILABLE_STRATEGIES[0],
    )
    symbols = _prompt_text("币种 CSV" if language == "zh" else "Symbols CSV", default=",".join(settings.symbols))
    poll_interval = _prompt_int(
        "轮询间隔秒数" if language == "zh" else "Poll interval seconds",
        default=settings.POLL_INTERVAL_SECONDS,
        minimum=5,
        maximum=86_400,
    )
    report_times = _prompt_text("报告时间 CSV" if language == "zh" else "Report times CSV", default=settings.REPORT_TIMES)
    notifier = _prompt_choice(
        "通知方式" if language == "zh" else "Notifier",
        ["console", "telegram", "none"],
        default=settings.NOTIFIER if settings.NOTIFIER in {"console", "telegram", "none"} else "console",
    )
    leverage = _prompt_int(
        "最大杠杆" if language == "zh" else "Max leverage",
        default=settings.MAX_LEVERAGE,
        minimum=1,
        maximum=125,
    )
    margin_mode = _prompt_choice(
        "保证金模式" if language == "zh" else "Margin mode",
        ["cross", "isolated"],
        default=str(settings.MARGIN_MODE),
    )
    max_open_positions = _prompt_int(
        "最大同时持仓数" if language == "zh" else "Max open positions",
        default=settings.MAX_OPEN_POSITIONS,
        minimum=1,
        maximum=50,
    )
    reference_capital = _prompt_float(
        "参考资金 USDT" if language == "zh" else "Reference capital USDT",
        default=settings.REFERENCE_CAPITAL_USDT,
        minimum=1.0,
        maximum=1_000_000_000.0,
    )
    max_risk = _prompt_float(
        "单笔最大风险" if language == "zh" else "Max risk per trade",
        default=settings.MAX_RISK_PER_TRADE,
        minimum=0.0001,
        maximum=0.02,
    )
    max_daily_loss = _prompt_float(
        "日内最大亏损" if language == "zh" else "Max daily loss",
        default=settings.MAX_DAILY_LOSS,
        minimum=0.0001,
        maximum=0.10,
    )
    stale_seconds = _prompt_int(
        "超时订单撤单秒数" if language == "zh" else "Stale order cancel seconds",
        default=settings.ORDER_STALE_SECONDS,
        minimum=30,
        maximum=86_400,
    )
    manage_positions = _confirm_localized(
        "启用持仓监控和平仓" if language == "zh" else "Enable position monitoring and exits",
        default=settings.MANAGE_EXISTING_POSITIONS,
        language=language,
    )
    position_timeout_hours = _prompt_int(
        "持仓超时小时数" if language == "zh" else "Position timeout hours",
        default=settings.POSITION_TIMEOUT_HOURS,
        minimum=1,
        maximum=24 * 30,
    )
    exit_on_reverse_signal = _confirm_localized(
        "反向信号触发平仓" if language == "zh" else "Exit on reverse signal",
        default=settings.EXIT_ON_REVERSE_SIGNAL,
        language=language,
    )

    if mode == TradingMode.LIVE.value and enable_trading:
        print(
            "警告：实盘模式会使用真实资金下单。"
            if language == "zh"
            else "WARNING: live trading can place real orders with real funds."
        )
        live_confirmation = input(
            "输入 LIVE 确认实盘下单: "
            if language == "zh"
            else "Type LIVE to confirm live order submission: "
        ).strip()
        if live_confirmation != "LIVE":
            print("已取消实盘机器人启动。" if language == "zh" else "Canceled live bot startup.")
            return None

    if notifier == "telegram" and (
        not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID
    ):
        print(
            "已选择 Telegram。请确认 .env 已配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。"
            if language == "zh"
            else "Telegram selected. Make sure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env."
        )

    return {
        "TRADING_MODE": mode,
        "ENABLE_TRADING": enable_trading,
        "STRATEGY_NAME": strategy_name,
        "SYMBOLS": symbols,
        "POLL_INTERVAL_SECONDS": poll_interval,
        "REPORT_TIMES": report_times,
        "NOTIFIER": notifier,
        "MAX_LEVERAGE": leverage,
        "MARGIN_MODE": margin_mode,
        "MAX_OPEN_POSITIONS": max_open_positions,
        "REFERENCE_CAPITAL_USDT": reference_capital,
        "MAX_RISK_PER_TRADE": max_risk,
        "MAX_DAILY_LOSS": max_daily_loss,
        "ORDER_STALE_SECONDS": stale_seconds,
        "MANAGE_EXISTING_POSITIONS": manage_positions,
        "POSITION_TIMEOUT_HOURS": position_timeout_hours,
        "EXIT_ON_REVERSE_SIGNAL": exit_on_reverse_signal,
    }


def _runtime_summary(settings: Settings, *, language: str = "en") -> str:
    if language == "zh":
        return (
            "\n即将使用以下配置启动连续机器人:\n"
            f"  模式: {settings.TRADING_MODE}\n"
            f"  是否提交订单: {settings.ENABLE_TRADING}\n"
            f"  策略: {settings.STRATEGY_NAME}\n"
            f"  轮询间隔: {settings.POLL_INTERVAL_SECONDS}s\n"
            f"  报告时间: {', '.join(settings.report_times)}\n"
            f"  最大杠杆: {settings.MAX_LEVERAGE}x\n"
            f"  保证金模式: {settings.MARGIN_MODE}\n"
            f"  最大同时持仓数: {settings.MAX_OPEN_POSITIONS}\n"
            f"  参考资金: {settings.REFERENCE_CAPITAL_USDT:g} USDT\n"
            f"  单笔最大风险: {settings.MAX_RISK_PER_TRADE:.2%}\n"
            f"  日内最大亏损: {settings.MAX_DAILY_LOSS:.2%}\n"
            f"  超时撤单: {settings.ORDER_STALE_SECONDS}s\n"
            f"  持仓监控: {settings.MANAGE_EXISTING_POSITIONS}\n"
            f"  持仓超时: {settings.POSITION_TIMEOUT_HOURS}h\n"
            f"  反向信号平仓: {settings.EXIT_ON_REVERSE_SIGNAL}\n"
            f"  币种: {', '.join(settings.symbols)}\n"
            f"  通知: {settings.NOTIFIER}\n"
        )

    return (
        "\nStarting continuous bot with:\n"
        f"  mode: {settings.TRADING_MODE}\n"
        f"  trading enabled: {settings.ENABLE_TRADING}\n"
        f"  strategy: {settings.STRATEGY_NAME}\n"
        f"  poll interval: {settings.POLL_INTERVAL_SECONDS}s\n"
        f"  report times: {', '.join(settings.report_times)}\n"
        f"  max leverage: {settings.MAX_LEVERAGE}x\n"
        f"  margin mode: {settings.MARGIN_MODE}\n"
        f"  max open positions: {settings.MAX_OPEN_POSITIONS}\n"
        f"  reference capital: {settings.REFERENCE_CAPITAL_USDT:g} USDT\n"
        f"  max risk per trade: {settings.MAX_RISK_PER_TRADE:.2%}\n"
        f"  max daily loss: {settings.MAX_DAILY_LOSS:.2%}\n"
        f"  stale order cancel: {settings.ORDER_STALE_SECONDS}s\n"
        f"  position monitoring: {settings.MANAGE_EXISTING_POSITIONS}\n"
        f"  position timeout: {settings.POSITION_TIMEOUT_HOURS}h\n"
        f"  exit on reverse signal: {settings.EXIT_ON_REVERSE_SIGNAL}\n"
        f"  symbols: {', '.join(settings.symbols)}\n"
        f"  notifier: {settings.NOTIFIER}\n"
    )


def _close_if_possible(runner) -> None:
    close = getattr(runner, "close", None)
    if callable(close):
        close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX AI Quant MVP")
    subparsers = parser.add_subparsers(dest="command")

    run_once = subparsers.add_parser("run-once", help="Fetch data and run one strategy cycle")
    run_once.add_argument("--symbol", required=True, help="OKX instrument ID, for example BTC-USDT-SWAP")
    run_once.add_argument(
        "--strategy",
        choices=AVAILABLE_STRATEGIES,
        default=AVAILABLE_STRATEGIES[0],
        help="Strategy to run",
    )
    run_once.add_argument(
        "--mode",
        choices=[TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=None,
        help="Override trading mode for this run",
    )
    run_once.add_argument(
        "--leverage",
        type=int,
        default=None,
        help="Override leverage for this run",
    )
    run_once.add_argument(
        "--submit",
        action="store_true",
        help="Submit an order if risk approves. Without this flag, ENABLE_TRADING controls submission.",
    )

    bot = subparsers.add_parser("bot", help="Run the long-lived multi-symbol trading bot")
    bot.add_argument(
        "--mode",
        choices=[TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=None,
        help="Override trading mode for the bot",
    )
    bot.add_argument("--once", action="store_true", help="Run one bot cycle and exit")

    report = subparsers.add_parser("report-now", help="Render and send today's report now")
    report.add_argument(
        "--mode",
        choices=[TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=None,
        help="Override trading mode for report generation",
    )

    telegram = subparsers.add_parser(
        "telegram-listen",
        help="Listen for Telegram commands such as /report and /status",
    )
    telegram.add_argument(
        "--mode",
        choices=[TradingMode.DEMO.value, TradingMode.LIVE.value],
        default=None,
        help="Override trading mode for Telegram-triggered reports",
    )
    telegram.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="Telegram long-poll timeout in seconds",
    )

    subparsers.add_parser("wizard", help="Choose mode, strategy, symbol, and leverage interactively")
    subparsers.add_parser("menu", help="Open the interactive bot control menu")

    backtest = subparsers.add_parser(
        "backtest",
        help="Replay strategies over OKX history and write an HTML report",
    )
    backtest.add_argument("--days", type=int, default=180, help="Lookback window in days")
    backtest.add_argument("--symbols", default="", help="CSV of instIds; defaults to SYMBOLS")
    backtest.add_argument(
        "--strategies",
        default="all",
        help="CSV of strategy names, or 'all'",
    )
    backtest.add_argument("--capital", type=float, default=80_000.0, help="Initial capital USDT")
    backtest.add_argument("--fee", type=float, default=0.0005, help="Taker fee per side")
    backtest.add_argument("--slippage", type=float, default=0.0005, help="Slippage per side")
    backtest.add_argument("--data-dir", default="data/backtest", help="Candle cache directory")
    backtest.add_argument("--out", default="backtest_report.html", help="HTML report path")
    backtest.add_argument(
        "--offline",
        action="store_true",
        help="Use only cached data; do not call the OKX API",
    )

    args = parser.parse_args(argv)
    if args.command == "run-once":
        runner = build_runner(
            mode=args.mode,
            strategy_name=args.strategy,
            symbol=args.symbol,
            leverage=args.leverage,
            enable_trading=True if args.submit else None,
        )
        try:
            runner.run_once(args.symbol)
        finally:
            _close_if_possible(runner)
    elif args.command == "bot":
        bot = build_bot(mode=args.mode)
        try:
            if args.once:
                bot.run_once()
            else:
                bot.run_forever()
        finally:
            _close_if_possible(bot.runner)
    elif args.command == "report-now":
        bot = build_bot(mode=args.mode)
        try:
            bot.send_daily_report(force=True)
        finally:
            _close_if_possible(bot.runner)
    elif args.command == "telegram-listen":
        run_telegram_listener(mode=args.mode, timeout=args.timeout)
    elif args.command == "backtest":
        from okx_ai_quant.backtest import run_backtest_command

        return run_backtest_command(args)
    elif args.command == "wizard":
        return run_wizard()
    elif args.command == "menu":
        return run_menu()
    return 0
