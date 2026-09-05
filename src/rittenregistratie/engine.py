"""Core orchestration: turn a parsed message into an audit-compliant trip.

Multi-car aware: the sender's phone number selects the car, and each car has its
own state file, Excel logs and seed origin/odometer.

This module contains no AI and never fabricates, invents, or reclassifies
trips. The distance recorded for each trip is always the real odometer
difference reported by the user. Trips are stored only in the append-only raw
ledger; spreadsheets are generated on request (see :mod:`.export`), which is
also the only point where plugins may transform data, via the event bus.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .cars import ActiveCarStore, Car, CarRegistry
from .config import Settings, load_yaml, save_location
from .events import EventBus, load_event_plugins
from .export import ExportService
from .models import (
    ParsedMessage,
    Reply,
    Trip,
    TripSource,
    TripType,
    VehicleTripReport,
)
from .parser import ParseError, parse_message
from .plugins import registry
from .routes import RouteBook
from .state import State, StateStore
from .whatsapp import reverse_geocode


class EngineError(ValueError):
    pass


class UnknownCarError(EngineError):
    """Raised when a sender number is not associated with any car."""


# Replies (case-insensitive) that drop a trip waiting for an address.
_CANCEL_WORDS = frozenset({"cancel", "/cancel", "skip", "stop", "annuleer"})
_HAS_LETTER_RE = re.compile(r"[^\W\d_]")
# Commands that request a spreadsheet: "excel", "excel 2025", "excel all",
# and for admins "excel <car_id> [year|all]".
_EXPORT_WORDS = frozenset({"excel", "export", "xlsx", "sheet", "spreadsheet"})


@dataclass
class ExportRequest:
    car: Car
    years: List[int]


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.routebook = self._load_routebook()
        self.cars = CarRegistry(
            load_yaml(settings.cars_file),
            fallback_seed_address=settings.seed_address,
            fallback_seed_odometer=settings.seed_odometer,
        )
        from .onboarding import OnboardingStore
        self.onboarding = OnboardingStore(settings.onboarding_file)
        self.active_cars = ActiveCarStore(settings.active_car_file)

        # Instantiate configured plugins (shared across cars). Any trajectory
        # provider whose constructor accepts an ``api_key`` parameter receives
        # the configured Google key; others are constructed with no arguments.
        traj_cls = registry.get_trajectory_provider(settings.trajectory_provider)
        self.trajectory = self._instantiate_trajectory(
            traj_cls, settings.google_maps_api_key
        )
        self.cap_plugin = registry.get_private_cap_plugin(
            settings.private_cap_plugin
        )()
        # Optional per-user override cap plugin (only for allow-listed numbers).
        self.cap_plugin_override = None
        if settings.private_cap_plugin_override:
            self.cap_plugin_override = registry.get_private_cap_plugin(
                settings.private_cap_plugin_override
            )()
        # Per-car cap plugin instances (cars.yaml ``cap_plugin``), by name.
        self._cap_instances: Dict[str, object] = {}

        # Event buses are per car: each car's ``event_plugins`` (or the global
        # RIT_EVENT_PLUGINS) subscribe to that car's bus. Built lazily.
        self._buses: Dict[str, EventBus] = {}
        self.loaded_event_plugins: Dict[str, List[str]] = {}
        self.exporter = ExportService(settings, self.bus_for)

    def bus_for(self, car_id: str) -> EventBus:
        """The event bus for one car, with that car's plugins subscribed."""
        bus = self._buses.get(car_id)
        if bus is None:
            car = self.cars.get(car_id)
            selection = (
                car.event_plugins if car is not None and car.event_plugins is not None
                else self.settings.event_plugin_selection()
            )
            bus = EventBus()
            self.loaded_event_plugins[car_id] = load_event_plugins(bus, selection)
            self._buses[car_id] = bus
        return bus

    def _load_routebook(self) -> RouteBook:
        """Seed locations from config, then learned locations from data/."""
        return RouteBook(
            load_yaml(self.settings.locations_file),
            load_yaml(self.settings.routes_file),
            load_yaml(self.settings.learned_locations_file),
        )

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

    def resolve_car(self, sender: str, hint: str | None = None) -> Car:
        """The car a message from ``sender`` is about.

        ``hint`` names a car by id or label (message prefix or command
        argument); it must be one of the sender's cars, or any car for an
        admin. Without a hint: the sender's only car, or their active car when
        they have several. Raises UnknownCarError for unregistered numbers and
        EngineError when the choice is ambiguous or the hint is wrong.
        """
        own = self.cars.cars_for(sender)
        if hint:
            car = next((c for c in own if c.matches(hint)), None)
            if car is None and self.is_admin(sender):
                car = self.cars.find(hint)
            if car is None:
                raise EngineError(
                    f"Unknown car '{hint}'. Your cars: {self._car_list(own)}."
                    if own else f"Unknown car '{hint}'."
                )
            return car
        if not own:
            raise UnknownCarError(
                f"Number {sender} is not registered to any car."
            )
        if len(own) == 1:
            return own[0]
        active = self.active_cars.get(sender)
        car = next((c for c in own if c.car_id == active), None)
        if car is None:
            raise EngineError(
                f"You report for several cars: {self._car_list(own)}. Send "
                "'car <id>' to choose one, or start the message with the car "
                "id, e.g. '{0} 145230 Office'.".format(own[0].car_id)
            )
        return car

    @staticmethod
    def _car_list(cars: List[Car]) -> str:
        return ", ".join(f"{c.label} [{c.car_id}]" for c in cars) or "none"

    def _split_car_prefix(self, text: str, sender: str) -> tuple[Optional[Car], str]:
        """``van 302050 Office`` -> (van, "302050 Office"); else (None, text)."""
        parts = (text or "").strip().split(maxsplit=1)
        if len(parts) == 2 and not parts[0].isdigit():
            car = next((c for c in self.cars.cars_for(sender) if c.matches(parts[0])), None)
            if car is not None:
                return car, parts[1]
        return None, text

    def handle_user_command(self, text: str, sender: str) -> Optional[str]:
        """``cars`` lists the sender's cars; ``car <id|label>`` picks the
        active one. None if ``text`` is not one of these commands."""
        parts = (text or "").strip().split()
        if not parts:
            return None
        cmd = parts[0].lower().lstrip("/")
        if cmd not in ("cars", "car", "name"):
            return None
        own = self.cars.cars_for(sender)
        if not own:
            raise UnknownCarError(f"Number {sender} is not registered to any car.")
        if cmd == "name":
            return self._name_last_unnamed(sender, " ".join(parts[1:]).strip())
        if cmd == "car" and len(parts) > 1:
            car = self.resolve_car(sender, " ".join(parts[1:]))
            if car not in own:
                raise EngineError("You can only activate one of your own cars.")
            self.active_cars.set(sender, car.car_id)
            return Reply(f"Active car: {car.label} [{car.car_id}].")
        active = self.active_cars.get(sender) if len(own) > 1 else own[0].car_id
        lines = ["Your cars:"]
        for c in own:
            mark = " (active)" if c.car_id == active else ""
            lines.append(f"- {c.label} [{c.car_id}]{mark}")
        if len(own) > 1:
            lines.append("Switch with 'car <id>' or prefix a message with the car id.")
        return Reply("\n".join(lines))

    def _name_last_unnamed(self, sender: str, place: str) -> Reply:
        """``name <place>``: give the last automatically logged, unnamed end
        position a name, so future trips there resolve to it."""
        car = self.resolve_car(sender)
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)
        if not place:
            return Reply("Usage: name <place>  (names the last unnamed trip end).")
        if not state.last_unnamed:
            return Reply("Nothing to name: the last automatic trip ended at a known place.")
        info = state.last_unnamed
        save_location(
            self.settings.learned_locations_file, place, info["address"],
            lat=info.get("lat"), lon=info.get("lon"),
        )
        self.routebook = self._load_routebook()
        state.last_unnamed = None
        store.save(state)
        return Reply(f"Saved '{place}': {info['address']}. Future trips ending there will use it.")

    # --- vehicle telemetry ------------------------------------------------
    def _place_for(self, report: VehicleTripReport) -> tuple[Optional[str], str, bool]:
        """Where a reported trip ended: ``(known_name, address, is_private)``.

        Order: a known place name from the source, a known geofence/zone name,
        the nearest known location within radius of the coordinates, then
        reverse geocoding (or the bare coordinates) with ``known_name`` None.
        """
        for hint in (report.place, report.zone):
            if hint and self.routebook.is_known(hint):
                return hint, self.routebook.address_for(hint), self.routebook.is_private_place(hint)
        if report.latitude is not None and report.longitude is not None:
            hit = self.routebook.nearest(
                report.latitude, report.longitude, float(self.settings.place_radius_m)
            )
            if hit is not None:
                name, address, _dist = hit
                return name, address, self.routebook.is_private_place(name)
            address = reverse_geocode(
                report.latitude, report.longitude, self.settings.google_maps_api_key
            ) or f"{report.latitude:.5f},{report.longitude:.5f}"
            return None, address, False
        return None, report.zone or "(unknown location)", False

    def handle_vehicle_trip(self, car: Car, report: VehicleTripReport) -> Optional[Reply]:
        """Log a trip reported by a vehicle-telemetry plugin. Returns None when
        nothing is logged (reading not above the last one), else the same
        Reply a typed message would get, with ``notice`` set when the end place
        is unknown so the user is asked to name it."""
        now = report.ended_at or datetime.now()
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)
        if report.end_odo <= state.last_odometer:
            return None  # already logged (manually or by an earlier event), or 0 km

        name, address, is_private = self._place_for(report)
        note_bits = [f"auto-logged from {report.source or 'vehicle'}"]
        if report.start_odo is not None and report.start_odo != state.last_odometer:
            gap = report.start_odo - state.last_odometer
            note_bits.append(
                f"reading at ignition on was {report.start_odo}; {gap} km before this trip "
                "are not covered by any row"
            )
        if name is None:
            note_bits.append("end place not known; reply 'name <place>' to teach it")
        destination = name if name is not None else address
        raw = f"[{report.source or 'vehicle'}] end_odo={report.end_odo}"
        if report.latitude is not None and report.longitude is not None:
            raw += f" at {report.latitude:.5f},{report.longitude:.5f}"
        if report.zone:
            raw += f" zone={report.zone}"

        reply = self._commit_trip(
            car, store, state, now,
            end_odo=report.end_odo, destination=destination, is_private=is_private,
            note="; ".join(note_bits), raw_message=raw,
        )
        state = store.load()
        if name is None:
            state.last_unnamed = {
                "address": address, "lat": report.latitude, "lon": report.longitude,
            }
            extra = "\nI don't know this place. Reply 'name <place>' to teach me it."
        else:
            state.last_unnamed = None
            extra = ""
        store.save(state)
        return Reply(str(reply) + extra, logged=True, notice=bool(extra) or reply.notice)

    # --- onboarding ---------------------------------------------------
    def reload_cars(self) -> None:
        self.cars = CarRegistry(
            load_yaml(self.settings.cars_file),
            fallback_seed_address=self.settings.seed_address,
            fallback_seed_odometer=self.settings.seed_odometer,
        )
        # Plugin selection lives in cars.yaml; rebuild buses on next use.
        self._buses.clear()

    def is_admin(self, sender: str) -> bool:
        from .cars import normalize_phone
        return normalize_phone(sender) in self.settings.admin_list()

    def _uses_override(self, car: Car) -> bool:
        """True if this car uses the override private-cap plugin (which may edit
        the generated spreadsheet)."""
        if self.cap_plugin_override is None:
            return False
        allow = set(self.settings.cap_override_list())
        return bool(allow and any(p in allow for p in car.phones))

    def _cap_for_car(self, car: Car):
        """The private-cap plugin for a car: ``cap_plugin`` from cars.yaml if
        set; else the legacy per-number override; else the default."""
        if car.cap_plugin:
            inst = self._cap_instances.get(car.cap_plugin)
            if inst is None:
                inst = registry.get_private_cap_plugin(car.cap_plugin)()
                self._cap_instances[car.cap_plugin] = inst
            return inst
        if self._uses_override(car):
            return self.cap_plugin_override
        return self.cap_plugin

    def register_join_request(self, sender: str, first_message: str) -> bool:
        """Record a pending join request. Returns True if newly added."""
        return self.onboarding.add(sender, first_message)

    def handle_admin_command(self, cmd: str, args: list) -> str:
        """Process an admin onboarding command; returns a reply for the admin."""
        from .cars import (
            add_car_to_yaml, add_phone_to_car_yaml, normalize_phone,
            remove_car_from_yaml, remove_phone_from_car_yaml, slugify_car_id,
        )

        if cmd == "help":
            return (
                "Admin commands:\n"
                "pending — list join requests\n"
                "approve <number> <label> <seed_odo> [address...] — add a car "
                "for a number (a number may have several cars)\n"
                "assign <number> <car_id> — let a number report for an existing car\n"
                "unassign <number> <car_id> — undo assign\n"
                "deny <number> — reject a request\n"
                "list — list cars, numbers and plugins\n"
                "remove <number|car_id> — remove a car, or a number from all cars\n"
                "Everyone: cars — list your cars; car <id> — choose the active one; "
                "excel [car] [year|all] — spreadsheet."
            )
        if cmd == "list":
            cars = self.cars.all()
            lines = [f"Registered cars ({len(cars)}/{self.settings.max_users}):"]
            for car in cars:
                plugins = []
                if car.event_plugins is not None:
                    plugins.append(f"events={','.join(car.event_plugins) or 'none'}")
                if car.cap_plugin:
                    plugins.append(f"cap={car.cap_plugin}")
                extra = f" [{'; '.join(plugins)}]" if plugins else ""
                lines.append(
                    f"- {car.label} [{car.car_id}] {', '.join(car.phones) or 'no numbers'} "
                    f"(seed {car.seed_odometer} km){extra}"
                )
            return "\n".join(lines)
        if cmd == "remove":
            if not args:
                return "Usage: remove <number|car_id>"
            result = remove_car_from_yaml(self.settings.cars_file, args[0])
            if not result:
                return f"No car or number found for '{args[0]}'."
            self.reload_cars()
            parts = []
            if result.removed_cars:
                parts.append(
                    "Removed car(s) " + ", ".join(f"'{c}'" for c in result.removed_cars)
                    + ". Their ledger and state files are kept on disk."
                )
            detached = [c for c in result.detached_from if c not in result.removed_cars]
            if detached:
                parts.append(
                    f"Number {normalize_phone(args[0])} no longer reports for "
                    + ", ".join(f"'{c}'" for c in detached) + "."
                )
            return " ".join(parts)
        if cmd in ("assign", "unassign"):
            if len(args) < 2:
                return f"Usage: {cmd} <number> <car_id>"
            phone, car_id = normalize_phone(args[0]), args[1]
            try:
                if cmd == "assign":
                    changed = add_phone_to_car_yaml(self.settings.cars_file, car_id, phone)
                else:
                    changed = remove_phone_from_car_yaml(self.settings.cars_file, car_id, phone)
            except ValueError as exc:
                return f"Could not {cmd}: {exc}"
            self.reload_cars()
            self.onboarding.remove(phone)
            if cmd == "assign":
                return (f"{phone} now reports for '{car_id}'." if changed
                        else f"{phone} already reports for '{car_id}'.")
            return (f"{phone} no longer reports for '{car_id}'." if changed
                    else f"{phone} was not assigned to '{car_id}'.")
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
            if len(self.cars.all()) >= self.settings.max_users:
                return (
                    f"User limit reached ({self.settings.max_users}). "
                    f"Remove a user first (remove <number>) or raise "
                    f"RIT_MAX_USERS."
                )
            existing = [c.car_id for c in self.cars.all()]
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

    def parse_export_command(
        self, text: str, sender: str, now: datetime | None = None,
    ) -> Optional[ExportRequest]:
        """Recognise a spreadsheet request; None if ``text`` is not one.

        ``excel`` exports the current year of the sender's car; ``excel 2025``
        a given year; ``excel all`` every year with trips. Admins may name a car
        first: ``excel van 2025``. Raises EngineError for bad arguments.
        """
        parts = (text or "").strip().split()
        if not parts or parts[0].lower().lstrip("/") not in _EXPORT_WORDS:
            return None
        now = now or datetime.now()
        years: List[int] = []
        want_all = False
        name_words: List[str] = []
        for arg in parts[1:]:
            low = arg.lower()
            if low == "all":
                want_all = True
            elif low.isdigit() and len(low) == 4:
                years.append(int(low))
            else:
                name_words.append(arg)  # car id or (multi-word) label
        # Own car by id/label without admin; any car for admins.
        car = self.resolve_car(sender, " ".join(name_words) or None)
        if want_all:
            years = self.exporter.years(car.car_id)
            if not years:
                raise EngineError(f"No trips recorded yet for {car.label}.")
        elif not years:
            years = [now.year]
        return ExportRequest(car=car, years=sorted(set(years)))

    def handle_text(
        self, text: str, sender: str, now: datetime | None = None,
        car: Car | None = None,
    ) -> str:
        now = now or datetime.now()
        if car is None:
            car, text = self._split_car_prefix(text, sender)
        if car is None:
            car = self.resolve_car(sender)
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)

        # If we are waiting for an address for a previously-parsed trip, this
        # text message supplies it.
        if state.pending:
            return self._resolve_pending_with_text(
                car, store, state, text, sender, now
            )

        parsed: ParsedMessage = parse_message(text)

        # The odometer has not moved, so whatever this is, it is not a trip.
        # Repeating the last reading and place is a duplicate; repeating the
        # reading with a different place used to write a 0 km row, which is how
        # a message meant as a comment ("20772 private") ended up in the
        # logbook as a journey.
        if parsed.end_odo == state.last_odometer:
            where = (
                f" at {state.last_address}" if state.last_address
                and self.routebook.address_for(parsed.destination)
                == self.routebook.address_for(state.last_address) else ""
            )
            return Reply(
                f"[{car.label}] Already logged: odometer {parsed.end_odo}{where}. "
                f"Nothing added — the odometer has not moved since."
            )

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
                "raw": parsed.raw,
            }
            store.save(state)
            return Reply(
                f"I don't have an address for '{parsed.destination}'.\n"
                "Please reply with the full address, the name of a known "
                "location, or share the location (WhatsApp attach \u2192 "
                "Location). Send 'cancel' to drop this trip."
            )

        return self._commit_trip(
            car, store, state, now,
            end_odo=parsed.end_odo,
            destination=parsed.destination,
            is_private=parsed.is_private,
            note=parsed.note,
            raw_message=parsed.raw,
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
            return Reply(
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
        self, car: Car, store: StateStore, state: State, text: str,
        sender: str, now: datetime,
    ) -> str:
        """Interpret a text received while a trip waits for an address.

        The reply may be: an address (learned and used), the name of a known
        location (its address is reused), 'cancel' (the pending trip is
        dropped), or a brand-new trip like ``20672 spg`` (the pending trip is
        dropped and the new one is processed, so a trip is never silently
        recorded with an odometer reading as its address).
        """
        answer = (text or "").strip()
        pending = state.pending or {}
        destination = pending.get("destination", "")
        if not answer:
            return Reply("Please reply with the address, or share the location.")

        if answer.lower() in _CANCEL_WORDS:
            state.pending = None
            store.save(state)
            return Reply(
                f"Cancelled: the trip to '{destination}' (odometer "
                f"{pending.get('end_odo')}) was not logged."
            )

        try:
            as_trip = parse_message(answer)
        except ParseError:
            as_trip = None
        if as_trip is not None and as_trip.end_odo >= state.last_odometer:
            state.pending = None
            store.save(state)
            inner = self.handle_text(answer, sender, now, car=car)
            return Reply(
                f"Dropped the pending trip to '{destination}' (odometer "
                f"{pending.get('end_odo')}): no address was given, so it was "
                "not logged.\n" + inner,
                logged=getattr(inner, "logged", False),
                notice=True,
            )

        if self.routebook.is_known(answer):
            address = self.routebook.address_for(answer)
        elif not _HAS_LETTER_RE.search(answer):
            return Reply(
                "That doesn't look like an address. Reply with a street address "
                "(e.g. 'Waldorpstraat 3, Den Haag') or the name of a known "
                "location, share the location, or send 'cancel'."
            )
        else:
            address = answer
        return self._save_location_and_commit(car, store, state, now, address)

    def _save_location_and_commit(
        self, car: Car, store: StateStore, state: State,
        now: datetime, address: str,
    ) -> str:
        pending = state.pending or {}
        destination = pending.get("destination", "")
        # Learn the location so it is not asked again. Learned entries live in
        # data/, separate from the committed seed config.
        save_location(self.settings.learned_locations_file, destination, address)
        self.routebook = self._load_routebook()
        state.pending = None
        store.save(state)
        return self._commit_trip(
            car, store, state, now,
            end_odo=pending["end_odo"],
            destination=destination,
            is_private=pending.get("is_private", False),
            learned=address,
            note=pending.get("note", ""),
            raw_message=pending.get("raw", ""),
        )

    def _commit_trip(
        self, car: Car, store: StateStore, state: State, now: datetime,
        *, end_odo: int, destination: str, is_private: bool,
        learned: str = "", note: str = "", raw_message: str = "",
    ) -> str:
        origin_name = state.last_address or car.seed_address
        start_odo = state.last_odometer
        trip_km = end_odo - start_odo

        start_address = self.routebook.address_for(origin_name)
        end_address = self.routebook.address_for(destination)

        # The Belastingdienst wants the driven route recorded when it was not
        # the most usual one. The raw ledger has no field for a route, so
        # looking one up here reports it once and then loses it; that decision
        # belongs to export time, where the whole year is available. Enable
        # RIT_ROUTE_ON_DEVIATION to have it mentioned in the reply anyway.
        route_str = ""
        if self.settings.route_on_deviation:
            route_info = self.routebook.lookup(origin_name, destination)
            if route_info is not None and trip_km != route_info.nearest_variant(trip_km):
                traj = self.trajectory.get_trajectory(start_address, end_address)
                route_str = traj.summary or ""

        trip_type = TripType.PRIVATE if is_private else TripType.BUSINESS

        private_ytd = state.private_ytd(now.year)
        if trip_type is TripType.PRIVATE:
            private_ytd = state.add_private(now.year, trip_km)

        trip = Trip(
            date=now,
            trip_type=trip_type,
            start_address=start_address,
            end_address=end_address,
            start_odo=start_odo,
            end_odo=end_odo,
            route=route_str,
            private_detour_km=0,
            source=TripSource.WHATSAPP,
        )

        # The append-only raw ledger is the only store. Every trip is written
        # exactly as reported; spreadsheets are generated from it on request
        # and this file is never modified after writing.
        from .raw_ledger import RawLedger, RawTrip
        RawLedger(self.settings.raw_ledger_file(car.car_id)).append(RawTrip(
            timestamp=now.isoformat(timespec="seconds"),
            start_odo=start_odo,
            end_odo=end_odo,
            start_address=start_address,
            end_address=end_address,
            destination_raw=destination,
            is_private=is_private,
            note=note,
            raw_message=raw_message,
        ))

        state.last_odometer = end_odo
        state.last_address = destination

        cap = self._cap_for_car(car).on_evaluate(
            now.year, private_ytd, self.settings.private_cap_km, [trip],
            context={
                "data_dir": self.settings.data_dir,
                "config_dir": self.settings.config_dir,
                "car_id": car.car_id,
                "year": now.year,
            },
        )
        store.save(state)

        lines: List[str] = [
            f"[{car.label}] {origin_name} -> {destination} "
            f"({trip_km} km, {trip_type.value}).",
            f"Odometer {end_odo}.",
        ]
        if learned:
            lines.append(f"Saved address for '{destination}': {learned}")
        if route_str:
            lines.append(f"Route recorded (deviation): {route_str}")
        if cap and cap.message:
            lines.append(cap.message)
        return Reply(
            "\n".join(lines),
            logged=True,
            notice=bool(
                learned or route_str
                or (cap and cap.message and getattr(cap, "important", False))
            ),
        )
