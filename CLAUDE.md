# A2A Market Platform — Dev Notes

## Project structure

```
shared/     protocol layer — models, crypto, a2a helpers (no LLM, no side effects)
registry/   dumb URL directory — stateless FastAPI, no game data
market/     market agent — FastAPI A2A server + LLM brain
trader/     trader agent — async bidding loop + FastAPI stats endpoint
observer/   observer agent — polling aggregator + rich dashboard + REST rankings
```

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...   # or set in .env

python -m registry.server          # port 8000
python -m market.agent             # port 8001
python -m trader.agent             # port 8002
python -m observer.agent           # port 8080  — dashboard at http://localhost:8080
```

Each agent generates its own Ed25519 keypair on first run (`market/agent.key`,
`trader/agent.key`, `registry/registry.key`). These are gitignored — every
checkout gets a fresh, independent identity automatically.

## Key invariants

- `shared/` has no imports from market/trader/observer — it's the foundation
- Game data lives in the market's AgentCard, not in the registry
- Traders re-discover markets every `discovery_interval_seconds` — no pub/sub
- Commit-reveal is in `shared/crypto.py` — never bypass it
- `Brain` is the only thing users should subclass; everything else is plumbing

## Auth model (Ed25519, no OAuth)

Every agent signs its registry registration with its private key. The registry
verifies and counter-signs the entry. Traders verify the registry's signature
on market listings before connecting. Markets verify trader signatures on bets.
Results are signed by the market so traders can detect tampering.

Trust anchor: the registry's public key, fetched once on first contact (TOFU).
For a single local registry this is automatic — no configuration needed.

## Customising agent behaviour

Edit `market/AGENTS.md` to change how the market designs games and sets odds.
Edit `trader/AGENTS.md` to change the trader's betting strategy and risk profile.
The `Brain` class in `market/brain.py` and `trader/brain.py` is the only
subclassable surface — everything else is protocol plumbing.

## Adding a new agent type

1. Create a new directory with `agent.py`, `brain.py`, `ledger.py`, `config.yaml`, `AGENTS.md`
2. Expose `GET /.well-known/agent-card.json` and `GET /stats` for observer compatibility
3. Register with registry at startup (use `shared.a2a.register_agent`) and heartbeat every 60s
4. Generate a keypair with `shared.crypto.load_or_create_keypair` and pass it to `register_agent`
