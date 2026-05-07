from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from shared.models import AgentType
from shared.crypto import canonical_bytes, load_or_create_keypair, sign, verify_sig

_agents: dict[str, dict] = {}
_register_times: dict[str, list[datetime]] = defaultdict(list)
_REGISTER_LIMIT = 10   # max registrations per IP per minute

STALE_AFTER_SECONDS = 180  # 3 missed heartbeats at 60s interval

_priv_key: bytes = b""
_pub_key: bytes = b""


class RegisterRequest(BaseModel):
    url: str
    name: str
    type: AgentType
    public_key: str   # agent's Ed25519 public key, hex-encoded
    timestamp: str    # ISO UTC — registry rejects if older than 5 min (replay prevention)
    signature: str    # Ed25519 sig over canonical_bytes({url,name,type,public_key,timestamp})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _priv_key, _pub_key
    _priv_key, _pub_key = load_or_create_keypair(Path("registry/registry"))
    print(f"[registry] public key: {_pub_key.hex()}")
    yield


app = FastAPI(title="A2A Market Platform Registry", lifespan=lifespan)


@app.get("/pubkey")
async def pubkey():
    return {"public_key": _pub_key.hex()}


@app.post("/register")
async def register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=60)
    times = _register_times[client_ip]
    _register_times[client_ip] = [t for t in times if t > cutoff]
    if len(_register_times[client_ip]) >= _REGISTER_LIMIT:
        raise HTTPException(status_code=429, detail="Too many registrations — try again later")
    _register_times[client_ip].append(now)

    try:
        ts = datetime.fromisoformat(req.timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = abs((datetime.now(timezone.utc) - ts).total_seconds())
        if age > 300:
            raise HTTPException(status_code=400, detail="Timestamp too old — possible replay")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp format")

    try:
        sig_bytes = bytes.fromhex(req.signature)
        pub_bytes = bytes.fromhex(req.public_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid hex in signature or public_key")

    payload = canonical_bytes({
        "url": req.url,
        "name": req.name,
        "type": req.type.value,
        "public_key": req.public_key,
        "timestamp": req.timestamp,
    })
    if not verify_sig(pub_bytes, payload, sig_bytes):
        raise HTTPException(status_code=401, detail="Invalid agent signature")

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "url": req.url,
        "name": req.name,
        "type": req.type.value,
        "public_key": req.public_key,
        "last_heartbeat": now,
    }
    entry["registry_signature"] = sign(
        _priv_key,
        canonical_bytes({k: v for k, v in entry.items()}),
    ).hex()
    _agents[req.url] = entry
    return {"status": "registered", "registry_signature": entry["registry_signature"]}


@app.get("/markets")
async def list_markets():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == AgentType.market]


@app.get("/traders")
async def list_traders():
    _evict_stale()
    return [e for e in _agents.values() if e["type"] == AgentType.trader]


@app.get("/agents")
async def list_all():
    _evict_stale()
    return list(_agents.values())



@app.get("/health")
async def health():
    return {"status": "ok", "agents": len(_agents)}


def _evict_stale():
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
    stale = []
    for url, e in _agents.items():
        ts = datetime.fromisoformat(e["last_heartbeat"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            stale.append(url)
    for url in stale:
        del _agents[url]


if __name__ == "__main__":
    import sys
    port = 8000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    uvicorn.run("registry.server:app", host="0.0.0.0", port=port, reload=False)
