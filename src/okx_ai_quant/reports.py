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
    """Render a compact Telegram-friendly trading overview."""
    generated_at = context.get("generated_at", "-")
    account = context.get("account", {})
    totals = context.get("totals", {})
    orders = context.get("orders", {})
    open_positions = list(context.get("open_positions") or [])
    closed_positions = list(context.get("closed_positions") or [])
    risks = list(context.get("risk_points") or [])
    failure_review = context.get("failure_review", {})
    failure_categories = list(failure_review.get("categories") or []) if failure_review else []
    failure_samples = list(failure_review.get("samples") or []) if failure_review else []
    symbol_failures = list(failure_review.get("symbol_failures") or []) if failure_review else []

    title_slot = f" {report_slot}" if report_slot else ""
    lines = [
        f"# OKX 交易概览 - {report_date.isoformat()}{title_slot}",
        f"生成时间: {generated_at}",
        "",
        "## 大模型总结",
        "",
        (llm_summary or build_trade_overview_fallback(context)).strip(),
        "",
        "## 关键数据",
        "",
        f"- 账户权益: {account.get('equity_usdt', '-')} USDT",
        f"- 可用余额: {account.get('available_usdt', '-')} USDT",
        f"- 当前持仓数: {totals.get('open_position_count', 0)}",
        f"- 估算未实现盈亏: {totals.get('unrealized_pnl_usdt', '0.00')} USDT",
        f"- 今日已平仓盈亏: {totals.get('realized_pnl_usdt', '0.00')} USDT",
        f"- 今日订单: filled={orders.get('filled', 0)}, canceled={orders.get('canceled', 0)}, submitted={orders.get('submitted', 0)}",
        f"- 杠杆/保证金: {totals.get('max_leverage', '-')}x / {totals.get('margin_mode', '-')}",
    ]

    lines.extend(["", "## 当前持仓", ""])
    if open_positions:
        for item in open_positions[:12]:
            values = dict(item)
            values.setdefault("margin_mode", "-")
            values.setdefault("leverage", "-")
            lines.append(
                "- {symbol} {side} {margin_mode}/{leverage}x qty={quantity} entry={entry_price} "
                "mark={mark_price} uPnL={unrealized_pnl_usdt} stop={stop_loss} target={take_profit}".format(**values)
            )
        if len(open_positions) > 12:
            lines.append(f"- 其余 {len(open_positions) - 12} 个持仓已省略")
    else:
        lines.append("- 暂无本地记录的开放持仓")

    lines.extend(["", "## 今日平仓", ""])
    if closed_positions:
        for item in closed_positions[:8]:
            lines.append(
                "- {symbol} {side} {reason} qty={quantity} entry={entry_price} "
                "exit={exit_price} PnL={realized_pnl_usdt}".format(**item)
            )
        if len(closed_positions) > 8:
            lines.append(f"- 其余 {len(closed_positions) - 8} 条平仓记录已省略")
    else:
        lines.append("- 今日暂无平仓记录")

    lines.extend(["", "## 风险点", ""])
    if risks:
        lines.extend(f"- {risk}" for risk in risks[:8])
    else:
        lines.append("- 暂无额外风险提示")

    lines.extend(["", "## 失败日志复盘", ""])
    if failure_categories:
        for item in failure_categories[:6]:
            lines.append(f"- {item.get('summary', item.get('name'))} count={item.get('count', 0)}")
        if symbol_failures:
            hot_symbols = ", ".join(
                f"{item.get('symbol')}({item.get('count')})" for item in symbol_failures[:5]
            )
            lines.append(f"- 受影响较多的合约: {hot_symbols}")
        if failure_samples:
            lines.append(f"- 样例: {failure_samples[0]}")
    else:
        lines.append("- 最近日志未发现失败记录")

    return "\n".join(lines).rstrip() + "\n"


def build_trade_overview_fallback(context: dict[str, Any]) -> str:
    totals = context.get("totals", {})
    account = context.get("account", {})
    open_positions = list(context.get("open_positions") or [])
    risk_points = list(context.get("risk_points") or [])
    pnl = totals.get("unrealized_pnl_usdt", "0.00")
    realized = totals.get("realized_pnl_usdt", "0.00")
    equity = account.get("equity_usdt", "-")
    available = account.get("available_usdt", "-")
    if open_positions:
        position_text = "；".join(
            f"{item['symbol']} {item['side']} 未实现盈亏 {item['unrealized_pnl_usdt']} USDT"
            for item in open_positions[:5]
        )
    else:
        position_text = "当前没有本地记录的开放持仓"
    risk_text = "；".join(risk_points[:5]) if risk_points else "未发现额外风险提示"
    failure_review = context.get("failure_review", {})
    failure_categories = list(failure_review.get("categories") or []) if failure_review else []
    if failure_categories:
        failure_text = "；".join(
            f"{item.get('summary', item.get('name'))} count={item.get('count', 0)}"
            for item in failure_categories[:3]
        )
    else:
        failure_text = "最近日志未发现失败记录"
    return (
        f"账户权益约 {equity} USDT，可用余额约 {available} USDT。"
        f"当前开放持仓 {totals.get('open_position_count', 0)} 个，"
        f"估算未实现盈亏 {pnl} USDT，今日已平仓盈亏 {realized} USDT。"
        f"持仓概况：{position_text}。风险点：{risk_text}。"
        f"失败日志复盘：{failure_text}。"
        "以上为系统记录和行情估算，不构成投资建议。"
    )
