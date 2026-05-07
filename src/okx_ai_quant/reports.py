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
