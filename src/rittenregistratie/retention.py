"""Delete a former subscriber's trip data once the retention period is over.

The privacy statement promises that trips and addresses are kept for
``RIT_RETENTION_DAYS`` (default 90) after a subscription ends, so the customer
can still ask for their workbook, and are deleted after that. This does it:

    python -m rittenregistratie.retention            # dry run: list what is due
    python -m rittenregistratie.retention --apply    # delete it

Run daily by ``deploy/rittenregistratie-retention.timer``.

Only signups that are ENDED (access over, car already out of cars.yaml) are
considered, and a car id that is in cars.yaml again, or belongs to another live
signup, is never touched. The operator's own cars never have a signup, so they
are out of reach. The signup record itself stays (name, number, Mollie ids):
that is billing data with its own, longer, fiscal retention.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .billing import ENDED, SignupStore
from .cars import CarRegistry, normalize_phone
from .config import Settings, load_yaml


@dataclass
class Purge:
    signup_id: str
    car_id: str
    phone: str
    access_until: str
    files: List[Path] = field(default_factory=list)


def _files_for(settings: Settings, car_id: str) -> List[Path]:
    data = settings.data_dir
    candidates = [
        settings.raw_ledger_file(car_id),
        settings.state_file(car_id),
        settings.car_learned_locations_file(car_id),
        *sorted(settings.exports_dir.glob(f"trips-{car_id}-*.xlsx")),
    ]
    return [p for p in candidates if p.exists() and data in p.parents]


def due(settings: Settings, today: Optional[date] = None) -> List[Purge]:
    """Everything whose retention period is over, without touching it."""
    today = today or date.today()
    keep = timedelta(days=settings.retention_days)
    registered = {c.car_id for c in CarRegistry(load_yaml(settings.cars_file)).all()}
    signups = SignupStore(settings.signups_file).all()
    live_cars = {s.car_id for s in signups if s.status != ENDED and s.car_id}
    out = []
    for s in signups:
        if s.status != ENDED or s.data_deleted_at or not s.car_id or not s.access_until:
            continue
        if s.car_id in registered or s.car_id in live_cars:
            continue
        if date.fromisoformat(s.access_until) + keep >= today:
            continue
        out.append(Purge(s.id, s.car_id, s.phone, s.access_until, _files_for(settings, s.car_id)))
    return out


def apply(settings: Settings, purges: List[Purge]) -> None:
    store = SignupStore(settings.signups_file)
    active = settings.active_car_file
    for p in purges:
        for f in p.files:
            f.unlink(missing_ok=True)
        if active.exists():
            try:
                mapping = json.loads(active.read_text(encoding="utf-8")) or {}
            except ValueError:
                mapping = {}
            if mapping.pop(normalize_phone(p.phone), None) is not None:
                active.write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")
        signup = store.get(p.signup_id)
        if signup is not None:
            signup.data_deleted_at = datetime.now().isoformat(timespec="seconds")
            signup.note(f"retention: trip data of {p.car_id} deleted ({len(p.files)} files)")
            store.save(signup)


def main(argv: Optional[List[str]] = None) -> int:
    from .config import get_settings
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="delete (default: only list)")
    args = ap.parse_args(argv)
    settings = get_settings()
    purges = due(settings)
    for p in purges:
        names = ", ".join(f.name for f in p.files) or "no files left"
        print(f"{p.car_id} (signup {p.signup_id[:6]}, ended {p.access_until}): {names}")
    if args.apply and purges:
        apply(settings, purges)
        print(f"deleted trip data of {len(purges)} former subscriber(s)")
    elif not purges:
        print(f"nothing due (retention {settings.retention_days} days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
