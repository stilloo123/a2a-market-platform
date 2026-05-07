from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BidRecord:
    task_id: str
    market_url: str
    game_id: str
    game_name: str
    bet_amount: float
    payout: float
    trader_won: bool
    verified: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TraderLedger:
    def __init__(self, seed_balance: float):
        self.seed_balance = seed_balance
        self.balance = seed_balance
        self.bets: list[BidRecord] = []
        self.blacklisted_markets: set[str] = set()

    def record_bid(self, record: BidRecord) -> None:
        self.balance += record.payout - record.bet_amount
        self.bets.append(record)

    def blacklist(self, market_url: str) -> None:
        self.blacklisted_markets.add(market_url)

    def is_blacklisted(self, market_url: str) -> bool:
        return market_url in self.blacklisted_markets

    def profit(self) -> float:
        return round(self.balance - self.seed_balance, 4)

    def roi(self) -> float:
        if self.seed_balance == 0:
            return 0.0
        return round(self.profit() / self.seed_balance * 100, 2)

    def total_wagered(self) -> float:
        return sum(b.bet_amount for b in self.bets)

    def total_paid_out(self) -> float:
        return sum(b.payout for b in self.bets)

    def recent_losses(self, n: int = 3) -> int:
        recent = self.bets[-n:]
        return sum(1 for b in recent if not b.trader_won)
