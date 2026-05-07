from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class BidRecord:
    task_id: str
    game_id: str
    game_name: str
    bet_amount: float
    payout: float
    trader_won: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MarketLedger:
    def __init__(self, seed_balance: float):
        self.seed_balance = seed_balance
        self.balance = seed_balance
        self.bets: list[BidRecord] = []
        self.last_bet_at: Optional[datetime] = None

    def record_bid(self, record: BidRecord) -> None:
        self.balance += record.bet_amount - record.payout
        self.bets.append(record)
        self.last_bet_at = record.timestamp

    def profit(self) -> float:
        return round(self.balance - self.seed_balance, 4)

    def total_wagered(self) -> float:
        return sum(b.bet_amount for b in self.bets)

    def total_paid_out(self) -> float:
        return sum(b.payout for b in self.bets)

    def minutes_since_last_bet(self) -> float:
        if self.last_bet_at is None:
            return float("inf")
        delta = datetime.now(timezone.utc) - self.last_bet_at
        return delta.total_seconds() / 60

    def per_game_stats(self) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for b in self.bets:
            s = stats.setdefault(b.game_id, {
                "game_name": b.game_name,
                "total_bets": 0,
                "total_wagered": 0.0,
                "total_paid_out": 0.0,
            })
            s["total_bets"] += 1
            s["total_wagered"] += b.bet_amount
            s["total_paid_out"] += b.payout
        for s in stats.values():
            s["profit"] = round(s["total_wagered"] - s["total_paid_out"], 4)
        return stats
