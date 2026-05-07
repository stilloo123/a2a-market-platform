import asyncio
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from market.brain import Brain, DefaultMarketBrain
from market.game_engine import GameEngine
from market.ledger import BidRecord, MarketLedger
from shared.a2a import register_agent
from shared.crypto import canonical_bytes, hash_game_spec, load_or_create_keypair, sign, verify_sig
from shared.thought_log import ThoughtLog
from shared.models import (
    A2AAgentCard,
    A2AArtifact,
    A2APart,
    A2ATask,
    A2ATaskStatus,
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
    AgentType,
    BidAck,
    BidRequest,
    GameSpec,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    StatsResponse,
    TaskState,
)


def load_config(path: str) -> dict:
    load_dotenv()
    cfg = yaml.safe_load(open(path))
    # registry_urls: support both old scalar and new list form
    if "registry_url" in cfg and "registry_urls" not in cfg:
        cfg["registry_urls"] = [cfg["registry_url"]]
    if (v := os.getenv("REGISTRY_URLS")) is not None:
        cfg["registry_urls"] = [u.strip() for u in v.split(",") if u.strip()]
    if (v := os.getenv("ADAPTATION_SILENCE_MINUTES")) is not None:
        cfg["adaptation_silence_minutes"] = float(v)
    if (v := os.getenv("PUBLIC_URL")) is not None:
        cfg["public_url"] = v.rstrip("/")
    return cfg


def build_app(cfg: dict) -> FastAPI:
    ledger = MarketLedger(seed_balance=cfg["seed_balance"])
    engine = GameEngine()
    agent_id = str(uuid.uuid4())
    self_url = cfg.get("public_url") or f"http://localhost:{cfg['port']}"
    registry_urls: list[str] = cfg["registry_urls"]
    priv_key, pub_key = load_or_create_keypair(Path("market/agent"))

    agents_md = Path(cfg["agents_md"]).read_text()
    brain: Brain = DefaultMarketBrain(
        agents_md=agents_md,
        model=cfg["model"],
        api_base=cfg.get("api_base"),
        api_key=cfg.get("api_key"),
    )

    games: list[GameSpec] = []
    thought_log = ThoughtLog(cfg["name"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal games
        reasoning, games = await asyncio.to_thread(brain.design_initial_games, ledger.balance)
        thought_log.write("GAME_DESIGN", reasoning, {
            "trigger": "startup",
            "games": [{"name": g.name, "rules": g.rules, "house_edge": g.house_edge(),
                       "schedule_interval_seconds": g.schedule_interval_seconds,
                       "bet_window_seconds": g.bet_window_seconds} for g in games],
        })
        print(f"[market] started with {len(games)} games: {[g.name for g in games]}")
        # Create first run for each game immediately
        for game in games:
            run = engine.create_run(game)
            print(f"[market] {game.name} — first run scheduled at {run.scheduled_at.isoformat()}")
        await register_agent(registry_urls, self_url, cfg["name"], "market", priv_key, pub_key)
        asyncio.create_task(_heartbeat_loop())
        asyncio.create_task(_scheduler_loop())
        asyncio.create_task(_adaptation_loop())
        yield

    app = FastAPI(title=cfg["name"], lifespan=lifespan)

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await register_agent(registry_urls, self_url, cfg["name"], "market", priv_key, pub_key)
            except Exception:
                pass

    async def _scheduler_loop():
        """Opens bid windows and resolves runs on schedule."""
        while True:
            now = datetime.now(timezone.utc)
            for game in _active_games():
                run = engine.current_run(game.game_id)
                if run is None:
                    run = engine.create_run(game)
                    print(f"[market] {game.name} — run scheduled at {run.scheduled_at.isoformat()}")
                elif run.status == "scheduled" and now >= run.bet_open_at:
                    engine.open_run(run.run_id)
                    print(f"[market] {game.name} — bid window OPEN (closes {run.scheduled_at.isoformat()})")
                elif run.status == "open" and now >= run.scheduled_at:
                    engine.close_run(run.run_id)
                    results = engine.resolve_run(run.run_id, game)
                    winners = sum(1 for r in results if r.trader_won)
                    total_payout = sum(r.payout for r in results)
                    for r in results:
                        ledger.record_bid(BidRecord(
                            task_id=r.task_id,
                            game_id=r.game_id,
                            game_name=game.name,
                            bet_amount=r.bet_amount,
                            payout=r.payout,
                            trader_won=r.trader_won,
                        ))
                    thought_log.write("RUN_RESOLVED", (
                        f"{game.name} run resolved. Outcome: {run.outcome_index}. "
                        f"{len(results)} bids, {winners} winners, ${total_payout:.2f} paid out."
                    ), {
                        "run_id": run.run_id,
                        "outcome_index": run.outcome_index,
                        "total_bids": len(results),
                        "winners": winners,
                        "total_payout": total_payout,
                        "server_seed": run.server_seed,
                    })
                    print(f"[market] {game.name} — resolved outcome={run.outcome_index}, "
                          f"bids={len(results)}, payout=${total_payout:.2f}")
                    # Schedule next run immediately
                    next_run = engine.create_run(game)
                    print(f"[market] {game.name} — next run at {next_run.scheduled_at.isoformat()}")
            await asyncio.sleep(5)

    async def _adaptation_loop():
        nonlocal games
        last_adapted_at: float = 0.0
        while True:
            await asyncio.sleep(60)
            silence = ledger.minutes_since_last_bet()
            cooldown_minutes = cfg.get("adaptation_silence_minutes", 10)
            since_last = (time.time() - last_adapted_at) / 60
            if silence >= cooldown_minutes and since_last >= cooldown_minutes:
                stats = {
                    "balance": ledger.balance,
                    "profit": ledger.profit(),
                    "total_bids": len(ledger.bets),
                    "per_game": ledger.per_game_stats(),
                }
                if await asyncio.to_thread(brain.should_adapt, stats, silence):
                    reasoning, new_games = await asyncio.to_thread(brain.redesign_games, games, stats)
                    thought_log.write("ADAPTATION", reasoning, {
                        "trigger": f"{silence:.1f} minutes silence",
                        "retired": [g.name for g in games],
                        "new_games": [{"name": g.name, "schedule_interval_seconds": g.schedule_interval_seconds,
                                       "bet_window_seconds": g.bet_window_seconds} for g in new_games],
                    })
                    games = new_games
                    for game in games:
                        run = engine.create_run(game)
                        print(f"[market] {game.name} — new run at {run.scheduled_at.isoformat()}")
                    last_adapted_at = time.time()
                    print(f"[market] adapted — new games: {[g.name for g in games]}")

    def _active_games() -> list[GameSpec]:
        return [g for g in games if g.active]

    def _find_game(game_id: str) -> GameSpec | None:
        return next((g for g in games if g.game_id == game_id and g.active), None)

    def _validate_and_place_bid(bid_req: BidRequest) -> tuple[str, BidAck]:
        run = engine._runs.get(bid_req.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status != "open":
            raise HTTPException(status_code=409, detail=f"Run is {run.status}, not open for bids")
        game = _find_game(run.game_id)
        if game is None:
            raise HTTPException(status_code=404, detail="Game not found or inactive")
        if not (game.min_bet <= bid_req.bet_amount <= game.max_bet):
            raise HTTPException(status_code=400, detail="Bid amount out of range")
        if bid_req.outcome_index >= len(game.outcomes):
            raise HTTPException(status_code=400, detail="Invalid outcome index")
        available = ledger.balance - engine.pending_exposure()
        max_acceptable = available * 0.10
        if bid_req.bet_amount > max_acceptable:
            raise HTTPException(status_code=400, detail="Bid exceeds market risk limit (accounting for pending exposure)")
        outcome_totals = engine.run_outcome_totals(bid_req.run_id)
        simulated = dict(outcome_totals)
        simulated[bid_req.outcome_index] = simulated.get(bid_req.outcome_index, 0.0) + bid_req.bet_amount
        worst_case_payout = max(
            simulated.get(i, 0.0) * game.outcomes[i].payout_multiplier
            for i in range(len(game.outcomes))
        )
        if worst_case_payout > ledger.balance:
            raise HTTPException(status_code=400, detail="Bid would exceed market payout capacity for this run")
        expected_hash = hash_game_spec(game)
        if bid_req.game_spec_hash != expected_hash:
            raise HTTPException(status_code=409, detail="Game spec mismatch — re-fetch agent card")
        try:
            task_id = engine.place_bid(bid_req.run_id, bid_req.outcome_index, bid_req.bet_amount, bid_req.trader_url)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        ack = BidAck(
            task_id=task_id,
            run_id=bid_req.run_id,
            server_seed_hash=run.server_seed_hash,
            game_spec_hash=expected_hash,
            scheduled_at=run.scheduled_at,
        )
        return task_id, ack

    @app.get("/thoughts")
    async def thoughts(n: int = 20):
        return thought_log.recent(n)

    @app.get("/stats", response_model=StatsResponse)
    async def stats():
        return StatsResponse(
            agent_id=agent_id,
            name=cfg["name"],
            type=AgentType.market,
            balance=ledger.balance,
            seed_balance=ledger.seed_balance,
            profit=ledger.profit(),
            total_bets=len(ledger.bets),
            total_wagered=ledger.total_wagered(),
            total_paid_out=ledger.total_paid_out(),
            active_games=len(_active_games()),
        )

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card():
        active = _active_games()
        skills = [
            AgentSkill(
                id="get-schedule",
                name="Get Game Schedule",
                description="Returns active games with odds and upcoming runs with bid windows. Call this before placing bids.",
                tags=["schedule", "games"],
                examples=["What games are available?", "Show me upcoming runs and bid windows"],
            ),
            *[
                AgentSkill(
                    id=game.game_id,
                    name=game.name,
                    description=f"{game.description} {game.rules}",
                    tags=["market platform", game.name.lower()],
                    examples=[
                        f"Place a bid on {game.name}",
                        f"Bid ${game.min_bet:.0f} on outcome 0 of {game.name}",
                    ],
                )
                for game in active
            ],
        ]
        card = A2AAgentCard(
            name=cfg["name"],
            description=f"A2A market agent offering {len(active)} game(s).",
            version="1.0",
            supported_interfaces=[
                AgentInterface(
                    url=f"{self_url}/rpc",
                    protocol_binding="json-rpc-2.0",
                    protocol_version="1.0",
                )
            ],
            capabilities=AgentCapabilities(),
            skills=skills,
            public_key=pub_key.hex(),
        )
        return card.model_dump(by_alias=True, mode="json")

    @app.post("/rpc")
    async def rpc(request: Request):
        try:
            body = await request.json()
            rpc_req = JsonRpcRequest.model_validate(body)
        except Exception:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

        def ok(result: dict) -> dict:
            return JsonRpcResponse(jsonrpc="2.0", id=rpc_req.id, result=result).model_dump()

        def err(code: int, message: str) -> dict:
            return JsonRpcResponse(
                jsonrpc="2.0", id=rpc_req.id,
                error=JsonRpcError(code=code, message=message),
            ).model_dump()

        method = rpc_req.method
        params = rpc_req.params

        if method == "tasks/send":
            try:
                parts = params.get("message", {}).get("parts", [])
                if not parts:
                    return err(-32600, "Invalid request: no parts in message")
                data = parts[0].get("data") or {}
                skill_id = data.get("skill_id")
                if not skill_id:
                    return err(-32600, "Missing skill_id in message data")
            except Exception as exc:
                return err(-32600, str(exc))

            if skill_id == "get-schedule":
                active = _active_games()
                scheduled_runs = [
                    run for game in active for run in engine.upcoming_runs(game.game_id, n=2)
                ]
                artifact = A2AArtifact(
                    artifact_id="game-schedule",
                    parts=[A2APart(data={
                        "balance": ledger.balance,
                        "games": [g.model_dump() for g in active],
                        "scheduled_runs": [r.model_dump() for r in scheduled_runs],
                    }, media_type="application/json")],
                )
                task = A2ATask(
                    id=str(uuid.uuid4()),
                    status=A2ATaskStatus(state=TaskState.completed),
                    artifacts=[artifact],
                )
                return ok(task.model_dump(by_alias=True, mode="json"))

            active_game_ids = {game.game_id for game in _active_games()}
            if skill_id not in active_game_ids:
                return err(-32601, f"Unknown skill: {skill_id}")

            # Require trader signature on every bid
            trader_pubkey_hex = data.get("trader_pubkey", "")
            trader_sig_hex = data.get("trader_signature", "")
            if not trader_pubkey_hex or not trader_sig_hex:
                return err(-32000, "Missing trader_pubkey or trader_signature")
            bid_data = {k: v for k, v in data.items()
                        if k not in ("trader_pubkey", "trader_signature", "skill_id")}
            try:
                valid = verify_sig(
                    bytes.fromhex(trader_pubkey_hex),
                    canonical_bytes(bid_data),
                    bytes.fromhex(trader_sig_hex),
                )
            except Exception:
                valid = False
            if not valid:
                return err(-32000, "Invalid trader signature")

            try:
                bid_req = BidRequest.model_validate(data)
            except Exception as exc:
                return err(-32600, str(exc))
            try:
                task_id, ack = _validate_and_place_bid(bid_req)
            except HTTPException as exc:
                return err(-32000, exc.detail)
            task = A2ATask(
                id=task_id,
                status=A2ATaskStatus(state=TaskState.submitted),
                metadata=ack.model_dump(),
            )
            return ok(task.model_dump(by_alias=True, mode="json"))

        elif method == "tasks/get":
            task_id = params.get("id")
            if not task_id:
                return err(-32600, "Missing task id")
            result = engine.get_result(task_id)
            if result is None:
                return err(-32001, "Task not found")
            if result.pending:
                task = A2ATask(id=task_id, status=A2ATaskStatus(state=TaskState.submitted))
            else:
                result_dict = result.model_dump()
                result_sig = sign(priv_key, canonical_bytes(result_dict)).hex()
                artifact = A2AArtifact(
                    artifact_id="bid_result",
                    parts=[A2APart(data={
                        **result_dict,
                        "market_signature": result_sig,
                        "market_pubkey": pub_key.hex(),
                    }, media_type="application/json")],
                )
                task = A2ATask(
                    id=task_id,
                    status=A2ATaskStatus(state=TaskState.completed),
                    artifacts=[artifact],
                )
            return ok(task.model_dump(by_alias=True, mode="json"))

        elif method == "tasks/cancel":
            return err(-32004, "Bids cannot be canceled once placed")

        elif method == "tasks/list":
            return ok({"tasks": []})

        else:
            return err(-32601, "Method not found")

    return app


if __name__ == "__main__":
    cfg_path = "market/config.yaml"
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]
    cfg = load_config(cfg_path)
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
