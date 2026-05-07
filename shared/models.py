from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic.alias_generators import to_camel


class AgentType(str, Enum):
    market = "market"
    trader = "trader"
    observer = "observer"


class GameOutcome(BaseModel):
    condition: str
    win_probability: float       # always 1/num_outcomes — enforced by GameSpec validator
    payout_multiplier: float

    @field_validator("payout_multiplier")
    @classmethod
    def validate_multiplier(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("payout_multiplier must be positive")
        return v


class GameSpec(BaseModel):
    game_id: str
    name: str
    description: str
    rules: str                        # full human-readable rules
    outcomes: list[GameOutcome]
    min_bet: float
    max_bet: float
    active: bool = True
    created_at: datetime = None
    schedule_interval_seconds: int = 1800   # how often the game runs
    bet_window_seconds: int = 300           # how long before run time bets are accepted

    @model_validator(mode="after")
    def enforce_uniform_probability(self) -> GameSpec:
        if not self.outcomes:
            raise ValueError("Game must have at least one outcome")
        true_prob = round(1.0 / len(self.outcomes), 8)
        for outcome in self.outcomes:
            outcome.win_probability = true_prob
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        return self

    def house_edge(self) -> float:
        true_prob = 1.0 / len(self.outcomes)
        best_ev = true_prob * max(o.payout_multiplier for o in self.outcomes)
        return round(1.0 - best_ev, 4)


class GameRun(BaseModel):
    run_id: str
    game_id: str
    game_name: str
    scheduled_at: datetime
    bet_open_at: datetime
    status: Literal["scheduled", "open", "closed", "resolved"] = "scheduled"
    server_seed_hash: str              # committed when run is created, before window opens
    outcome_index: int | None = None   # set at resolution
    server_seed: str | None = None     # revealed at resolution only
    total_bets: int = 0
    total_wagered: float = 0.0


class BidRequest(BaseModel):
    run_id: str                # which scheduled run to bid on
    outcome_index: int
    bet_amount: float
    game_spec_hash: str        # locks in the rules at bid time
    trader_url: str            # agent identity — market enforces one bid per trader per run


class BidAck(BaseModel):
    task_id: str
    run_id: str
    server_seed_hash: str      # from GameRun — committed before window opened
    game_spec_hash: str        # echoed back for trader to verify
    scheduled_at: datetime     # when trader can expect resolution


class BidResult(BaseModel):
    task_id: str
    run_id: str
    game_id: str
    pending: bool = False
    outcome_index: int | None = None
    server_seed: str | None = None     # revealed only when resolved
    trader_won: bool = False
    bet_amount: float
    payout: float = 0.0


class StatsResponse(BaseModel):
    agent_id: str
    name: str
    type: AgentType
    balance: float
    seed_balance: float
    profit: float
    total_bets: int
    total_wagered: float
    total_paid_out: float
    active_games: int = 0
    blacklisted: bool = False


class RegistryEntry(BaseModel):
    url: str
    name: str
    type: AgentType
    last_heartbeat: datetime
    public_key: str = ""
    registry_signature: str = ""
    blacklisted: bool = False


class ReportRequest(BaseModel):
    url: str
    task_id: str
    reason: str


_camel = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AgentInterface(BaseModel):
    model_config = _camel
    url: str
    protocol_binding: str
    protocol_version: str
    tenant: str = ""


class AgentCapabilities(BaseModel):
    model_config = _camel
    streaming: bool = False
    push_notifications: bool = False
    extended_agent_card: bool = False


class AgentSkill(BaseModel):
    model_config = _camel
    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str]
    input_modes: list[str] = ["application/json"]
    output_modes: list[str] = ["application/json"]


class A2AAgentCard(BaseModel):
    model_config = _camel
    name: str
    description: str
    version: str
    supported_interfaces: list[AgentInterface]
    capabilities: AgentCapabilities
    default_input_modes: list[str] = ["application/json"]
    default_output_modes: list[str] = ["application/json"]
    skills: list[AgentSkill]
    public_key: str = ""


class A2APart(BaseModel):
    model_config = _camel
    text: str | None = None
    data: dict | None = None
    media_type: str = "application/json"


class A2AMessage(BaseModel):
    model_config = _camel
    message_id: str
    role: Literal["user", "agent"]
    parts: list[A2APart]


class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class A2ATaskStatus(BaseModel):
    model_config = _camel
    state: TaskState
    message: str | None = None


class A2AArtifact(BaseModel):
    model_config = _camel
    artifact_id: str
    parts: list[A2APart]


class A2ATask(BaseModel):
    model_config = _camel
    id: str
    status: A2ATaskStatus
    metadata: dict = {}
    artifacts: list[A2AArtifact] = []


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


