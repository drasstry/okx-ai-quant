from datetime import date

from okx_ai_quant.models import TradeAnalysis
from okx_ai_quant.reports import render_daily_report


def test_render_daily_report_includes_metrics():
    report = render_daily_report(
        date(2026, 5, 6),
        {
            "signals": 3,
            "approved": 1,
            "rejected": 2,
            "daily_pnl_usdt": -12.5,
        },
    )

    assert "# Daily Report / 日报 - 2026-05-06" in report
    assert "| signals | 3 |" in report
    assert "| approved | 1 |" in report
    assert "| rejected | 2 |" in report
    assert "| daily_pnl_usdt | -12.5 |" in report


def test_render_daily_report_includes_bilingual_analysis():
    analysis = TradeAnalysis(
        signal_id=42,
        english="BTC setup was rejected because risk limits were hit.",
        chinese="BTC 信号被拒绝，因为触发了风控限制。",
        created_at=date(2026, 5, 6),
    )

    report = render_daily_report(date(2026, 5, 6), {}, analyses=[analysis])

    assert "## Trade Analyses / 交易分析" in report
    assert "### Signal 42" in report
    assert "**English**" in report
    assert "BTC setup was rejected because risk limits were hit." in report
    assert "**中文**" in report
    assert "BTC 信号被拒绝，因为触发了风控限制。" in report
