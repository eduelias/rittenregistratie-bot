"""Data models shared across the core and plugins.

These are the stable contract that plugin packages depend on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TripType(str, Enum):
    BUSINESS = "business"
    PRIVATE = "private"
    # 'virtual' is produced only by external plugins, never by the core.
    VIRTUAL = "virtual"


class TripSource(str, Enum):
    WHATSAPP = "whatsapp"
    GENERATED = "generated"  # produced by an external DeltaAllocator plugin
    MERCEDES = "mercedes"


@dataclass
class OdometerReading:
    """A single odometer snapshot from an OdometerSource."""

    odometer_km: int
    timestamp: datetime
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@dataclass
class TrajectoryResult:
    """Result of resolving the route between two addresses."""

    summary: str
    distance_km: Optional[float] = None
    url: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class Trip:
    """A single audit-relevant trip row.

    Fields map directly onto the Belastingdienst 'sluitende rittenregistratie'
    requirements: date, begin/end odometer, begin/end address, driven route
    (with a note when it deviates from the usual route) and the
    business/private character of the trip.
    """

    date: datetime
    trip_type: TripType
    start_address: str
    end_address: str
    start_odo: int
    end_odo: int
    route: str = ""
    deviation_note: str = ""
    source: TripSource = TripSource.WHATSAPP
    private_km_ytd: int = 0

    @property
    def trip_km(self) -> int:
        return self.end_odo - self.start_odo


@dataclass
class VirtualTrip:
    """A trip proposed by an external DeltaAllocator plugin.

    The core never creates these itself; it only provides the type so plugins
    have a stable return contract.
    """

    end_address: str
    km: int
    date: datetime
    note: str = ""


@dataclass
class CapAction:
    """Result of a PrivateCapPlugin evaluation."""

    message: str
    converted_km: int = 0
    modified: bool = False


@dataclass
class ParsedMessage:
    """Structured form of an inbound WhatsApp text message."""

    end_odo: int
    destination: str
    is_private: bool = False
    note: str = ""
    raw: str = ""
