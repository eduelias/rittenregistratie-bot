"""Persistent state: last odometer/address and yearly counters.

Kept intentionally tiny (single-car scope) as a JSON file so it is trivial to
inspect and back up.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class State:
    last_odometer: int = 0
    last_address: str = ""
    # keyed by year (string) -> integer km
    private_km: Dict[str, int] = field(default_factory=dict)
    # a trip awaiting an address for an unknown, non-private destination
    pending: Optional[dict] = None
    # the last automatically logged trip whose end place had no name yet:
    # {"address", "lat", "lon"}; 'name <place>' turns it into a known location
    last_unnamed: Optional[dict] = None

    def private_ytd(self, year: int) -> int:
        return self.private_km.get(str(year), 0)

    def add_private(self, year: int, km: int) -> int:
        y = str(year)
        self.private_km[y] = self.private_km.get(y, 0) + km
        return self.private_km[y]


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # Tolerate keys from older versions that are no longer part of State.
        known = State.__dataclass_fields__.keys()
        data = {k: v for k, v in data.items() if k in known}
        return State(**data)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8"
        )

    def seed_if_empty(self, address: str, odometer: int) -> State:
        """Seed the first-trip origin + starting odometer if no state exists."""
        if self.path.exists():
            return self.load()
        state = State(last_odometer=odometer, last_address=address)
        self.save(state)
        return state
