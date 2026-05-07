from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from okx_ai_quant.models import BalanceSnapshot


@dataclass(frozen=True, kw_only=True)
class AccountSummary:
    total_equity_usdt: float
    currency_equity: dict[str, float] = field(default_factory=dict)
    currency_available: dict[str, float] = field(default_factory=dict)

    def balance_snapshots(self, updated_at: datetime | None = None) -> list[BalanceSnapshot]:
        timestamp = updated_at or datetime.now(UTC)
        currencies = sorted(set(self.currency_equity) | set(self.currency_available))
        return [
            BalanceSnapshot(
                currency=currency,
                available=self.currency_available.get(currency, 0.0),
                equity=self.currency_equity.get(currency, 0.0),
                updated_at=timestamp,
            )
            for currency in currencies
        ]


class AccountService:
    def __init__(self, client) -> None:
        self.client = client

    def fetch_summary(self) -> AccountSummary:
        return parse_balance_response(self.client.get_balance())


def parse_balance_response(response: dict[str, Any]) -> AccountSummary:
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return AccountSummary(total_equity_usdt=0.0)

    first = data[0]
    if not isinstance(first, dict):
        return AccountSummary(total_equity_usdt=0.0)

    total_equity = _float(first.get("totalEq"))
    currency_equity: dict[str, float] = {}
    currency_available: dict[str, float] = {}

    details = first.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            currency = str(detail.get("ccy") or "").upper()
            if not currency:
                continue
            equity = _float(detail.get("eq") or detail.get("cashBal") or detail.get("bal"))
            available = _float(detail.get("availEq") or detail.get("availBal") or detail.get("availBal"))
            currency_equity[currency] = currency_equity.get(currency, 0.0) + equity
            currency_available[currency] = currency_available.get(currency, 0.0) + available

    if total_equity <= 0:
        total_equity = currency_equity.get("USDT", 0.0)

    return AccountSummary(
        total_equity_usdt=total_equity,
        currency_equity=currency_equity,
        currency_available=currency_available,
    )


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
