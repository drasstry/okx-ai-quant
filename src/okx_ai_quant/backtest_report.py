"""Render backtest results as a self-contained HTML report + stdout table."""

from __future__ import annotations

from datetime import datetime
from html import escape

from okx_ai_quant.backtest import BacktestConfig, StrategyResult


def render_backtest_summary(results: list[StrategyResult]) -> str:
    header = (
        f"{'策略':<34}{'总收益':>10}{'最大回撤':>10}{'交易数':>8}"
        f"{'胜率':>8}{'盈亏比':>8}{'费用':>12}"
    )
    lines = [header, "-" * len(header)]
    for result in sorted(results, key=lambda item: item.total_return, reverse=True):
        stats = result.stats()
        profit_factor = stats["profit_factor"]
        pf_text = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        lines.append(
            f"{stats['strategy']:<34}"
            f"{stats['total_return']:>9.2%}"
            f"{stats['max_drawdown']:>9.2%}"
            f"{stats['trades']:>8}"
            f"{stats['win_rate']:>7.1%}"
            f"{pf_text:>8}"
            f"{stats['total_fees']:>11.2f}"
        )
    return "\n".join(lines)


def render_backtest_html(
    results: list[StrategyResult],
    *,
    config: BacktestConfig,
    start: datetime,
    end: datetime,
) -> str:
    ordered = sorted(results, key=lambda item: item.total_return, reverse=True)
    rows = "".join(_stats_row(result) for result in ordered)
    sections = "".join(_strategy_section(result) for result in ordered)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OKX AI Quant 回测报告</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 2rem auto;
         max-width: 1000px; padding: 0 1rem; color: #1a1a2e; background: #fafafa; }}
  h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 2.2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: 0.45rem 0.6rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ background: #f0f0f5; }}
  .meta {{ color: #555; font-size: 0.9rem; line-height: 1.6; }}
  .pos {{ color: #0a7a4b; }} .neg {{ color: #c0392b; }}
  .chart {{ margin: 0.8rem 0; overflow-x: auto; }}
  .assumptions li {{ margin: 0.25rem 0; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14141c; color: #e8e8ee; }}
    th {{ background: #22222e; }} th, td {{ border-color: #333; }}
    .meta {{ color: #aaa; }}
  }}
</style>
</head>
<body>
<h1>OKX AI Quant 回测报告</h1>
<p class="meta">
回测区间：{start.date().isoformat()} → {end.date().isoformat()}（UTC，1H 级逐根回放）<br>
初始资金：{config.initial_capital_usdt:,.0f} USDT ｜ 币种：{len(config.symbols)} 个 ｜
单笔风险 {config.max_risk_per_trade:.1%} ｜ 日亏损熔断 {config.max_daily_loss:.1%} ｜
最大持仓 {config.max_open_positions} ｜ 持仓超时 {config.position_timeout_hours}h<br>
成本假设：taker 费率 {config.fee_rate_per_side:.4%}/边 ＋ 滑点 {config.slippage_rate:.4%}/边 ＋ 实际资金费率
</p>

<h2>策略对比</h2>
<div class="chart"><table>
<tr><th>策略</th><th>最终权益</th><th>总收益</th><th>最大回撤</th><th>交易数</th>
<th>胜率</th><th>盈亏比</th><th>总费用</th><th>资金费</th></tr>
{rows}
</table></div>

{sections}

<h2>假设与口径</h2>
<ul class="assumptions">
<li>信号只使用已收盘 K 线（与修复后的实盘代码一致），在信号 K 线收盘价成交（实盘为收盘后一个轮询周期内的市价单）。</li>
<li>止损/止盈按交易所侧触发单模拟：用每根 K 线最高/最低价判断是否触发，同一根 K 线内止损与止盈都触发时按止损优先（保守）。</li>
<li>触发后按触发价加滑点的市价成交；超时/反向信号/风控平仓按收盘价加滑点成交。</li>
<li>已实现盈亏口径与实盘一致（价格盈亏，不含费用）用于日亏损/连亏熔断；权益曲线则包含全部费用与资金费。</li>
<li>不模拟合约张数取整与保证金占用（1x 名义本金记账）；同币种最多一笔持仓。</li>
<li>历史回测无法重现实盘的下单失败、部分成交与网络中断。回测结果不代表未来收益。</li>
</ul>
</body>
</html>
"""


def _stats_row(result: StrategyResult) -> str:
    stats = result.stats()
    profit_factor = stats["profit_factor"]
    pf_text = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    return (
        "<tr>"
        f"<td>{escape(str(stats['strategy']))}</td>"
        f"<td>{stats['final_equity']:,.0f}</td>"
        f"<td class=\"{_sign_class(stats['total_return'])}\">{stats['total_return']:.2%}</td>"
        f"<td class=\"neg\">{stats['max_drawdown']:.2%}</td>"
        f"<td>{stats['trades']}</td>"
        f"<td>{stats['win_rate']:.1%}</td>"
        f"<td>{pf_text}</td>"
        f"<td>{stats['total_fees']:,.0f}</td>"
        f"<td class=\"{_sign_class(stats['total_funding'])}\">{stats['total_funding']:,.0f}</td>"
        "</tr>"
    )


def _strategy_section(result: StrategyResult) -> str:
    stats = result.stats()
    reasons = stats["exit_reasons"]
    reason_text = "、".join(f"{name} ×{count}" for name, count in sorted(reasons.items())) or "无平仓"
    closed = [trade for trade in result.trades if trade.closed_at is not None]
    worst = sorted(closed, key=lambda trade: trade.net_pnl)[:3]
    worst_rows = "".join(
        "<tr>"
        f"<td>{escape(trade.symbol)} {trade.direction.value}</td>"
        f"<td>{trade.opened_at.date().isoformat()}</td>"
        f"<td>{escape(trade.exit_reason or '-')}</td>"
        f"<td class=\"neg\">{trade.net_pnl:,.0f}</td>"
        "</tr>"
        for trade in worst
    )
    worst_table = (
        "<table><tr><th>最差交易</th><th>开仓日</th><th>平仓原因</th><th>净盈亏</th></tr>"
        f"{worst_rows}</table>"
        if worst_rows
        else ""
    )
    halted_at = stats.get("halted_at")
    halted_text = (
        f" ｜ ⛔ {halted_at.date().isoformat()} 触发回撤熔断后停止交易" if halted_at else ""
    )
    return (
        f"<h2>{escape(result.strategy)}</h2>"
        f"<p class=\"meta\">平仓原因：{escape(reason_text)} ｜ 平均盈利 {stats['avg_win']:,.0f} ｜ "
        f"平均亏损 {stats['avg_loss']:,.0f}{halted_text}</p>"
        f"<div class=\"chart\">{_equity_svg(result)}</div>"
        f"<div class=\"chart\">{worst_table}</div>"
    )


def _equity_svg(result: StrategyResult, *, width: int = 920, height: int = 220) -> str:
    curve = result.equity_curve
    if len(curve) < 2:
        return "<p class=\"meta\">数据不足，无权益曲线。</p>"
    values = [equity for _, equity in curve]
    low = min(values + [result.initial_capital])
    high = max(values + [result.initial_capital])
    if high <= low:
        high = low + 1.0
    pad = 12
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def x(index: int) -> float:
        return pad + plot_w * index / (len(curve) - 1)

    def y(value: float) -> float:
        return pad + plot_h * (1 - (value - low) / (high - low))

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(curve))
    baseline = y(result.initial_capital)
    color = "#0a7a4b" if values[-1] >= result.initial_capital else "#c0392b"
    start_label = curve[0][0].date().isoformat()
    end_label = curve[-1][0].date().isoformat()
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{escape(result.strategy)} 权益曲线">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="none"/>'
        f'<line x1="{pad}" y1="{baseline:.1f}" x2="{width - pad}" y2="{baseline:.1f}" '
        f'stroke="#999" stroke-dasharray="4 4" stroke-width="1"/>'
        f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
        f'<text x="{pad}" y="{height - 2}" font-size="11" fill="#888">{start_label}</text>'
        f'<text x="{width - pad}" y="{height - 2}" font-size="11" fill="#888" '
        f'text-anchor="end">{end_label}</text>'
        f'<text x="{pad}" y="{pad + 4}" font-size="11" fill="#888">高 {high:,.0f}</text>'
        f'<text x="{pad}" y="{height - pad + 4}" font-size="11" fill="#888">低 {low:,.0f}</text>'
        "</svg>"
    )


def _sign_class(value: float) -> str:
    return "pos" if value >= 0 else "neg"
