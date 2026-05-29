from datetime import date

from okx_ai_quant.models import TradeAnalysis
from okx_ai_quant.reports import render_daily_report, render_trade_overview


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


def test_render_trade_overview_is_compact_and_includes_llm_summary():
    report = render_trade_overview(
        report_date=date(2026, 5, 6),
        report_slot="08:00",
        context={
            "generated_at": "2026-05-06T08:00:00+08:00",
            "account": {"equity_usdt": "1000.00", "available_usdt": "900.00"},
            "totals": {
                "open_position_count": 1,
                "unrealized_pnl_usdt": "12.30",
                "realized_pnl_usdt": "-4.00",
            },
            "orders": {"filled": 2, "canceled": 1, "submitted": 0},
            "failure_review": {
                "categories": [
                    {
                        "summary": "OKX 持仓同步失败，本地仓位与交易所仓位可能短暂不一致。",
                        "count": 3,
                    }
                ],
                "samples": ["Could not reconcile OKX positions: SSL EOF"],
                "symbol_failures": [{"symbol": "BTC-USDT-SWAP", "count": 2}],
            },
            "open_positions": [
                {
                    "symbol": "BTC-USDT-SWAP",
                    "side": "LONG",
                    "quantity": "1",
                    "entry_price": "80000",
                    "mark_price": "80500",
                    "unrealized_pnl_usdt": "5",
                    "stop_loss": "79000",
                    "take_profit": "82000",
                }
            ],
            "closed_positions": [],
            "risk_points": ["当前持仓接近止损"],
        },
        llm_summary="大模型摘要：当前风险可控。",
    )

    assert "# OKX 交易概览 - 2026-05-06 08:00" in report
    assert "大模型摘要：当前风险可控。" in report
    assert "BTC-USDT-SWAP LONG" in report
    assert "当前持仓接近止损" in report
    assert "失败日志复盘" in report
    assert "OKX 持仓同步失败" in report
