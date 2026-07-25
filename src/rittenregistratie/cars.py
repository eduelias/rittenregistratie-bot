"""Car registry: maps sender phone numbers to a car.

Identifies which car (and its own trip log / state / seed) a message belongs to,
based on the WhatsApp sender number. This turns the logger from single-car into
multi-car while keeping each car's administration fully separate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


def normalize_phone(phone: str) -> str:
    """Reduce a phone number to digits only (E.164 without '+')."""
    return re.sub(r"\D", "", phone or "")


@dataclass
class Car:
    car_id: str
    label: str
    seed_address: str
    seed_odometer: int
    phones: List[str]


class CarRegistry:
    def __init__(self, cars: Dict[str, dict], fallback_seed_address: str = "Home",
                 fallback_seed_odometer: int = 0):
        self._cars: Dict[str, Car] = {}
        self._by_phone: Dict[str, Car] = {}
        for car_id, val in (cars or {}).items():
            car = Car(
                car_id=car_id,
                label=val.get("label", car_id),
                seed_address=val.get("seed_address", fallback_seed_address),
                seed_odometer=int(val.get("seed_odometer", fallback_seed_odometer)),
                phones=[normalize_phone(p) for p in val.get("phones", [])],
            )
            self._cars[car_id] = car
            for phone in car.phones:
                if phone in self._by_phone:
                    raise ValueError(
                        f"Phone {phone} is assigned to multiple cars "
                        f"({self._by_phone[phone].car_id} and {car_id})."
                    )
                self._by_phone[phone] = car

    def resolve(self, phone: str) -> Optional[Car]:
        return self._by_phone.get(normalize_phone(phone))

    def get(self, car_id: str) -> Optional[Car]:
        return self._cars.get(car_id)

    def all_phones(self) -> List[str]:
        return list(self._by_phone.keys())

    def is_empty(self) -> bool:
        return not self._cars
