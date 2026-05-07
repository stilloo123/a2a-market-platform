from datetime import datetime, timezone

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from observer.rankings import network_summary, rank_markets, rank_traders
from shared.models import StatsResponse


def _market_table(markets: list[StatsResponse]) -> Table:
    t = Table(title="Top Markets", header_style="bold magenta")
    t.add_column("#", width=3)
    t.add_column("Name")
    t.add_column("Balance", justify="right")
    t.add_column("Profit", justify="right")
    t.add_column("Games", justify="right")
    t.add_column("Bets", justify="right")

    for i, s in enumerate(markets[:10], 1):
        profit_str = f"+${s.profit:.2f}" if s.profit >= 0 else f"-${abs(s.profit):.2f}"
        profit_color = "green" if s.profit >= 0 else "red"
        t.add_row(
            str(i),
            s.name,
            f"${s.balance:.2f}",
            Text(profit_str, style=profit_color),
            str(s.active_games),
            str(s.total_bets),
        )
    return t


def _trader_table(traders: list[StatsResponse]) -> Table:
    t = Table(title="Top Traders", header_style="bold cyan")
    t.add_column("#", width=3)
    t.add_column("Name")
    t.add_column("Balance", justify="right")
    t.add_column("Start", justify="right")
    t.add_column("Profit", justify="right")
    t.add_column("ROI", justify="right")
    t.add_column("Bets", justify="right")

    for i, s in enumerate(traders[:10], 1):
        roi = (s.profit / s.seed_balance * 100) if s.seed_balance else 0
        profit_str = f"+${s.profit:.2f}" if s.profit >= 0 else f"-${abs(s.profit):.2f}"
        roi_str = f"{roi:+.1f}%"
        color = "green" if s.profit >= 0 else "red"
        t.add_row(
            str(i),
            s.name,
            f"${s.balance:.2f}",
            f"${s.seed_balance:.2f}",
            Text(profit_str, style=color),
            Text(roi_str, style=color),
            str(s.total_bets),
        )
    return t


def render(all_stats: list[StatsResponse]) -> Layout:
    summary = network_summary(all_stats)
    markets = rank_markets(all_stats)
    traders = rank_traders(all_stats)

    header = (
        f"[bold]A2A MARKET NETWORK[/bold]  "
        f"Markets: [magenta]{summary['total_markets']}[/magenta]  "
        f"Traders: [cyan]{summary['total_traders']}[/cyan]  "
        f"Volume: [yellow]${summary['total_volume']:.2f}[/yellow]  "
        f"Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

    layout = Layout()
    layout.split_column(
        Layout(Panel(header, border_style="dim"), size=3),
        Layout(name="tables"),
    )
    layout["tables"].split_row(
        Layout(Panel(_market_table(markets))),
        Layout(Panel(_trader_table(traders))),
    )
    return layout


def run_live(get_stats_fn, refresh_seconds: int = 30):
    console = Console()
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            import asyncio
            all_stats = asyncio.get_event_loop().run_until_complete(get_stats_fn())
            live.update(render(all_stats))
            import time
            time.sleep(refresh_seconds)
