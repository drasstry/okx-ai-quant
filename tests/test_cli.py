import tomllib
from pathlib import Path

import pytest

import okx_ai_quant.cli as cli
from okx_ai_quant.cli import main


def test_cli_help_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    assert "OKX AI Quant" in capsys.readouterr().out


def test_cli_run_once_wires_runner_with_mocks(monkeypatch):
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run_once(self, symbol):
            self.calls.append(symbol)

    fake_runner = FakeRunner()
    captured = {}

    def fake_build_runner(**kwargs):
        captured.update(kwargs)
        return fake_runner

    monkeypatch.setattr(cli, "build_runner", fake_build_runner)

    exit_code = main(
        [
            "run-once",
            "--symbol",
            "BTC-USDT-SWAP",
            "--strategy",
            "donchian-breakout",
            "--mode",
            "demo",
            "--leverage",
            "2",
        ]
    )

    assert exit_code == 0
    assert fake_runner.calls == ["BTC-USDT-SWAP"]
    assert captured == {
        "mode": "demo",
        "strategy_name": "donchian-breakout",
        "symbol": "BTC-USDT-SWAP",
        "leverage": 2,
        "enable_trading": None,
    }


def test_cli_run_once_submit_forces_trading_with_mocks(monkeypatch):
    class FakeRunner:
        def run_once(self, symbol):
            self.symbol = symbol

    fake_runner = FakeRunner()
    captured = {}

    def fake_build_runner(**kwargs):
        captured.update(kwargs)
        return fake_runner

    monkeypatch.setattr(cli, "build_runner", fake_build_runner)

    exit_code = main(["run-once", "--symbol", "BTC-USDT-SWAP", "--submit"])

    assert exit_code == 0
    assert captured["enable_trading"] is True


def test_cli_wizard_prompts_for_mode_strategy_symbol_leverage_and_runs(monkeypatch):
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run_once(self, symbol):
            self.calls.append(symbol)

    fake_runner = FakeRunner()
    captured = {}
    answers = iter(["demo", "ema-momentum", "ETH-USDT-SWAP", "1", "yes"])

    def fake_build_runner(**kwargs):
        captured.update(kwargs)
        return fake_runner

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "build_runner", fake_build_runner)

    exit_code = main(["wizard"])

    assert exit_code == 0
    assert fake_runner.calls == ["ETH-USDT-SWAP"]
    assert captured == {
        "mode": "demo",
        "strategy_name": "ema-momentum",
        "symbol": "ETH-USDT-SWAP",
        "leverage": 1,
    }


def test_okx_sdk_dependency_is_declared_for_run_once():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert any(dependency.startswith("python-okx") for dependency in dependencies)


def test_cli_bot_once_wires_bot_with_mocks(monkeypatch):
    class FakeRunner:
        def close(self):
            self.closed = True

    class FakeBot:
        def __init__(self):
            self.runner = FakeRunner()
            self.calls = []

        def run_once(self):
            self.calls.append("once")

    fake_bot = FakeBot()
    captured = {}

    def fake_build_bot(**kwargs):
        captured.update(kwargs)
        return fake_bot

    monkeypatch.setattr(cli, "build_bot", fake_build_bot)

    exit_code = main(["bot", "--once", "--mode", "demo"])

    assert exit_code == 0
    assert fake_bot.calls == ["once"]
    assert captured == {"mode": "demo"}


def test_cli_menu_quits_after_showing_config(monkeypatch, capsys):
    answers = iter(["en", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    exit_code = main(["menu"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "OKX AI Quant menu" in output
    assert "strategy: ema-rsi-atr" in output
    assert "BTC-USDT-SWAP" in output


def test_cli_menu_can_show_chinese(monkeypatch, capsys):
    answers = iter(["zh", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    exit_code = main(["menu"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "OKX AI Quant 菜单" in output
    assert "策略: ema-rsi-atr" in output


def test_cli_menu_shows_runtime_summary_before_continuous_bot(monkeypatch, capsys):
    class FakeRunner:
        def close(self):
            self.closed = True

    class FakeBot:
        def __init__(self):
            self.runner = FakeRunner()
            self.calls = []

        def run_forever(self):
            self.calls.append("forever")

    fake_bot = FakeBot()
    answers = iter(
        [
            "en",
            "4",
            "demo",
            "yes",
            "ema-momentum",
            "BTC-USDT-SWAP,ETH-USDT-SWAP",
            "60",
            "08:00,20:00",
            "none",
            "2",
            "5000",
            "0.005",
            "0.03",
            "120",
            "yes",
        ]
    )
    captured = {}

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "build_bot", lambda **kwargs: captured.update(kwargs) or fake_bot)

    exit_code = main(["menu"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Starting continuous bot with:" in output
    assert "strategy: ema-momentum" in output
    assert "trading enabled: True" in output
    assert fake_bot.calls == ["forever"]
    assert captured["settings_overrides"] == {
        "TRADING_MODE": "demo",
        "ENABLE_TRADING": True,
        "STRATEGY_NAME": "ema-momentum",
        "SYMBOLS": "BTC-USDT-SWAP,ETH-USDT-SWAP",
        "POLL_INTERVAL_SECONDS": 60,
        "REPORT_TIMES": "08:00,20:00",
        "NOTIFIER": "none",
        "MAX_LEVERAGE": 2,
        "REFERENCE_CAPITAL_USDT": 5000.0,
        "MAX_RISK_PER_TRADE": 0.005,
        "MAX_DAILY_LOSS": 0.03,
        "ORDER_STALE_SECONDS": 120,
    }


def test_cli_menu_can_run_one_bot_cycle(monkeypatch):
    class FakeRunner:
        def close(self):
            self.closed = True

    class FakeBot:
        def __init__(self):
            self.runner = FakeRunner()
            self.calls = []

        def run_once(self):
            self.calls.append("once")

    fake_bot = FakeBot()
    answers = iter(["en", "1", "q"])

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "build_bot", lambda **_kwargs: fake_bot)

    exit_code = main(["menu"])

    assert exit_code == 0
    assert fake_bot.calls == ["once"]
