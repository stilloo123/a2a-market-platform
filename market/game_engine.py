import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from shared.crypto import commit, generate_server_seed, resolve_run, verify
from shared.models import BidResult, GameRun, GameSpec


@dataclass
class PendingBid:
    task_id: str
    run_id: str
    game_id: str
    outcome_index: int
    bet_amount: float


class GameEngine:
    def __init__(self):
        self._runs: dict[str, GameRun] = {}                  # run_id → GameRun
        self._seeds: dict[str, str] = {}                     # run_id → server_seed (secret until resolution)
        self._bids: dict[str, list[PendingBid]] = {}         # run_id → [bids]
        self._results: dict[str, BidResult] = {}             # task_id → result
        self._task_to_run: dict[str, str] = {}               # task_id → run_id
        self._game_current_run: dict[str, str] = {}          # game_id → active run_id
        self._run_traders: dict[str, set[str]] = {}          # run_id → {trader_url} already bid
        self._reserved: float = 0.0                          # total pending exposure not yet resolved

    def pending_exposure(self) -> float:
        """Sum of all accepted bids not yet resolved. Deduct from balance before risk checks."""
        return self._reserved

    def create_run(self, game: GameSpec) -> GameRun:
        """Create the next scheduled run for a game. Commits server_seed immediately."""
        server_seed = generate_server_seed()
        server_seed_hash = commit(server_seed)
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        scheduled_at = now + timedelta(seconds=game.schedule_interval_seconds)
        bet_open_at = scheduled_at - timedelta(seconds=game.bet_window_seconds)

        run = GameRun(
            run_id=run_id,
            game_id=game.game_id,
            game_name=game.name,
            scheduled_at=scheduled_at,
            bet_open_at=bet_open_at,
            status="scheduled",
            server_seed_hash=server_seed_hash,
        )
        self._runs[run_id] = run
        self._seeds[run_id] = server_seed
        self._bids[run_id] = []
        self._run_traders[run_id] = set()
        self._game_current_run[game.game_id] = run_id
        return run

    def open_run(self, run_id: str) -> None:
        if run_id in self._runs:
            self._runs[run_id].status = "open"

    def close_run(self, run_id: str) -> None:
        if run_id in self._runs:
            self._runs[run_id].status = "closed"

    def place_bid(self, run_id: str, outcome_index: int, bet_amount: float, trader_url: str) -> str:
        """Record a bid on an open run. Returns task_id. Updates reserved exposure immediately."""
        run = self._runs.get(run_id)
        if run is None or run.status != "open":
            raise ValueError(f"Run {run_id} is not open for bidding")
        if trader_url in self._run_traders.get(run_id, set()):
            raise ValueError(f"Trader {trader_url} already bid on run {run_id}")
        task_id = str(uuid.uuid4())
        bid = PendingBid(
            task_id=task_id,
            run_id=run_id,
            game_id=run.game_id,
            outcome_index=outcome_index,
            bet_amount=bet_amount,
        )
        self._bids[run_id].append(bid)
        self._run_traders[run_id].add(trader_url)
        self._task_to_run[task_id] = run_id
        self._reserved += bet_amount
        run.total_bets += 1
        run.total_wagered = round(run.total_wagered + bet_amount, 4)
        return task_id

    def resolve_run(self, run_id: str, game: GameSpec) -> list[BidResult]:
        """Resolve all bids for a run. Reveals server_seed and pays out winners."""
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")

        server_seed = self._seeds.get(run_id)
        if server_seed is None:
            raise ValueError(f"No server_seed for run {run_id}")

        # Verify our own commitment (sanity check)
        assert verify(server_seed, run.server_seed_hash), "Internal commitment mismatch"

        outcome_index = resolve_run(server_seed, len(game.outcomes))
        outcome = game.outcomes[outcome_index]

        # Update run with revealed info
        run.status = "resolved"
        run.outcome_index = outcome_index
        run.server_seed = server_seed

        results: list[BidResult] = []
        for bid in self._bids.get(run_id, []):
            self._reserved = max(0.0, self._reserved - bid.bet_amount)  # release reservation
            trader_won = bid.outcome_index == outcome_index
            payout = round(bid.bet_amount * outcome.payout_multiplier, 4) if trader_won else 0.0
            result = BidResult(
                task_id=bid.task_id,
                run_id=run_id,
                game_id=bid.game_id,
                pending=False,
                outcome_index=outcome_index,
                server_seed=server_seed,
                trader_won=trader_won,
                bet_amount=bid.bet_amount,
                payout=payout,
            )
            self._results[bid.task_id] = result
            results.append(result)

        # Free bulk per-run storage — individual results remain queryable via self._results.
        # _task_to_run entries stay so get_result() can route unknown task_ids correctly.
        self._bids.pop(run_id, None)
        self._seeds.pop(run_id, None)
        self._run_traders.pop(run_id, None)
        self._runs.pop(run_id, None)

        return results

    def get_result(self, task_id: str) -> BidResult | None:
        """Return resolved result, a pending placeholder, or None if unknown."""
        if task_id in self._results:
            return self._results[task_id]
        run_id = self._task_to_run.get(task_id)
        if run_id is None:
            return None
        run = self._runs.get(run_id)
        if run is None:
            return None
        # Find the bid to get bet_amount
        bid = next((b for b in self._bids.get(run_id, []) if b.task_id == task_id), None)
        if bid is None:
            return None
        return BidResult(
            task_id=task_id,
            run_id=run_id,
            game_id=run.game_id,
            pending=True,
            bet_amount=bid.bet_amount,
        )

    def current_run(self, game_id: str) -> GameRun | None:
        run_id = self._game_current_run.get(game_id)
        if run_id is None:
            return None
        return self._runs.get(run_id)

    def run_outcome_totals(self, run_id: str) -> dict[int, float]:
        """Return total wagered per outcome_index for a run."""
        totals: dict[int, float] = {}
        for bid in self._bids.get(run_id, []):
            totals[bid.outcome_index] = round(totals.get(bid.outcome_index, 0.0) + bid.bet_amount, 4)
        return totals

    def upcoming_runs(self, game_id: str, n: int = 2) -> list[GameRun]:
        """Return next n non-resolved runs for a game (for AgentCard)."""
        run = self.current_run(game_id)
        if run is None or run.status == "resolved":
            return []
        return [run]
