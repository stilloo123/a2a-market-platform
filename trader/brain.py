from __future__ import annotations

import json
from abc import ABC, abstractmethod

from shared.llm import complete
from shared.models import GameSpec


class Brain(ABC):
    def __init__(self, agents_md: str, model: str, api_base: str | None = None, api_key: str | None = None):
        self.system_prompt = agents_md
        self.model = model
        self.api_base = api_base
        self.api_key = api_key

    @abstractmethod
    def select_plays(
        self,
        schedule: list[dict],
        balance: float,
        pending_exposure: float,
        recent_losses: int,
    ) -> list[tuple[str, str, int, float, str]]:
        """Return list of (market_url, run_id, outcome_index, bet_amount, reasoning) or []."""
        ...


class DefaultTraderBrain(Brain):

    def select_plays(
        self,
        schedule: list[dict],
        balance: float,
        pending_exposure: float,
        recent_losses: int,
    ) -> list[tuple[str, str, int, float, str]]:
        if not schedule:
            return []

        prompt = (
            f"Your balance: ${balance:.2f}\n"
            f"Tied up in unresolved bids: ${pending_exposure:.2f}\n"
            f"Free to bid now: ${balance - pending_exposure:.2f}\n"
            f"Recent consecutive losses: {recent_losses}\n\n"
            f"Open runs available to bid on:\n{json.dumps(schedule, indent=2, default=str)}\n\n"
            "Only bid on runs with status='open'. You cannot bid on the same run twice.\n"
            "Decide whether to bid, on what, and how much — and explain your reasoning.\n\n"
            "Return ONLY JSON:\n"
            '{"reasoning": "your thinking about each run, the odds, your current situation, and why you sized bids as you did", '
            '"bids": [{"market_url": "...", "run_id": "...", "outcome_index": 0, "bet_amount": 5.0}]}\n'
            'or {"reasoning": "...", "bids": []} to sit this one out'
        )

        raw = complete(
            self.model,
            [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": prompt}],
            max_tokens=800,
            api_base=self.api_base,
            api_key=self.api_key,
        ).strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found in LLM response: {raw[:200]}")
        decision = json.loads(raw[start:end])
        reasoning = decision.get("reasoning", "")
        plays = []
        for bid in decision.get("bids", []):
            amount = round(float(bid["bet_amount"]), 2)
            plays.append((
                bid["market_url"],
                bid["run_id"],
                int(bid["outcome_index"]),
                amount,
                reasoning,
            ))
        return plays
