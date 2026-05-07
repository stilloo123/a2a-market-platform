from shared.models import AgentType, StatsResponse


def rank_markets(stats: list[StatsResponse]) -> list[StatsResponse]:
    markets = [s for s in stats if s.type == AgentType.market]
    return sorted(markets, key=lambda s: s.profit, reverse=True)


def rank_traders(stats: list[StatsResponse]) -> list[StatsResponse]:
    traders = [s for s in stats if s.type == AgentType.trader]
    return sorted(traders, key=lambda s: s.profit / s.seed_balance if s.seed_balance else 0, reverse=True)


def network_summary(stats: list[StatsResponse]) -> dict:
    markets = [s for s in stats if s.type == AgentType.market]
    traders = [s for s in stats if s.type == AgentType.trader]
    return {
        "total_markets": len(markets),
        "total_traders": len(traders),
        "total_volume": round(sum(s.total_wagered for s in traders), 2),
        "total_payouts": round(sum(s.total_paid_out for s in traders), 2),
        "total_house_profit": round(sum(s.profit for s in markets), 2),
        "total_trader_profit": round(sum(s.profit for s in traders), 2),
    }
