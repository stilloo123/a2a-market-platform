# A2A Protocol Compliance Plan

Reference: [A2A Spec](https://github.com/a2aproject/A2A) · [AgentCard proto](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto)

---

## Gap Analysis

| | Current | Spec requires |
|---|---|---|
| Discovery endpoint | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| AgentCard fields | `games`, `balance`, `scheduled_runs` | `name`, `description`, `version`, `supported_interfaces`, `capabilities`, `skills` |
| Transport | Plain REST POST | JSON-RPC 2.0 at `/rpc` |
| Bet submission | `POST /tasks/send` | `POST /rpc` → method `tasks/send` |
| Result poll | `GET /tasks/{task_id}` | `POST /rpc` → method `tasks/get` |
| Games in card | Raw `GameSpec` objects | `AgentSkill` objects (one per game) |
| Domain live data | Embedded in AgentCard | Separate `/api/games` endpoint (outside A2A) |

---

## Phase 1 — `shared/models.py`

**Pure additive. No risk. No existing models are changed.**

Add new A2A spec types alongside existing domain models (`BetRequest`, `BetAck`, `BetResult`, etc. are untouched — they become internal domain models wrapped by A2A envelopes on the wire).

> **Wire format:** All A2A JSON uses camelCase field names (standard protobuf JSON). Every new Pydantic model below needs:
> ```python
> from pydantic import ConfigDict
> from pydantic.alias_generators import to_camel
> model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
> ```
> `populate_by_name=True` lets internal code use snake_case; `alias_generator` ensures the wire format uses camelCase. This applies to **every** class defined below.

### Models to add

```python
_camel = ConfigDict(alias_generator=to_camel, populate_by_name=True)

# --- Discovery ---

class AgentInterface(BaseModel):
    model_config = _camel
    url: str
    protocol_binding: str          # "JSONRPC" | "GRPC" | "HTTP+JSON"
    protocol_version: str          # e.g. "1.0"
    tenant: str = ""

class AgentCapabilities(BaseModel):
    model_config = _camel
    streaming: bool = False
    push_notifications: bool = False
    extended_agent_card: bool = False
    # extensions: list omitted — not needed for minimal compliance

class AgentSkill(BaseModel):
    model_config = _camel
    id: str                        # = game_id
    name: str
    description: str
    tags: list[str]
    examples: list[str]            # natural language prompts, NOT serialized JSON
    input_modes: list[str] = ["application/json"]
    output_modes: list[str] = ["application/json"]

class A2AAgentCard(BaseModel):
    model_config = _camel
    name: str
    description: str
    version: str
    supported_interfaces: list[AgentInterface]  # first entry = preferred
    capabilities: AgentCapabilities
    default_input_modes: list[str] = ["application/json"]
    default_output_modes: list[str] = ["application/json"]
    skills: list[AgentSkill]       # one per active game

# --- Message envelope ---
# Part mirrors the proto oneof: set exactly one of text/data.
# Wire JSON: {"data": {...}, "mediaType": "application/json"}
# There is NO dataPart wrapper — the field name IS "data" directly on Part.

class A2APart(BaseModel):
    model_config = _camel
    text: str | None = None
    data: dict | None = None       # google.protobuf.Value → plain JSON object
    media_type: str = "application/json"

class A2AMessage(BaseModel):
    model_config = _camel
    message_id: str
    role: Literal["user", "agent"]
    parts: list[A2APart]

# --- Task lifecycle ---

class TaskState(str, Enum):
    submitted = "submitted"        # accepted, not yet resolved
    working   = "working"          # optional intermediate (skip if not streaming)
    completed = "completed"        # resolved (win or loss — outcome, not game result)
    failed    = "failed"           # protocol failure (bad request, etc.)
    canceled  = "canceled"         # not used, but declared

class A2ATaskStatus(BaseModel):
    model_config = _camel
    state: TaskState
    message: str | None = None

class A2AArtifact(BaseModel):
    model_config = _camel
    artifact_id: str
    parts: list[A2APart]           # BetResult dict goes in parts[0].data on completion

class A2ATask(BaseModel):
    model_config = _camel
    id: str                        # = task_id
    status: A2ATaskStatus
    metadata: dict = {}            # BetAck fields go here on submission
    artifacts: list[A2AArtifact] = []

# --- JSON-RPC envelopes ---

class JsonRpcRequest(BaseModel):
    model_config = _camel
    jsonrpc: Literal["2.0"]
    id: str | int
    method: str
    params: dict = {}

class JsonRpcError(BaseModel):
    model_config = _camel
    code: int
    message: str
    data: dict | None = None

class JsonRpcResponse(BaseModel):
    model_config = _camel
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    result: dict | None = None
    error: JsonRpcError | None = None
```

---

## Phase 2 — `casino/agent.py`

**Three new endpoints. Old endpoints stay alive for backward compat.**

### 2a. `GET /.well-known/agent-card.json`

Build `A2AAgentCard` from live games at request time. Each active `GameSpec` becomes an `AgentSkill`:

```python
AgentSkill(
    id=game.game_id,
    name=game.name,
    description=f"{game.description} {game.rules}",
    tags=["gambling", game.name.lower()],
    examples=[
        # Natural language prompts, per spec — NOT serialized JSON payloads.
        f"Place a bet on {game.name}",
        f"Bet ${game.min_bet:.0f} on outcome 0 of {game.name}",
    ],
)
```

`supported_interfaces` points to `/rpc`:
```python
AgentInterface(url=f"{self_url}/rpc", protocol_binding="json-rpc-2.0", protocol_version="1.0")
```

### 2b. `GET /api/games`

Exposes live domain state the player needs to construct bets. Not A2A protocol — plain REST.

```json
{
  "balance": 1000.0,
  "games": [ /* list of GameSpec dicts */ ],
  "scheduled_runs": [ /* list of GameRun dicts */ ]
}
```

### 2c. `POST /rpc` — JSON-RPC dispatcher

> **Critical:** Always return HTTP 200. JSON-RPC errors go in `{"error": {...}}` in the body, never in HTTP status codes.

Extract shared helper first:
```python
def _validate_and_place_bet(bet_req: BetRequest) -> tuple[str, BetAck]:
    # Move the 47 lines of validation from tasks_send() here.
    # Raises HTTPException on validation failure.
    # Returns (task_id, ack) on success.
```

Both `POST /tasks/send` (old) and `POST /rpc` (new) call this helper.

Dispatch table:

| Method | Action |
|---|---|
| `tasks/send` | Extract `BetRequest` from `params.message.parts[0].data` (flat Part — no wrapper), call `_validate_and_place_bet()`, return `A2ATask(state=submitted, metadata=ack.model_dump())` |
| `tasks/get` | `engine.get_result(task_id)` → `state=submitted` if pending, `state=completed` + artifact if resolved |
| `tasks/cancel` | Return error code `-32004`, message `"Bets cannot be canceled once placed"` |
| `tasks/list` | Return `{"tasks": []}` |
| unknown | Return error code `-32601`, message `"Method not found"` |

When task is resolved, BetResult goes into an artifact:
```python
A2AArtifact(
    artifact_id="bet_result",
    parts=[A2APart(data=result.model_dump(), media_type="application/json")]
)
```

---

## Phase 3 — `shared/a2a.py`

**Additive. All existing functions stay.**

```python
async def get_a2a_agent_card(url: str) -> A2AAgentCard:
    # GET /.well-known/agent-card.json

async def get_game_state(casino_url: str) -> dict:
    # GET /api/games → {"balance": ..., "games": [...], "scheduled_runs": [...]}

async def send_message(casino_url: str, message: A2AMessage) -> A2ATask:
    # POST /rpc, method="tasks/send"
    # Raises A2AError if response contains "error" field

async def get_task_jsonrpc(casino_url: str, task_id: str) -> A2ATask:
    # POST /rpc, method="tasks/get"
    # Raises A2AError(code=-32001) if task not found
    # Raises on transport errors (let caller decide retry vs. abandon)
```

> **Important:** Distinguish A2A errors from transport errors.
> - `code=-32001` (task not found) → caller should remove from pending, not retry
> - Connection error / timeout → caller should retry

---

## Phase 4 — `player/agent.py`

**Two loops change. Fairness verification and blacklisting are completely untouched.**

### Schedule fetch

Before:
```python
card = await get_agent_card(url)
# read card.games and card.scheduled_runs
```

After:
```python
card = await get_a2a_agent_card(url)   # verify A2A support, read skills
state = await get_game_state(url)      # read live games + runs
# same schedule-building logic, sourced from state["games"] + state["scheduled_runs"]
```

### Bet submission

Before:
```python
resp = await send_task(casino_url, payload=bet_req.model_dump())
# BetAck data in resp.data
```

After:
```python
message = A2AMessage(
    message_id=str(uuid.uuid4()),
    role="user",
    parts=[A2APart(data=bet_req.model_dump(), media_type="application/json")]
)
task = await send_message(casino_url, message)
# BetAck data in task.metadata
```

### Result polling

Before:
```python
result = await get_task_result(casino_url, task_id)
if result.get("pending", True):
    continue   # still waiting
# extract BetResult from result dict
```

After:
```python
task = await get_task_jsonrpc(casino_url, task_id)
if task.status.state in (TaskState.submitted, TaskState.working):
    continue   # still waiting
# extract BetResult from task.artifacts[0].parts[0].data  ← flat, no dataPart wrapper
```

> **Gotcha:** `pending=True` (old) meant "still waiting". In A2A, a missing task raises `A2AError(code=-32001)` — that means the task was lost, not just pending. On `-32001`, log and remove from `pending_tasks` rather than retrying forever.

---

## Phase 5 — `observer/agent.py`

**Minimal changes. Stats pipeline, dashboard, rankings are untouched.**

### Schedule collection

Replace `get_agent_card(url)` + `card.scheduled_runs` with:
```python
await get_a2a_agent_card(url)     # confirm A2A support
state = await get_game_state(url) # get scheduled_runs
```

### Observer's own card

Add `GET /.well-known/agent-card.json` on the observer returning a minimal `A2AAgentCard` (no skills, no `/rpc` — just required fields so it's nominally discoverable). Old `/.well-known/agent.json` stays.

---

## Phase 6 — Authentication (internet deployment only)

**Skip this phase for local simulation. Add it when casinos and players are hosted publicly by different operators.**

The A2A spec deliberately does not define an auth protocol — it defines how agents *declare and discover* auth requirements. Credentials are obtained out-of-band (registration with the casino operator). The recommended scheme for agent-to-agent (machine-to-machine, no human in the loop) is **OAuth 2.0 Client Credentials**.

### How it works end-to-end

```
Player agent                       Casino's auth server
     |                                      |
     |-- POST /token ─────────────────────→ |
     |   (client_id, client_secret,         |
     |    grant_type=client_credentials)    |
     |←── access_token (expires in ~1h) ── |
     |                                      |
     |-- POST /rpc ──────────────────────→ Casino agent
     |   Authorization: Bearer <token>
```

The player holds a `client_id` + `client_secret` issued by the casino operator at registration time. It exchanges these for a short-lived token, caches it, and attaches it to every RPC call. No human clicks anything — fully autonomous.

---

### 6a. Casino — declare auth in `A2AAgentCard`

Add `security_schemes` and `security_requirements` to the `A2AAgentCard` model (Phase 1) and populate them in `casino/agent.py`:

```python
# In A2AAgentCard (shared/models.py)
class OAuth2SecurityScheme(BaseModel):
    description: str = ""
    oauth2_metadata_url: str   # RFC 8414 metadata endpoint of the auth server

class SecurityScheme(BaseModel):
    oauth2_security_scheme: OAuth2SecurityScheme | None = None
    # extend with api_key_security_scheme, http_auth_security_scheme as needed

# Added fields on A2AAgentCard:
security_schemes: dict[str, SecurityScheme] = {}   # name → scheme
security_requirements: list[dict[str, list]] = []  # [{"casino_auth": []}]
```

In `casino/config.yaml`, add:
```yaml
auth:
  enabled: false                 # flip to true for public deployment
  token_url: "https://auth.example.com/token"
  oauth2_metadata_url: "https://auth.example.com/.well-known/oauth-authorization-server"
```

In `casino/agent.py`, when auth is enabled:
1. Add `security_schemes` + `security_requirements` to the `A2AAgentCard` response.
2. Add a FastAPI dependency on `POST /rpc` that validates the Bearer token (verify JWT signature against the auth server's JWKS, check expiry and audience).

```python
async def require_auth(authorization: str = Header(None)):
    if not cfg["auth"]["enabled"]:
        return   # local mode — skip
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ")
    # verify JWT: fetch JWKS from auth server, validate signature + claims
    _verify_jwt(token, expected_audience=self_url)
```

> **Gotcha:** Auth validation goes on `POST /rpc` only. `GET /.well-known/agent-card.json` and `GET /api/games` must stay **public** — the player needs to read the card to discover what auth scheme to use before it has a token.

---

### 6b. Player — token fetch and injection in `shared/a2a.py`

Add a `TokenCache` class that manages the token lifecycle:

```python
class TokenCache:
    def __init__(self, token_url: str, client_id: str, client_secret: str):
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()   # prevents concurrent refreshes

    async def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 30:
            return self._token   # fast path — no lock needed for reads
        async with self._lock:
            # re-check after acquiring lock — another coroutine may have refreshed
            if self._token and time.time() < self._expires_at - 30:
                return self._token
            async with httpx.AsyncClient() as client:
                resp = await client.post(self._token_url, data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                })
                resp.raise_for_status()
                body = resp.json()
                self._token = body["access_token"]
                self._expires_at = time.time() + body["expires_in"]
        return self._token
```

Update `send_message()` and `get_task_jsonrpc()` to accept an optional `token_cache: TokenCache | None`:

```python
async def send_message(casino_url, message, token_cache=None) -> A2ATask:
    headers = {"Content-Type": "application/json"}
    if token_cache:
        headers["Authorization"] = f"Bearer {await token_cache.get_token()}"
    # ... rest of the call unchanged
```

---

### 6c. Player — per-casino token cache in `player/agent.py`

When the player discovers a casino during `_fetch_schedule()`:

1. Read `card.security_schemes` from the `A2AAgentCard`.
2. If a scheme is present and `player/config.yaml` has credentials for this casino, instantiate a `TokenCache` for it.
3. Store the cache in a `casino_tokens: dict[str, TokenCache]` dict keyed by `casino_url`.
4. Pass the relevant `TokenCache` into `send_message()` and `get_task_jsonrpc()` calls.

In `player/config.yaml`, add:
```yaml
casino_credentials:
  "https://casino-a.example.com":
    client_id: "player-001"
    client_secret: "s3cr3t"
  "https://casino-b.example.com":
    client_id: "player-001"
    client_secret: "diff3r3nt"
```

If a casino has no entry in `casino_credentials`, the player operates unauthenticated (works fine for local casinos with `auth.enabled: false`).

---

### Security schemes reference

| Scheme | Best for | Config needed |
|---|---|---|
| **OAuth 2.0 Client Credentials** | M2M, recommended | `client_id`, `client_secret`, `token_url` |
| **API Key** | Simple deployments | A static key in a header — casino gives you one manually |
| **Bearer JWT** | When casino issues tokens itself | Pre-shared token, no refresh flow |
| **OpenID Connect** | When operator uses Google/Azure/etc. | OIDC discovery URL |
| **Mutual TLS** | High-security infra | Client cert + key pair issued by casino |

For a public gambling platform between autonomous agents, **OAuth 2.0 Client Credentials** is the right default. Each player operator registers with each casino operator to receive a `client_id` + `client_secret`.

---

## What We're Skipping (intentionally)

| Feature | Why skipped |
|---|---|
| Streaming (SSE) | Poll-based model is sufficient; `capabilities.streaming=False` declares this |
| Push notifications | `capabilities.push_notifications=False` |
| Extended agent card | `capabilities.extended_agent_card=False` |
| Auth (Phase 6) | Local simulation only — add when going public |
| Full `tasks/list` | Nothing requires it; return empty list |
| `TaskState.working` | Skip intermediate state; go `submitted → completed/failed` directly |

---

## Execution Order

```
Phase 1 (models)    ← no deps, do first, zero runtime risk
       ↓
Phase 2 (casino)  ←─┐  can be done in parallel
Phase 3 (helpers) ←─┘
       ↓
Phase 4 (player)  ←─┐  can be done in parallel
Phase 5 (observer)←─┘
       ↓
Phase 6 (auth)      ← only needed for public internet deployment
```

**Backward compat:** Old endpoints (`/.well-known/agent.json`, `POST /tasks/send`, `GET /tasks/{task_id}`) stay alive until Phases 4 and 5 are confirmed working against the new protocol.

---

## Rough Scope

| Phase | File | Lines added | Lines touched |
|---|---|---|---|
| 1 | `shared/models.py` | ~120 | 0 |
| 2 | `casino/agent.py` | ~100 | ~50 (extract helper) |
| 3 | `shared/a2a.py` | ~60 | 0 |
| 4 | `player/agent.py` | ~20 | ~50 |
| 5 | `observer/agent.py` | ~20 | ~20 |
| 6 | `shared/a2a.py`, `casino/agent.py`, `player/agent.py`, configs | ~100 | ~30 |
| **Total** | | **~420** | **~150** |

No deletions until backward compat is confirmed working end-to-end.
