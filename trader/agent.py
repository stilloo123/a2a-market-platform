import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI

from trader.brain import Brain, DefaultTraderBrain
from trader.ledger import BidRecord, TraderLedger
from shared.a2a import A2AError, get_a2a_agent_card, get_markets, get_schedule, get_task_jsonrpc, register_agent, send_message
from shared.crypto import canonical_bytes, hash_game_spec, load_or_create_keypair, resolve_run, sign, verify, verify_sig
from shared.thought_log import ThoughtLog
from shared.models import A2AAgentCard, A2AMessage, A2APart, AgentCapabilities, AgentType, BidRequest, GameRun, GameSpec, StatsResponse, TaskState


def load_config(path: str) -> dict:
    load_dotenv()
    cfg = yaml.safe_load(open(path))
    if "registry_url" in cfg and "registry_urls" not in cfg:
        cfg["registry_urls"] = [cfg["registry_url"]]
    if (v := os.getenv("REGISTRY_URLS")) is not None:
        cfg["registry_urls"] = [u.strip() for u in v.split(",") if u.strip()]
    if (v := os.getenv("BET_DELAY_SECONDS")) is not None:
        cfg["bet_delay_seconds"] = float(v)
    if (v := os.getenv("DISCOVERY_INTERVAL_SECONDS")) is not None:
        cfg["discovery_interval_seconds"] = float(v)
    if (v := os.getenv("PUBLIC_URL")) is not None:
        cfg["public_url"] = v.rstrip("/")
    return cfg


def build_app(cfg: dict) -> FastAPI:
    ledger = TraderLedger(seed_balance=cfg["seed_balance"])
    agent_id = str(uuid.uuid4())
    self_url = cfg.get("public_url") or f"http://localhost:{cfg['port']}"
    registry_urls: list[str] = cfg["registry_urls"]

    agents_md = Path(cfg["agents_md"]).read_text()
    brain: Brain = DefaultTraderBrain(
        agents_md=agents_md,
        model=cfg["model"],
        api_base=cfg.get("api_base"),
        api_key=cfg.get("api_key"),
    )

    thought_log = ThoughtLog(cfg["name"])
    priv_key, pub_key = load_or_create_keypair(Path("trader/agent"))

    # State shared across loops
    known_schedule: list[dict] = []       # flat list of open+upcoming runs across all markets
    already_bid: set[str] = set()         # run_ids we've placed a bid on this cycle
    # task_id → {market_url, run_id, server_seed_hash, outcome_index, bet_amount, num_outcomes}
    pending_tasks: dict[str, dict] = {}
    market_pubkeys: dict[str, bytes] = {}  # market_url → Ed25519 public key bytes

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await register_agent(registry_urls, self_url, cfg["name"], "trader", priv_key, pub_key)
        asyncio.create_task(_heartbeat_loop())
        asyncio.create_task(_schedule_loop())
        asyncio.create_task(_bidding_loop())
        asyncio.create_task(_result_loop())
        yield

    app = FastAPI(title=cfg["name"], lifespan=lifespan)

    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await register_agent(registry_urls, self_url, cfg["name"], "trader", priv_key, pub_key)
            except Exception:
                pass

    async def _schedule_loop():
        """Fetch all market AgentCards and build a flat schedule of upcoming runs."""
        nonlocal known_schedule, already_bid
        while True:
            schedule = await _fetch_schedule()
            known_schedule = schedule
            # Retain entries that are still live in the schedule OR still awaiting
            # a result — clearing based on schedule presence alone would drop a
            # run_id the moment it resolves and leaves the AgentCard, before
            # _result_loop has had a chance to confirm and remove the task.
            live_run_ids = {r["run_id"] for r in schedule}
            pending_run_ids = {info["run_id"] for info in pending_tasks.values()}
            already_bid = {rid for rid in already_bid if rid in live_run_ids or rid in pending_run_ids}
            await asyncio.sleep(cfg.get("discovery_interval_seconds", 60))

    async def _fetch_schedule() -> list[dict]:
        market_urls: list[str] = list(cfg.get("market_urls") or [])
        try:
            entries = await get_markets(registry_urls)
            market_urls += [e["url"] for e in entries]
        except Exception as exc:
            print(f"[trader] registry unreachable: {exc}")
        market_urls = list(dict.fromkeys(market_urls))

        schedule: list[dict] = []

        async def fetch(url: str):
            if ledger.is_blacklisted(url):
                return
            try:
                card = await get_a2a_agent_card(url)
                if card.public_key:
                    market_pubkeys[url] = bytes.fromhex(card.public_key)
                state = await get_schedule(url)
                games_by_id = {g["game_id"]: GameSpec.model_validate(g) for g in state["games"]}
                for run_dict in state["scheduled_runs"]:
                    run = GameRun.model_validate(run_dict)
                    game = games_by_id.get(run.game_id)
                    if game is None or not game.active:
                        continue
                    now = datetime.now(timezone.utc)
                    opens_in = (run.bet_open_at - now).total_seconds()
                    closes_in = (run.scheduled_at - now).total_seconds()
                    schedule.append({
                        "market_url": url,
                        "market_name": card.name,
                        "run_id": run.run_id,
                        "game_id": run.game_id,
                        "game_name": run.game_name,
                        "status": run.status,
                        "scheduled_at": run.scheduled_at.isoformat(),
                        "opens_in_seconds": max(0, opens_in),
                        "closes_in_seconds": max(0, closes_in),
                        "server_seed_hash": run.server_seed_hash,
                        "house_edge": game.house_edge(),
                        "min_bet": game.min_bet,
                        "max_bet": game.max_bet,
                        "num_outcomes": len(game.outcomes),
                        "outcomes": [
                            {
                                "index": i,
                                "condition": o.condition,
                                "win_probability": o.win_probability,
                                "payout_multiplier": o.payout_multiplier,
                            }
                            for i, o in enumerate(game.outcomes)
                        ],
                        "game_spec_hash": hash_game_spec(game),
                    })
            except Exception as exc:
                print(f"[trader] could not reach market {url}: {exc}")

        await asyncio.gather(*[fetch(u) for u in market_urls])
        print(f"[trader] schedule: {len(schedule)} run(s) across {len(market_urls)} market(s)")
        return schedule

    async def _bidding_loop():
        """Place bids on open runs the brain selects."""
        while True:
            await asyncio.sleep(cfg.get("bet_delay_seconds", 60))
            if ledger.balance < ledger.seed_balance * 0.20:
                print("[trader] balance too low — pausing bids")
                continue

            open_runs = [
                r for r in known_schedule
                if r["status"] == "open" and r["run_id"] not in already_bid
            ]
            if not open_runs:
                continue

            pending_exposure = sum(t["bet_amount"] for t in pending_tasks.values())
            plays = await asyncio.to_thread(
                brain.select_plays,
                open_runs,
                ledger.balance,
                pending_exposure,
                ledger.recent_losses(),
            )
            if not plays:
                print("[trader] brain skipped all open runs")
                continue

            for market_url, run_id, outcome_index, bet_amount, reasoning in plays:
                run_info = next((r for r in open_runs if r["run_id"] == run_id), None)
                if run_info is None:
                    continue

                thought_log.write("BID_DECISION", reasoning, {
                    "market_url": market_url,
                    "run_id": run_id,
                    "game_name": run_info["game_name"],
                    "outcome_index": outcome_index,
                    "bet_amount": bet_amount,
                    "scheduled_at": run_info["scheduled_at"],
                    "balance_before": ledger.balance,
                })

                try:
                    bid_req = BidRequest(
                        run_id=run_id,
                        outcome_index=outcome_index,
                        bet_amount=bet_amount,
                        game_spec_hash=run_info["game_spec_hash"],
                        trader_url=self_url,
                    )
                    bid_data = bid_req.model_dump()
                    trader_sig = sign(priv_key, canonical_bytes(bid_data)).hex()
                    message = A2AMessage(
                        message_id=str(uuid.uuid4()),
                        role="user",
                        parts=[A2APart(data={
                            "skill_id": run_info["game_id"],
                            **bid_data,
                            "trader_pubkey": pub_key.hex(),
                            "trader_signature": trader_sig,
                        }, media_type="application/json")],
                    )
                    task = await send_message(market_url, message)
                    ack = task.metadata
                    # Verify market echoed back the same server_seed_hash we saw in the schedule
                    if ack.get("server_seed_hash") != run_info["server_seed_hash"]:
                        print(f"[trader] server_seed_hash mismatch from {market_url} — skipping")
                        continue

                    pending_tasks[task.id] = {
                        "market_url": market_url,
                        "run_id": run_id,
                        "game_id": run_info["game_id"],
                        "game_name": run_info["game_name"],
                        "server_seed_hash": ack["server_seed_hash"],
                        "outcome_index": outcome_index,
                        "bet_amount": bet_amount,
                        "num_outcomes": run_info["num_outcomes"],
                        "scheduled_at": run_info["scheduled_at"],
                    }
                    already_bid.add(run_id)
                    print(f"[trader] bid ${bet_amount:.2f} on {run_info['game_name']} "
                          f"@ {market_url} (resolves {run_info['scheduled_at']})")
                except Exception as exc:
                    print(f"[trader] bid error at {market_url}: {exc}")

    async def _result_loop():
        """Poll for results of pending bids and verify fairness."""
        while True:
            await asyncio.sleep(10)
            now = datetime.now(timezone.utc)
            for task_id in list(pending_tasks.keys()):
                info = pending_tasks[task_id]

                # Expire tasks whose scheduled_at + 10 min grace has passed without resolution.
                deadline = datetime.fromisoformat(info["scheduled_at"]) + timedelta(minutes=10)
                if now > deadline:
                    print(f"[trader] task {task_id} expired (market offline?) — writing off ${info['bet_amount']:.2f}")
                    ledger.record_bid(BidRecord(
                        task_id=task_id,
                        market_url=info["market_url"],
                        game_id=info["game_id"],
                        game_name=info["game_name"],
                        bet_amount=info["bet_amount"],
                        payout=0.0,
                        trader_won=False,
                        verified=False,
                    ))
                    del pending_tasks[task_id]
                    continue

                try:
                    task = await get_task_jsonrpc(info["market_url"], task_id)
                    if task.status.state in (TaskState.submitted, TaskState.working):
                        continue

                    result_data = task.artifacts[0].parts[0].data if task.artifacts else {}

                    # Verify market's signature on the result
                    market_sig_hex = result_data.get("market_signature", "")
                    market_pubkey = market_pubkeys.get(info["market_url"])
                    if market_sig_hex and market_pubkey:
                        result_for_sig = {k: v for k, v in result_data.items()
                                          if k not in ("market_signature", "market_pubkey")}
                        if not verify_sig(market_pubkey, canonical_bytes(result_for_sig),
                                          bytes.fromhex(market_sig_hex)):
                            reason = "invalid market result signature"
                            print(f"[trader] CHEAT DETECTED at {info['market_url']} ({reason}) — blacklisting")
                            ledger.blacklist(info["market_url"])
                            del pending_tasks[task_id]
                            continue

                    server_seed = result_data["server_seed"]
                    outcome_index = result_data["outcome_index"]
                    num_outcomes = info["num_outcomes"]

                    hash_ok = verify(server_seed, info["server_seed_hash"])
                    outcome_ok = resolve_run(server_seed, num_outcomes) == outcome_index

                    if not hash_ok or not outcome_ok:
                        reason = "hash mismatch" if not hash_ok else "outcome mismatch — market lied"
                        print(f"[trader] CHEAT DETECTED at {info['market_url']} ({reason}) — blacklisting")
                        ledger.blacklist(info["market_url"])
                        del pending_tasks[task_id]
                        continue

                    trader_won = result_data["trader_won"]
                    payout = result_data["payout"]
                    ledger.record_bid(BidRecord(
                        task_id=task_id,
                        market_url=info["market_url"],
                        game_id=result_data["game_id"],
                        game_name=info["game_name"],
                        bet_amount=info["bet_amount"],
                        payout=payout,
                        trader_won=trader_won,
                        verified=True,
                    ))
                    status = "WON" if trader_won else "lost"
                    post_note = (
                        f"{status} ${info['bet_amount']:.2f} on {info['game_name']}. "
                        f"Payout: ${payout:.2f}. Balance now: ${ledger.balance:.2f}. "
                        f"Consecutive losses: {ledger.recent_losses()}."
                    )
                    thought_log.write("BID_RESULT", post_note, {
                        "won": trader_won,
                        "bet_amount": info["bet_amount"],
                        "payout": payout,
                        "balance_after": ledger.balance,
                        "verified": True,
                    })
                    print(f"[trader] {status} ${info['bet_amount']:.2f} at {info['market_url']} | "
                          f"payout: ${payout:.2f} | balance: ${ledger.balance:.2f}")
                    del pending_tasks[task_id]

                except A2AError as exc:
                    if exc.code == -32001:
                        print(f"[trader] task {task_id} not found at market — removing from pending")
                        del pending_tasks[task_id]
                except Exception as exc:
                    print(f"[trader] result poll error for {task_id}: {exc}")

    @app.get("/thoughts")
    async def thoughts(n: int = 20):
        return thought_log.recent(n)

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card():
        card = A2AAgentCard(
            name=cfg["name"],
            description="A2A trader agent.",
            version="1.0",
            supported_interfaces=[],
            capabilities=AgentCapabilities(),
            skills=[],
            public_key=pub_key.hex(),
        )
        return card.model_dump(by_alias=True, mode="json")

    @app.get("/stats", response_model=StatsResponse)
    async def stats():
        return StatsResponse(
            agent_id=agent_id,
            name=cfg["name"],
            type=AgentType.trader,
            balance=ledger.balance,
            seed_balance=ledger.seed_balance,
            profit=ledger.profit(),
            total_bets=len(ledger.bets),
            total_wagered=ledger.total_wagered(),
            total_paid_out=ledger.total_paid_out(),
        )

    return app


if __name__ == "__main__":
    cfg_path = "trader/config.yaml"
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]
    cfg = load_config(cfg_path)
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
