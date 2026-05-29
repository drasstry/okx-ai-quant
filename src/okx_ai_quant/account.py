from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from okx_ai_quant.models import BalanceSnapshot, PositionRecord, PositionState, SignalDirection


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

    def fetch_positions(self) -> list[PositionRecord]:
        get_positions = getattr(self.client, "get_positions", None)
        if not callable(get_positions):
            return []
        return parse_positions_response(get_positions(inst_type="SWAP"))


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


def parse_positions_response(response: dict[str, Any]) -> list[PositionRecord]:
    rows = response.get("data")
    if not isinstance(rows, list):
        return []

    positions: list[PositionRecord] = []
    now = datetime.now(UTC)
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("instId") or "")
        if not symbol:
            continue

        quantity = _float(row.get("pos"))
        if quantity == 0:
            continue

        side = _position_side(row, quantity)
        signed_quantity = abs(quantity) if side == SignalDirection.LONG else -abs(quantity)
        average_entry = _float(row.get("avgPx") or row.get("openAvgPx"))
        if average_entry <= 0:
            average_entry = _float(row.get("markPx") or row.get("last") or row.get("lastPx"))

        updated_at = _datetime_from_ms(row.get("uTime") or row.get("cTime")) or now
        positions.append(
            PositionRecord(
                symbol=symbol,
                side=side,
                quantity=signed_quantity,
                average_entry=average_entry,
                state=PositionState.OPEN,
                opened_at=updated_at,
                updated_at=updated_at,
                margin_mode=str(row.get("mgnMode") or "") or None,
                leverage=_int_or_none(row.get("lever")),
            )
        )
    return positions


def _position_side(row: dict[str, Any], quantity: float) -> SignalDirection:
    pos_side = str(row.get("posSide") or "").lower()
    if pos_side == "short":
        return SignalDirection.SHORT
    if pos_side == "long":
        return SignalDirection.LONG
    return SignalDirection.LONG if quantity > 0 else SignalDirection.SHORT


def _datetime_from_ms(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
