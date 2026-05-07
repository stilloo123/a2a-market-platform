from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from shared.llm import complete
from shared.models import GameOutcome, GameSpec


class Brain(ABC):
    def __init__(self, agents_md: str, model: str, api_base: str | None = None, api_key: str | None = None):
        self.system_prompt = agents_md
        self.model = model
        self.api_base = api_base
        self.api_key = api_key

    @abstractmethod
    def design_initial_games(self, balance: float) -> tuple[str, list[GameSpec]]: ...

    @abstractmethod
    def should_adapt(self, stats: dict, silence_minutes: float) -> bool: ...

    @abstractmethod
    def redesign_games(self, current_games: list[GameSpec], stats: dict) -> tuple[str, list[GameSpec]]: ...


class DefaultMarketBrain(Brain):

    def design_initial_games(self, balance: float) -> list[GameSpec]:
        prompt = (
            f"You are starting a market with ${balance:.2f}. Design your opening game portfolio.\n\n"
            "PROTOCOL RULES (these are hard constraints, not guidelines):\n"
            "- All outcomes have EQUAL probability = 1/num_outcomes\n"
            "- To get house edge H with N outcomes: payout_multiplier = (1 - H) × N\n"
            "- bet_window_seconds must be >= 120 and < schedule_interval_seconds\n\n"
            "Each game needs:\n"
            "- name, description\n"
            "- rules: full trader-facing documentation — how to play, what determines the outcome "
            "(provably fair RNG, one result per scheduled run), win/loss conditions, "
            "a worked example with numbers, and the exact odds\n"
            "- outcomes: list of {condition, win_probability (ignored by engine), payout_multiplier}\n"
            "- min_bet, max_bet\n"
            "- schedule_interval_seconds, bet_window_seconds\n\n"
            "Return ONLY JSON, no markdown:\n"
            '{"reasoning": "your thinking about what games to offer, why, and how you sized them...", "games": ['
            '{"name": "...", "description": "...", "rules": "...", '
            '"min_bet": 1, "max_bet": 50, "schedule_interval_seconds": 600, "bet_window_seconds": 120, '
            '"outcomes": [{"condition": "...", "win_probability": 0.5, "payout_multiplier": 1.9}]}'
            "]}"
        )
        return self._call_for_games(prompt)

    def should_adapt(self, stats: dict, silence_minutes: float) -> bool:
        if silence_minutes < 5:
            return False
        prompt = (
            f"Market stats: {json.dumps(stats, indent=2)}\n"
            f"No bids for {silence_minutes:.1f} minutes. "
            "Should the market redesign its games to attract more traders? "
            'Reply with ONLY "yes" or "no".'
        )
        text = complete(
            self.model,
            [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": prompt}],
            max_tokens=10,
            api_base=self.api_base,
            api_key=self.api_key,
        )
        return "yes" in text.lower()

    def redesign_games(self, current_games: list[GameSpec], stats: dict) -> tuple[str, list[GameSpec]]:
        current_summary = [
            {
                "name": g.name,
                "house_edge": g.house_edge(),
                "schedule_interval_seconds": g.schedule_interval_seconds,
                "bet_window_seconds": g.bet_window_seconds,
                "active": g.active,
            }
            for g in current_games
        ]
        prompt = (
            f"Current games: {json.dumps(current_summary)}\n"
            f"Performance stats: {json.dumps(stats, indent=2)}\n"
            "Traders aren't bidding. Decide what to change and why — you could tweak odds, "
            "retheme games, change the schedule, adjust bid limits, or start fresh entirely.\n\n"
            "Protocol constraints: bet_window_seconds >= 120 and < schedule_interval_seconds. "
            "All outcomes have equal probability; payout_multiplier = (1 - house_edge) × num_outcomes.\n\n"
            "Each game needs full rules documentation (same schema as before).\n"
            "Return ONLY JSON: "
            '{"reasoning": "your diagnosis of why traders aren\'t coming and what you\'re changing...", "games": [...]}'
        )
        return self._call_for_games(prompt)

    def _call_for_games(self, prompt: str) -> tuple[str, list[GameSpec]]:
        raw = complete(
            self.model,
            [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": prompt}],
            max_tokens=4096,
            api_base=self.api_base,
            api_key=self.api_key,
        ).strip()
        # Extract the first {...} block — handles markdown fences, trailing text, etc.
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError(f"No JSON object found in LLM response: {raw[:200]}")
        try:
            data = json.loads(raw[start:end])
        except json.JSONDecodeError as exc:
            print(f"[market brain] JSON parse failed: {exc}\nRaw ({len(raw)} chars): {raw[:500]}")
            raise
        reasoning = data.get("reasoning", "")
        games = [self._parse_game(g) for g in data.get("games", data if isinstance(data, list) else [])]
        return reasoning, games

    def _parse_game(self, data: dict) -> GameSpec:
        outcomes = [
            GameOutcome(
                condition=o["condition"],
                win_probability=float(o["win_probability"]),
                payout_multiplier=float(o["payout_multiplier"]),
            )
            for o in data["outcomes"]
        ]
        return GameSpec(
            game_id=str(uuid.uuid4()),
            name=data["name"],
            description=data.get("description", ""),
            rules=data.get("rules", "No rules provided."),
            outcomes=outcomes,
            min_bet=float(data["min_bet"]),
            max_bet=float(data["max_bet"]),
            active=True,
            created_at=datetime.now(timezone.utc),
            schedule_interval_seconds=int(data["schedule_interval_seconds"]),
            bet_window_seconds=int(data.get("bet_window_seconds", 120)),
        )
