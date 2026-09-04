"""Car registry: maps sender phone numbers to cars, many-to-many.

A car may be reported by several phones (a shared car) and one phone may drive
several cars. Each car keeps its own ledger, state and seed, and selects its
own plugins. ``cars.yaml``::

    mercedes:
      label: "Mercedes"
      seed_address: "Home"
      seed_odometer: 18811
      phones: ["31612345678", "31698765432"]   # both report for this car
      event_plugins: ["reallocate"]   # optional; default: RIT_EVENT_PLUGINS
      cap_plugin: "warn"              # optional; default: RIT_PRIVATE_CAP_PLUGIN
    van:
      label: "Delivery van"
      seed_address: "Depot"
      seed_odometer: 302000
      phones: ["31612345678"]          # same person, second car

When a phone has more than one car, the sender chooses with ``car <id>`` (kept
as their active car, see :class:`ActiveCarStore`) or by starting a message
with the car id or label: ``van 302050 Office``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


def normalize_phone(phone: str) -> str:
    """Reduce a phone number to digits only (E.164 without '+')."""
    return re.sub(r"\D", "", phone or "")


def _plugin_list(value) -> Optional[List[str]]:
    """None when the key is absent (use the global default); else a list."""
    if value is None:
        return None
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p).strip() for p in value if str(p).strip()]


@dataclass
class Car:
    car_id: str
    label: str
    seed_address: str
    seed_odometer: int
    phones: List[str] = field(default_factory=list)
    # Per-car plugin selection. None = fall back to the global setting.
    event_plugins: Optional[List[str]] = None
    cap_plugin: Optional[str] = None

    def matches(self, token: str) -> bool:
        """True if ``token`` names this car by id or label (case-insensitive)."""
        t = (token or "").strip().lower()
        return bool(t) and t in (self.car_id.lower(), self.label.lower())


class CarRegistry:
    def __init__(self, cars: Dict[str, dict], fallback_seed_address: str = "Home",
                 fallback_seed_odometer: int = 0):
        self._cars: Dict[str, Car] = {}
        self._by_phone: Dict[str, List[Car]] = {}
        for car_id, val in (cars or {}).items():
            val = val or {}
            car = Car(
                car_id=car_id,
                label=val.get("label", car_id),
                seed_address=val.get("seed_address", fallback_seed_address),
                seed_odometer=int(val.get("seed_odometer", fallback_seed_odometer)),
                phones=[normalize_phone(p) for p in val.get("phones", []) if normalize_phone(p)],
                event_plugins=_plugin_list(val.get("event_plugins")),
                cap_plugin=(str(val["cap_plugin"]).strip() or None) if val.get("cap_plugin") else None,
            )
            self._cars[car_id] = car
            for phone in dict.fromkeys(car.phones):  # de-duplicate, keep order
                self._by_phone.setdefault(phone, []).append(car)

    def cars_for(self, phone: str) -> List[Car]:
        """Every car this phone may report for (possibly none, possibly several)."""
        return list(self._by_phone.get(normalize_phone(phone), []))

    def is_registered(self, phone: str) -> bool:
        return bool(self._by_phone.get(normalize_phone(phone)))

    def resolve(self, phone: str) -> Optional[Car]:
        """The phone's car when it has exactly one; None when zero or several.

        Callers that must handle several cars use :meth:`cars_for`.
        """
        cars = self.cars_for(phone)
        return cars[0] if len(cars) == 1 else None

    def get(self, car_id: str) -> Optional[Car]:
        return self._cars.get(car_id)

    def find(self, token: str) -> Optional[Car]:
        """Look a car up by id, then by label (case-insensitive)."""
        if token in self._cars:
            return self._cars[token]
        for car in self._cars.values():
            if car.matches(token):
                return car
        return None

    def all(self) -> List[Car]:
        return list(self._cars.values())

    def all_phones(self) -> List[str]:
        return list(self._by_phone.keys())

    def is_empty(self) -> bool:
        return not self._cars


# --- cars.yaml editing (used by admin commands) ------------------------------

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8"
    )


def add_car_to_yaml(
    path: Path, car_id: str, label: str, seed_address: str,
    seed_odometer: int, phone: str,
    event_plugins: Optional[List[str]] = None, cap_plugin: Optional[str] = None,
) -> None:
    """Add a new car for ``phone``. The phone may already own other cars."""
    data = _load(path)
    if car_id in data:
        raise ValueError(f"Car id '{car_id}' already exists.")
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("A phone number is required.")
    entry = {
        "label": label,
        "seed_address": seed_address,
        "seed_odometer": int(seed_odometer),
        "phones": [phone],
    }
    if event_plugins is not None:
        entry["event_plugins"] = list(event_plugins)
    if cap_plugin:
        entry["cap_plugin"] = cap_plugin
    data[car_id] = entry
    _save(path, data)


def add_phone_to_car_yaml(path: Path, car_id: str, phone: str) -> bool:
    """Let ``phone`` report for an existing car. False if it already does."""
    data = _load(path)
    if car_id not in data:
        raise ValueError(f"Unknown car '{car_id}'.")
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("A phone number is required.")
    phones = [normalize_phone(p) for p in data[car_id].get("phones", [])]
    if phone in phones:
        return False
    data[car_id]["phones"] = phones + [phone]
    _save(path, data)
    return True


def remove_phone_from_car_yaml(path: Path, car_id: str, phone: str) -> bool:
    """Stop ``phone`` reporting for ``car_id``. False if it was not assigned."""
    data = _load(path)
    if car_id not in data:
        raise ValueError(f"Unknown car '{car_id}'.")
    phone = normalize_phone(phone)
    phones = [normalize_phone(p) for p in data[car_id].get("phones", [])]
    if phone not in phones:
        return False
    data[car_id]["phones"] = [p for p in phones if p != phone]
    _save(path, data)
    return True


@dataclass
class Removal:
    removed_cars: List[str] = field(default_factory=list)   # car ids deleted
    detached_from: List[str] = field(default_factory=list)  # car ids the phone left

    def __bool__(self) -> bool:
        return bool(self.removed_cars or self.detached_from)


def remove_car_from_yaml(path: Path, identifier: str) -> Removal:
    """Remove by car id (deletes the car) or by phone (detaches the phone from
    every car; a car left without phones is deleted). Data files stay on disk.
    """
    result = Removal()
    data = _load(path)
    if not data:
        return result
    if identifier in data:
        del data[identifier]
        result.removed_cars.append(identifier)
        _save(path, data)
        return result
    phone = normalize_phone(identifier)
    if not phone:
        return result
    for cid in list(data):
        phones = [normalize_phone(p) for p in data[cid].get("phones", [])]
        if phone not in phones:
            continue
        remaining = [p for p in phones if p != phone]
        result.detached_from.append(cid)
        if remaining:
            data[cid]["phones"] = remaining
        else:
            del data[cid]
            result.removed_cars.append(cid)
    if result:
        _save(path, data)
    return result


def slugify_car_id(label: str, existing: List[str]) -> str:
    """Make a unique, filesystem-safe car id from a label."""
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "car"
    cid = base
    i = 2
    while cid in existing:
        cid = f"{base}_{i}"
        i += 1
    return cid


# --- which car a multi-car phone is currently reporting for ------------------

class ActiveCarStore:
    """Per-phone active car, for phones that drive several cars.

    A tiny JSON map ``{phone: car_id}`` under ``data/``. Phones with a single
    car never need it.
    """

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except ValueError:
            return {}

    def get(self, phone: str) -> Optional[str]:
        return self._load().get(normalize_phone(phone))

    def set(self, phone: str, car_id: str) -> None:
        data = self._load()
        data[normalize_phone(phone)] = car_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def clear(self, phone: str) -> None:
        data = self._load()
        if data.pop(normalize_phone(phone), None) is not None:
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
