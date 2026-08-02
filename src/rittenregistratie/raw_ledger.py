"""Immutable raw trip ledger (append-only).

Records every trip exactly as the user reported it, before it is written to the
spreadsheet. This preserves the pristine, unmodified source of truth so the
compliant spreadsheet can always be rebuilt from it if needed.

Format: JSON Lines (one JSON object per line) at
``data/raw-ledger-<car_id>.jsonl``. The file is only ever appended to; existing
lines are never modified or removed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class RawTrip:
    """A trip exactly as reported by the user (pre-plugin, unmodified)."""

    timestamp: str          # ISO datetime the trip was recorded
    start_odo: int
    end_odo: int
    start_address: str
    end_address: str
    destination_raw: str    # the destination text the user typed
    is_private: bool
    note: str = ""
    raw_message: str = ""   # the original message text, verbatim


class RawLedger:
    def __init__(self, path: Path):
        self.path = path

    def append(self, trip: RawTrip) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(trip), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self) -> List[RawTrip]:
        if not self.path.exists():
            return []
        out: List[RawTrip] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(RawTrip(**json.loads(line)))
        return out
