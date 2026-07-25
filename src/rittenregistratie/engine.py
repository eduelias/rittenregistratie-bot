"""Core orchestration: turn a parsed message into an audit-compliant trip.

Multi-car aware: the sender's phone number selects the car, and each car has its
own state file, Excel logs and seed origin/odometer.

This module contains no AI and never fabricates or reclassifies trips. It only
*invokes* the configured DeltaAllocator and PrivateCapPlugin, whose default
implementations are inert.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from .cars import Car, CarRegistry
from .config import Settings, load_yaml
from .excel_writer import ExcelWriter
from .models import (
    ParsedMessage,
    Trip,
    TripSource,
    TripType,
)
from .parser import parse_message
from .plugins import registry
from .routes import RouteBook
from .state import State, StateStore


class EngineError(ValueError):
    pass


class UnknownCarError(EngineError):
    """Raised when a sender number is not associated with any car."""


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.excel_dir = settings.data_dir
        self.routebook = RouteBook(
            load_yaml(settings.locations_file),
            load_yaml(settings.routes_file),
        )
        self._locations = load_yaml(settings.locations_file)
        self._routes = load_yaml(settings.routes_file)
        self.cars = CarRegistry(
            load_yaml(settings.cars_file),
            fallback_seed_address=settings.seed_address,
            fallback_seed_odometer=settings.seed_odometer,
        )

        # Instantiate configured plugins (shared across cars).
        traj_cls = registry.get_trajectory_provider(settings.trajectory_provider)
        self.trajectory = (
            traj_cls(settings.google_maps_api_key)
            if settings.trajectory_provider == "google"
            else traj_cls()
        )
        self.allocator = registry.get_delta_allocator(settings.delta_allocator)()
        self.cap_plugin = registry.get_private_cap_plugin(
            settings.private_cap_plugin
        )()

    def resolve_car(self, sender: str) -> Car:
        car = self.cars.resolve(sender)
        if car is None:
            raise UnknownCarError(
                f"Number {sender} is not registered to any car."
            )
        return car

    def handle_text(
        self, text: str, sender: str, now: datetime | None = None
    ) -> str:
        now = now or datetime.now()
        car = self.resolve_car(sender)
        parsed: ParsedMessage = parse_message(text)

        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)
        excel = ExcelWriter(self.excel_dir, car.car_id)

        if parsed.end_odo < state.last_odometer:
            raise EngineError(
                f"[{car.label}] odometer {parsed.end_odo} is lower than last "
                f"recorded {state.last_odometer}."
            )

        origin_name = state.last_address or car.seed_address
        start_odo = state.last_odometer
        end_odo = parsed.end_odo
        trip_km = end_odo - start_odo

        start_address = self.routebook.address_for(origin_name)
        end_address = self.routebook.address_for(parsed.destination)

        route_info = self.routebook.lookup(origin_name, parsed.destination)
        traj = self.trajectory.get_trajectory(start_address, end_address)
        route_str = traj.summary + (f" ({traj.url})" if traj.url else "")

        deviation_note = ""
        delta = 0
        if route_info is not None:
            expected = route_info.nearest_variant(trip_km)
            if trip_km != expected:
                deviation_note = f"Actual {trip_km} km vs expected {expected} km."
            delta = max(0, trip_km - expected)

        trip_type = TripType.PRIVATE if parsed.is_private else TripType.BUSINESS

        private_ytd = state.private_ytd(now.year)
        if trip_type is TripType.PRIVATE:
            private_ytd = state.add_private(now.year, trip_km)
        if delta:
            state.add_delta(now.year, delta)

        trip = Trip(
            date=now,
            trip_type=trip_type,
            start_address=start_address,
            end_address=end_address,
            start_odo=start_odo,
            end_odo=end_odo,
            route=route_str,
            deviation_note=deviation_note,
            source=TripSource.WHATSAPP,
            private_km_ytd=private_ytd,
        )
        excel.append_trip(trip)

        state.last_odometer = end_odo
        state.last_address = parsed.destination

        cap = self.cap_plugin.on_evaluate(
            now.year, private_ytd, self.settings.private_cap_km, [trip]
        )

        store.save(state)

        lines: List[str] = [
            f"[{car.label}] {origin_name} -> {parsed.destination} "
            f"({trip_km} km, {trip_type.value}).",
            f"Odometer {end_odo}.",
        ]
        if deviation_note:
            lines.append(deviation_note)
        if cap and cap.message:
            lines.append(cap.message)
        return "\n".join(lines)
