"""Self-service onboarding: let new numbers request to join; admin approves.

Flow:
- An unregistered number messages the bot -> a pending join request is stored
  and the sender is told an admin will review it. Admins are notified.
- An admin sends: ``approve <number> <label> <seed_odo> [seed_address...]``
  -> a new car is registered for the number and the user is welcomed. A
  number may own several cars, so approving an already-registered number
  adds a second car for it.
- ``assign <number> <car_id>`` lets another number report for an existing
  car (a shared car); ``unassign <number> <car_id>`` reverses it.
- ``pending`` lists requests; ``deny <number>`` removes one.

Pending requests are persisted to ``data/onboarding.json`` so they survive
restarts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .cars import add_car_to_yaml, normalize_phone, slugify_car_id


@dataclass
class JoinRequest:
    phone: str
    first_message: str
    requested_at: str


class OnboardingStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def add(self, phone: str, first_message: str) -> bool:
        """Record a pending request. Returns True if newly added."""
        phone = normalize_phone(phone)
        data = self._load()
        if phone in data:
            return False
        data[phone] = asdict(
            JoinRequest(phone, first_message[:200], datetime.now().isoformat(timespec="seconds"))
        )
        self._save(data)
        return True

    def has(self, phone: str) -> bool:
        return normalize_phone(phone) in self._load()

    def list(self) -> List[JoinRequest]:
        return [JoinRequest(**v) for v in self._load().values()]

    def remove(self, phone: str) -> bool:
        phone = normalize_phone(phone)
        data = self._load()
        if phone in data:
            del data[phone]
            self._save(data)
            return True
        return False


class AdminError(ValueError):
    """Raised for malformed admin commands."""


@dataclass
class ApprovalResult:
    car_id: str
    label: str
    phone: str
    seed_odometer: int
    seed_address: str


def parse_admin_command(text: str) -> tuple[str, list[str]]:
    """Return (command, args) for an admin message, or ('', []) if not one."""
    parts = (text or "").strip().split()
    if not parts:
        return "", []
    cmd = parts[0].lower().lstrip("/")
    if cmd in ("approve", "deny", "pending", "help", "list", "remove",
               "assign", "unassign"):
        return cmd, parts[1:]
    return "", []
