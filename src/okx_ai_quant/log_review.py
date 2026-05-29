from collections import Counter
from pathlib import Path
from typing import Iterable


_CATEGORY_LABELS = {
    "telegram_polling_failed": "Telegram 长轮询失败，通常是网络/代理 SSL 或超时问题。",
    "telegram_send_failed": "Telegram 发送失败，报告可能没有送达。",
    "okx_reconcile_failed": "OKX 持仓同步失败，本地仓位与交易所仓位可能短暂不一致。",
    "okx_symbol_failed": "单币种行情/策略循环失败，多见于 OKX API SSL EOF 或握手超时。",
    "okx_api_error": "OKX API 返回业务错误，需要检查参数、账户模式或权限。",
    "order_failure": "订单提交或平仓失败，需要优先复核是否影响真实持仓。",
    "traceback": "程序出现异常堆栈，说明存在未覆盖的崩溃路径。",
    "other_failure": "其他失败日志，需要人工复核。",
}


def summarize_recent_failures(
    log_paths: Iterable[str | Path],
    *,
    max_lines_per_file: int = 600,
    max_samples: int = 8,
) -> dict[str, object]:
    """Summarize recent operational failures from bot/listener logs."""
    categories: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    samples: list[str] = []
    scanned_files: list[str] = []
    scanned_lines = 0

    for path in log_paths:
        log_path = Path(path)
        if not log_path.exists() or not log_path.is_file():
            continue
        scanned_files.append(str(log_path))
        lines = _tail_lines(log_path, max_lines_per_file)
        scanned_lines += len(lines)
        for line in lines:
            category = _failure_category(line)
            if category is None:
                continue
            categories[category] += 1
            symbol = _extract_symbol(line)
            if symbol:
                symbols[symbol] += 1
            if len(samples) < max_samples:
                samples.append(_compact_line(line))

    ordered_categories = [
        {
            "name": name,
            "count": count,
            "summary": _CATEGORY_LABELS.get(name, name),
        }
        for name, count in categories.most_common()
    ]
    return {
        "total_failures": sum(categories.values()),
        "categories": ordered_categories,
        "symbol_failures": [
            {"symbol": symbol, "count": count} for symbol, count in symbols.most_common(8)
        ],
        "samples": samples,
        "scanned_files": scanned_files,
        "scanned_lines": scanned_lines,
    }


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-max_lines:]


def _failure_category(line: str) -> str | None:
    lower = line.lower()
    if "telegram polling failed" in lower:
        return "telegram_polling_failed"
    if "telegram" in lower and ("send failed" in lower or "notification" in lower):
        return "telegram_send_failed"
    if "could not reconcile okx positions" in lower:
        return "okx_reconcile_failed"
    if "traceback" in lower:
        return "traceback"
    if "okx api error" in lower:
        return "okx_api_error"
    if "order" in lower and ("failed" in lower or "error" in lower):
        return "order_failure"
    if "usdt-swap failed:" in lower:
        return "okx_symbol_failed"
    if "failed" in lower or "error" in lower or "timed out" in lower:
        return "other_failure"
    return None


def _extract_symbol(line: str) -> str | None:
    for token in line.replace(",", " ").split():
        cleaned = token.strip(":[]()")
        if cleaned.endswith("-USDT-SWAP"):
            return cleaned
    return None


def _compact_line(line: str, *, limit: int = 220) -> str:
    compact = " ".join(line.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
