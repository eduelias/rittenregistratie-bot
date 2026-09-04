"""Data models for the trip recorder.

These describe a trip exactly as the user reports it. The core records what it
receives and never fabricates, invents, or reclassifies trips.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class TripType(str, Enum):
    BUSINESS = "business"
    PRIVATE = "private"


class TripSource(str, Enum):
    WHATSAPP = "whatsapp"
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
    """A single audit-relevant trip row, exactly as reported by the user.

    Fields map directly onto the Belastingdienst 'sluitende rittenregistratie'
    requirements: date, begin/end odometer, begin/end address, driven route
    (with a note when it deviates from the usual route) and the
    business/private character of the trip. The recorded distance is always the
    real odometer difference.
    """

    date: datetime
    trip_type: TripType
    start_address: str
    end_address: str
    start_odo: int
    end_odo: int
    route: str = ""
    private_detour_km: int = 0
    source: TripSource = TripSource.WHATSAPP

    @property
    def trip_km(self) -> int:
        return self.end_odo - self.start_odo


@dataclass
class CapAction:
    """Result of a PrivateCapPlugin evaluation. The core's default only reports a
    message (e.g. remaining private km); it never modifies recorded trips.

    ``important`` marks a message the user must see even when the bot only
    acknowledges trips with a reaction (e.g. the cap has been exceeded).
    """

    message: str
    important: bool = False


@dataclass
class VehicleTripReport:
    """A finished trip as reported by a vehicle-telemetry plugin.

    Produced by a ``hook.<plugin>`` handler from whatever the source posts
    (a car cloud, Home Assistant, an OBD dongle). The core turns it into a
    ledger row exactly like a typed message would: ``end_odo`` is the reading,
    the place comes from ``place``/``zone`` if they name a known location,
    else from the nearest known location to the coordinates, else from
    reverse geocoding. Nothing here is guessed by the core.
    """

    end_odo: int
    ended_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    start_odo: Optional[int] = None       # reading at ignition on, if the source knows it
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    zone: str = ""                         # geofence name, e.g. "home"
    place: str = ""                        # a place name, if the source has one
    source: str = ""                       # plugin name, for the note
    raw: Optional[dict] = None


class Reply(str):
    """A user-facing reply. Behaves exactly like ``str``.

    Carries two flags for the transport layer: ``logged`` (a trip was written,
    so a thumbs-up reaction is an adequate acknowledgement) and ``notice``
    (the text holds information beyond a plain confirmation, e.g. a learned
    address, a route deviation or a cap warning, and should be sent even in
    reaction mode).
    """

    logged: bool
    notice: bool

    def __new__(cls, text: str = "", *, logged: bool = False, notice: bool = False):
        obj = super().__new__(cls, text)
        obj.logged = logged
        obj.notice = notice
        return obj


@dataclass
class ParsedMessage:
    """Structured form of an inbound WhatsApp text message."""

    end_odo: int
    destination: str
    is_private: bool = False
    note: str = ""
    raw: str = ""
