from datetime import date
from typing import Any, Iterable


def render_daily_report(
    report_date: date,
    metrics: dict[str, Any],
    analyses: Iterable[Any] | None = None,
) -> str:
    lines = [
        f"# Daily Report / 日报 - {report_date.isoformat()}",
        "",
        "## Metrics / 指标",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]

    if metrics:
        for name, value in metrics.items():
            lines.append(f"| {name} | {value} |")
    else:
        lines.append("| No metrics / 暂无指标 | - |")

    analysis_list = list(analyses or [])
    if analysis_list:
        lines.extend(["", "## Trade Analyses / 交易分析", ""])
        for analysis in analysis_list:
            signal_id = getattr(analysis, "signal_id", "unknown")
            english = getattr(analysis, "english", "")
            chinese = getattr(analysis, "chinese", "")
            lines.extend(
                [
                    f"### Signal {signal_id}",
                    "",
                    "**English**",
                    "",
                    str(english),
                    "",
                    "**中文**",
                    "",
                    str(chinese),
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def render_trade_overview(
    *,
    report_date: date,
    report_slot: str | None,
    context: dict[str, Any],
    llm_summary: str | None = None,
) -> str:
    """Render a compact, plain-text trading overview for Telegram.

    Telegram's default sendMessage renders plain text, so no Markdown
    headers. The key account numbers lead; detail sections are tightly
    capped so the reader sees PnL and risk without scrolling.
    """
    account = context.get("account", {})
    totals = context.get("totals", {})
    orders = context.get("orders", {})
    open_positions = list(context.get("open_positions") or [])
    closed_positions = list(context.get("closed_positions") or [])
    risks = list(context.get("risk_points") or [])
    failure_review = context.get("failure_review", {})
    failure_categories = list(failure_review.get("categories") or []) if failure_review else []

    title_slot = f" {report_slot}" if report_slot else ""
    mode = context.get("mode", "-")
    strategy = context.get("strategy", "-")
    lines = [
        f"📊 OKX 交易日报 {report_date.isoformat()}{title_slot} | {mode} | {strategy}",
    ]
    halted = str(totals.get("halted") or "")
    if halted:
        lines.append(f"⛔ 已停机：{halted}（发送 /resume 恢复）")
    lines.extend([
        "",
        f"权益 {account.get('equity_usdt', '-')} USDT（可用 {account.get('available_usdt', '-')}）",
    ])
    drawdown = str(totals.get("drawdown") or "")
    if drawdown and drawdown != "-":
        lines.append(f"回撤 {drawdown}")
    lines.extend([
        f"今日已实现盈亏 {totals.get('realized_pnl_usdt', '0.00')} USDT | "
        f"持仓浮动盈亏 {totals.get('unrealized_pnl_usdt', '0.00')} USDT",
        f"持仓 {totals.get('open_position_count', 0)} 个 | 今日订单 成交 {orders.get('filled', 0)} / "
        f"撤销 {orders.get('canceled', 0)} / 未结 {orders.get('submitted', 0)}",
    ])

    lines.extend(["", "📈 当前持仓"])
    if open_positions:
        for item in open_positions[:8]:
            values = dict(item)
            lines.append(
                "· {symbol} {side} {quantity} @{entry_price} → {mark_price} "
                "盈亏 {unrealized_pnl_usdt} | SL {stop_loss} TP {take_profit}".format(**values)
            )
        if len(open_positions) > 8:
            lines.append(f"· 其余 {len(open_positions) - 8} 个持仓已省略")
    else:
        lines.append("· 无持仓")

    if closed_positions:
        lines.extend(["", "✅ 今日平仓"])
        for item in closed_positions[:5]:
            lines.append(
                "· {symbol} {side} {reason} 盈亏 {realized_pnl_usdt}".format(**item)
            )
        if len(closed_positions) > 5:
            lines.append(f"· 其余 {len(closed_positions) - 5} 条已省略")

    if risks:
        lines.extend(["", "⚠️ 风险点"])
        lines.extend(f"· {risk}" for risk in risks[:3])

    if failure_categories:
        lines.extend(["", "🔧 失败日志复盘"])
        for item in failure_categories[:2]:
            lines.append(f"· {item.get('summary', item.get('name'))} ×{item.get('count', 0)}")

    summary = (llm_summary or build_trade_overview_fallback(context)).strip()
    if summary:
        lines.extend(["", "🤖 AI 点评", summary])

    return "\n".join(lines).rstrip() + "\n"


def build_trade_overview_fallback(context: dict[str, Any]) -> str:
    """Short deterministic digest; the report body already lists the numbers."""
    totals = context.get("totals", {})
    risk_points = list(context.get("risk_points") or [])
    realized = totals.get("realized_pnl_usdt", "0.00")
    unrealized = totals.get("unrealized_pnl_usdt", "0.00")
    risk_text = risk_points[0] if risk_points else "暂无额外风险提示"
    return (
        f"今日已实现盈亏 {realized} USDT，持仓浮动盈亏 {unrealized} USDT。"
        f"最需要关注：{risk_text}"
    )
