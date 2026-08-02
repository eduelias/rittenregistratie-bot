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
from .config import Settings, load_yaml, save_location
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
from .whatsapp import reverse_geocode


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
        from .onboarding import OnboardingStore
        self.onboarding = OnboardingStore(settings.onboarding_file)

        # Instantiate configured plugins (shared across cars). Any trajectory
        # provider whose constructor accepts an ``api_key`` parameter receives
        # the configured Google key; others are constructed with no arguments.
        traj_cls = registry.get_trajectory_provider(settings.trajectory_provider)
        self.trajectory = self._instantiate_trajectory(
            traj_cls, settings.google_maps_api_key
        )
        self.allocator = registry.get_delta_allocator(settings.delta_allocator)()
        self.cap_plugin = registry.get_private_cap_plugin(
            settings.private_cap_plugin
        )()

    @staticmethod
    def _instantiate_trajectory(traj_cls, api_key: str):
        """Construct a trajectory provider, passing api_key only if accepted."""
        import inspect

        try:
            params = inspect.signature(traj_cls).parameters
        except (TypeError, ValueError):
            params = {}
        if "api_key" in params:
            return traj_cls(api_key)
        return traj_cls()

    def resolve_car(self, sender: str) -> Car:
        car = self.cars.resolve(sender)
        if car is None:
            raise UnknownCarError(
                f"Number {sender} is not registered to any car."
            )
        return car

    # --- onboarding ---------------------------------------------------
    def reload_cars(self) -> None:
        self.cars = CarRegistry(
            load_yaml(self.settings.cars_file),
            fallback_seed_address=self.settings.seed_address,
            fallback_seed_odometer=self.settings.seed_odometer,
        )

    def is_admin(self, sender: str) -> bool:
        from .cars import normalize_phone
        return normalize_phone(sender) in self.settings.admin_list()

    def register_join_request(self, sender: str, first_message: str) -> bool:
        """Record a pending join request. Returns True if newly added."""
        return self.onboarding.add(sender, first_message)

    def handle_admin_command(self, cmd: str, args: list) -> str:
        """Process an admin onboarding command; returns a reply for the admin."""
        from .cars import (
            add_car_to_yaml, normalize_phone, remove_car_from_yaml, slugify_car_id,
        )

        if cmd == "help":
            return (
                "Admin commands:\n"
                "pending — list join requests\n"
                "approve <number> <label> <seed_odo> [address...] — add a user\n"
                "deny <number> — reject a request\n"
                "list — list registered users\n"
                "remove <number|car_id> — remove a user"
            )
        if cmd == "list":
            cars = self.cars._cars
            lines = [f"Registered users ({len(cars)}/{self.settings.max_users}):"]
            for cid, car in cars.items():
                lines.append(
                    f"- {car.label} [{cid}] {', '.join(car.phones)} "
                    f"(seed {car.seed_odometer} km)"
                )
            return "\n".join(lines)
        if cmd == "remove":
            if not args:
                return "Usage: remove <number|car_id>"
            removed = remove_car_from_yaml(self.settings.cars_file, args[0])
            if not removed:
                return f"No user found for '{args[0]}'."
            self.reload_cars()
            return (
                f"Removed user '{removed}'. Their trip logs and state files are "
                f"kept on disk (trips-{removed}-*.xlsx, state-{removed}.json)."
            )
        if cmd == "pending":
            reqs = self.onboarding.list()
            if not reqs:
                return "No pending join requests."
            lines = ["Pending join requests:"]
            for r in reqs:
                lines.append(f"- {r.phone}: \"{r.first_message[:40]}\" ({r.requested_at})")
            lines.append("\nApprove: approve <number> <label> <seed_odo> [address]")
            return "\n".join(lines)
        if cmd == "deny":
            if not args:
                return "Usage: deny <number>"
            phone = normalize_phone(args[0])
            return (
                f"Denied and removed request from {phone}."
                if self.onboarding.remove(phone)
                else f"No pending request from {phone}."
            )
        if cmd == "approve":
            if len(args) < 3:
                return (
                    "Usage: approve <number> <label> <seed_odo> [address...]\n"
                    "e.g. approve 31612345678 \"Alice Golf\" 45000 Home"
                )
            phone = normalize_phone(args[0])
            # label may be quoted; support simple quoted first token or single word
            rest = args[1:]
            label, seed_odo, address = self._parse_approve_args(rest)
            if seed_odo is None:
                return "Could not find the seed odometer (a number). " \
                       "Usage: approve <number> <label> <seed_odo> [address]"
            if len(self.cars._cars) >= self.settings.max_users:
                return (
                    f"User limit reached ({self.settings.max_users}). "
                    f"Remove a user first (remove <number>) or raise "
                    f"RIT_MAX_USERS."
                )
            existing = list(self.cars._cars.keys())
            car_id = slugify_car_id(label, existing)
            try:
                add_car_to_yaml(
                    self.settings.cars_file, car_id, label,
                    address or self.settings.seed_address, seed_odo, phone,
                )
            except ValueError as exc:
                return f"Could not add: {exc}"
            self.onboarding.remove(phone)
            self.reload_cars()
            return (
                f"Approved {phone} as '{label}' (car id: {car_id}, "
                f"seed {seed_odo} km at {address or self.settings.seed_address})."
            )
        return "Unknown command. Send 'help'."

    @staticmethod
    def _parse_approve_args(rest: list):
        """From [label..., seed_odo, address...] extract label, odo, address.

        The seed odometer is the first purely-numeric token; everything before it
        is the label, everything after is the address.
        """
        odo_idx = next((i for i, t in enumerate(rest) if t.isdigit()), None)
        if odo_idx is None:
            label = " ".join(rest).strip('"')
            return label, None, ""
        label = " ".join(rest[:odo_idx]).strip('"') or "New car"
        seed_odo = int(rest[odo_idx])
        address = " ".join(rest[odo_idx + 1:]).strip('"')
        return label, seed_odo, address

    def handle_text(
        self, text: str, sender: str, now: datetime | None = None
    ) -> str:
        now = now or datetime.now()
        car = self.resolve_car(sender)
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)

        # If we are waiting for an address for a previously-parsed trip, this
        # text message supplies it.
        if state.pending:
            return self._resolve_pending_with_text(car, store, state, text)

        parsed: ParsedMessage = parse_message(text)

        if parsed.end_odo < state.last_odometer:
            raise EngineError(
                f"[{car.label}] odometer {parsed.end_odo} is lower than last "
                f"recorded {state.last_odometer}."
            )

        # Unknown, non-private destination without an address: ask for it.
        if (
            not parsed.is_private
            and not self.routebook.is_known(parsed.destination)
        ):
            state.pending = {
                "end_odo": parsed.end_odo,
                "destination": parsed.destination,
                "is_private": parsed.is_private,
                "note": parsed.note,
            }
            store.save(state)
            return (
                f"I don't have an address for '{parsed.destination}'.\n"
                "Please reply with the full address, or share the location "
                "(WhatsApp attach \u2192 Location) and I'll extract it."
            )

        return self._commit_trip(
            car, store, state, now,
            end_odo=parsed.end_odo,
            destination=parsed.destination,
            is_private=parsed.is_private,
        )

    def handle_location(
        self, sender: str, latitude, longitude, address: str = "",
        now: datetime | None = None,
    ) -> str:
        """Handle a shared WhatsApp location, resolving a pending trip."""
        now = now or datetime.now()
        car = self.resolve_car(sender)
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)

        if not state.pending:
            return (
                "Thanks, but I wasn't waiting for a location. Send "
                "<odometer> <destination> first."
            )

        resolved = address or reverse_geocode(
            latitude, longitude, self.settings.google_maps_api_key
        )
        if not resolved:
            resolved = f"{latitude},{longitude}"
        return self._save_location_and_commit(car, store, state, now, resolved)

    def _resolve_pending_with_text(
        self, car: Car, store: StateStore, state: State, text: str
    ) -> str:
        address = (text or "").strip()
        if not address:
            return "Please reply with the address, or share the location."
        return self._save_location_and_commit(
            car, store, state, datetime.now(), address
        )

    def _save_location_and_commit(
        self, car: Car, store: StateStore, state: State,
        now: datetime, address: str,
    ) -> str:
        pending = state.pending or {}
        destination = pending.get("destination", "")
        # Learn the location so it is not asked again.
        save_location(self.settings.locations_file, destination, address)
        self.routebook = RouteBook(
            load_yaml(self.settings.locations_file),
            load_yaml(self.settings.routes_file),
        )
        state.pending = None
        store.save(state)
        return self._commit_trip(
            car, store, state, now,
            end_odo=pending["end_odo"],
            destination=destination,
            is_private=pending.get("is_private", False),
            learned=address,
        )

    def _commit_trip(
        self, car: Car, store: StateStore, state: State, now: datetime,
        *, end_odo: int, destination: str, is_private: bool,
        learned: str = "",
    ) -> str:
        excel = ExcelWriter(self.excel_dir, car.car_id)

        origin_name = state.last_address or car.seed_address
        start_odo = state.last_odometer
        trip_km = end_odo - start_odo

        start_address = self.routebook.address_for(origin_name)
        end_address = self.routebook.address_for(destination)

        route_info = self.routebook.lookup(origin_name, destination)
        traj = self.trajectory.get_trajectory(start_address, end_address)
        route_str = traj.summary + (f" ({traj.url})" if traj.url else "")

        deviation_note = ""
        delta = 0
        if route_info is not None:
            expected = route_info.nearest_variant(trip_km)
            if trip_km != expected:
                deviation_note = f"Actual {trip_km} km vs expected {expected} km."
            delta = max(0, trip_km - expected)

        trip_type = TripType.PRIVATE if is_private else TripType.BUSINESS

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
        state.last_address = destination

        cap = self.cap_plugin.on_evaluate(
            now.year, private_ytd, self.settings.private_cap_km, [trip]
        )

        store.save(state)

        lines: List[str] = [
            f"[{car.label}] {origin_name} -> {destination} "
            f"({trip_km} km, {trip_type.value}).",
            f"Odometer {end_odo}.",
        ]
        if learned:
            lines.append(f"Saved address for '{destination}': {learned}")
        if deviation_note:
            lines.append(deviation_note)
        if cap and cap.message:
            lines.append(cap.message)
        return "\n".join(lines)
