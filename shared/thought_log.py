import json
import re
from datetime import datetime, timezone
from pathlib import Path


class ThoughtLog:
    def __init__(self, agent_name: str, log_dir: str = "logs"):
        log_path = Path(log_dir).resolve()
        log_path.mkdir(exist_ok=True)
        safe_name = re.sub(r'[^a-z0-9_-]', '_', agent_name.lower()) or "agent"
        self._path = log_path / f"{safe_name}.jsonl"

    def write(self, event_type: str, reasoning: str, data: dict | None = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "reasoning": reasoning,
        }
        if data:
            entry["data"] = data
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def recent(self, n: int = 20) -> list[dict]:
        if not self._path.exists():
            return []
        lines = self._path.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-n:]]
