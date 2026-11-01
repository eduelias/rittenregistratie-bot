"""Who can still get a plain WhatsApp text: the 24-hour customer service window.

Meta delivers a free-form text only within 24 hours of the recipient's last
message to the bot. Outside it the API still answers 200 and the message
fails later (error 131047), so a sender cannot find out in time. This keeps,
per number, when it last wrote to us (``data/last-inbound.json``, one
timestamp per number, nothing else), and :meth:`is_open` answers with a
safety margin so a closing window is treated as closed.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from .cars import normalize_phone

MARGIN = timedelta(hours=23)


class InboundWindow:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except ValueError:
            return {}

    def seen(self, phone: str, now: Optional[datetime] = None) -> None:
        """Record an inbound message from ``phone``."""
        now = now or datetime.now()
        with self._lock:
            data = self._load()
            data[normalize_phone(phone)] = now.isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def is_open(self, phone: str, now: Optional[datetime] = None) -> bool:
        last = self._load().get(normalize_phone(phone))
        if not last:
            return False
        return (now or datetime.now()) - datetime.fromisoformat(last) < MARGIN
