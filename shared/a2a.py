import asyncio
import uuid
from datetime import datetime, timezone

import httpx

from shared.crypto import canonical_bytes, sign, verify_sig
from shared.models import (
    A2AAgentCard,
    A2AMessage,
    A2APart,
    A2ATask,
    JsonRpcRequest,
    StatsResponse,
)

_TIMEOUT = httpx.Timeout(10.0)

# Trust-on-first-use cache: registry_url -> registry public key bytes
_registry_pubkeys: dict[str, bytes] = {}


async def _fetch_registry_pubkey(registry_url: str) -> bytes:
    if registry_url not in _registry_pubkeys:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{registry_url.rstrip('/')}/pubkey")
            resp.raise_for_status()
            _registry_pubkeys[registry_url] = bytes.fromhex(resp.json()["public_key"])
    return _registry_pubkeys[registry_url]


async def get_stats(url: str) -> StatsResponse:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{url.rstrip('/')}/stats")
        resp.raise_for_status()
        return StatsResponse.model_validate(resp.json())


async def register_agent(
    registry_urls: list[str],
    agent_url: str,
    name: str,
    agent_type: str,
    private_key: bytes,
    public_key: bytes,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = canonical_bytes({
        "url": agent_url,
        "name": name,
        "type": agent_type,
        "public_key": public_key.hex(),
        "timestamp": timestamp,
    })
    signature = sign(private_key, payload).hex()

    async def _register(url: str):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                await client.post(
                    f"{url.rstrip('/')}/register",
                    json={
                        "url": agent_url,
                        "name": name,
                        "type": agent_type,
                        "public_key": public_key.hex(),
                        "timestamp": timestamp,
                        "signature": signature,
                    },
                )
        except Exception:
            pass

    await asyncio.gather(*[_register(u) for u in registry_urls])


async def get_markets(registry_urls: list[str]) -> list[dict]:
    async def _fetch(url: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{url.rstrip('/')}/markets")
            resp.raise_for_status()
            entries = resp.json()

        try:
            reg_pubkey = await _fetch_registry_pubkey(url)
        except Exception:
            return entries  # registry pubkey unavailable — return unverified

        verified = []
        for entry in entries:
            sig_hex = entry.get("registry_signature", "")
            if not sig_hex:
                verified.append(entry)
                continue
            entry_for_sig = {k: v for k, v in entry.items() if k != "registry_signature"}
            if verify_sig(reg_pubkey, canonical_bytes(entry_for_sig), bytes.fromhex(sig_hex)):
                verified.append(entry)
            else:
                print(f"[a2a] invalid registry signature for {entry.get('url')} — dropping")
        return verified

    results = await asyncio.gather(*[_fetch(u) for u in registry_urls], return_exceptions=True)
    seen: set[str] = set()
    merged: list[dict] = []
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for entry in batch:
            if entry["url"] not in seen:
                seen.add(entry["url"])
                merged.append(entry)
    return merged


async def get_traders(registry_urls: list[str]) -> list[dict]:
    async def _fetch(url: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{url.rstrip('/')}/traders")
            resp.raise_for_status()
            entries = resp.json()

        try:
            reg_pubkey = await _fetch_registry_pubkey(url)
        except Exception:
            return entries

        verified = []
        for entry in entries:
            sig_hex = entry.get("registry_signature", "")
            if not sig_hex:
                verified.append(entry)
                continue
            entry_for_sig = {k: v for k, v in entry.items() if k != "registry_signature"}
            if verify_sig(reg_pubkey, canonical_bytes(entry_for_sig), bytes.fromhex(sig_hex)):
                verified.append(entry)
            else:
                print(f"[a2a] invalid registry signature for {entry.get('url')} — dropping")
        return verified

    results = await asyncio.gather(*[_fetch(u) for u in registry_urls], return_exceptions=True)
    seen: set[str] = set()
    merged: list[dict] = []
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for entry in batch:
            if entry["url"] not in seen:
                seen.add(entry["url"])
                merged.append(entry)
    return merged


class A2AError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def get_a2a_agent_card(url: str) -> A2AAgentCard:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{url.rstrip('/')}/.well-known/agent-card.json")
        resp.raise_for_status()
        return A2AAgentCard.model_validate(resp.json())


async def get_schedule(market_url: str) -> dict:
    message = A2AMessage(
        message_id=str(uuid.uuid4()),
        role="user",
        parts=[A2APart(data={"skill_id": "get-schedule"}, media_type="application/json")],
    )
    task = await send_message(market_url, message)
    if not task.artifacts:
        raise A2AError(code=-32000, message="Market returned no schedule artifact")
    return task.artifacts[0].parts[0].data


async def send_message(market_url: str, message: A2AMessage) -> A2ATask:
    body = JsonRpcRequest(
        jsonrpc="2.0",
        id=str(uuid.uuid4()),
        method="tasks/send",
        params={"message": message.model_dump(by_alias=True)},
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{market_url.rstrip('/')}/rpc",
            json=body.model_dump(by_alias=True),
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("error"):
        err = data["error"]
        raise A2AError(code=err["code"], message=err["message"])
    return A2ATask.model_validate(data.get("result", {}))


async def get_task_jsonrpc(market_url: str, task_id: str) -> A2ATask:
    body = JsonRpcRequest(
        jsonrpc="2.0",
        id=str(uuid.uuid4()),
        method="tasks/get",
        params={"id": task_id},
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{market_url.rstrip('/')}/rpc",
            json=body.model_dump(by_alias=True),
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("error"):
        err = data["error"]
        raise A2AError(code=err["code"], message=err["message"])
    return A2ATask.model_validate(data.get("result", {}))
