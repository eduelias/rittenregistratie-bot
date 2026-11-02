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

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .cars import ActiveCarStore, Car, CarRegistry, normalize_phone
from .config import Settings, load_yaml, save_location
from .events import EventBus, load_event_plugins
from .i18n import Key, LanguageStore, render_parts, set_default, t
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

log = logging.getLogger("rittenregistratie.engine")


class EngineError(ValueError):
    pass


class UnknownCarError(EngineError):
    """Raised when a sender number is not associated with any car."""


# Replies (case-insensitive) that drop a trip waiting for an address.
_CANCEL_WORDS = frozenset({"cancel", "/cancel", "skip", "stop", "annuleer", "annuleren"})
_HAS_LETTER_RE = re.compile(r"[^\W\d_]")
# A registered driver's first message is usually a greeting (the website's
# WhatsApp link pre-fills "start"); answer it with how the bot works instead
# of failing to parse it as a trip.
_HELP_WORDS = ("start", "help", "hulp", "hallo", "hoi", "hi", "hello")
# Dutch spellings of the car commands.
_CAR_ALIASES = {"auto": "car", "autos": "cars", "auto's": "cars", "naam": "name"}
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
        self.languages = LanguageStore(settings.languages_file)
        set_default(settings.default_language)

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
        # Location books of subscriber cars, by car id (see book()).
        self._books: Dict[str, RouteBook] = {}
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
            if car is not None and car.is_subscriber:
                selection = self._subscriber_plugins(car, selection)
            bus = EventBus()
            self.loaded_event_plugins[car_id] = load_event_plugins(bus, selection)
            self._buses[car_id] = bus
        return bus

    def _subscriber_plugins(self, car: Car, selection) -> List[str]:
        """A subscriber's event plugins: what the car asks for, but only names
        RIT_SUBSCRIBER_EVENT_PLUGINS allows. Never "all installed" and never
        the global default, so the operator's own plugins stay with the
        operator however cars.yaml or .env are edited."""
        allowed = set(self.settings.subscriber_event_plugin_list())
        wanted = selection if selection is not None else []
        refused = [n for n in wanted if n not in allowed]
        if refused:
            log.warning("Car %s is a subscriber; not loading event plugins %s.",
                        car.car_id, ", ".join(refused))
        return [n for n in wanted if n in allowed]

    def _load_routebook(self) -> RouteBook:
        """Seed locations from config, then learned locations from data/."""
        return RouteBook(
            load_yaml(self.settings.locations_file),
            load_yaml(self.settings.routes_file),
            load_yaml(self.settings.learned_locations_file),
        )

    def book(self, car: Optional[Car]) -> RouteBook:
        """The places a car's messages resolve against.

        The operator's cars share the committed seed config and
        ``data/locations-learned.yaml``. A subscriber's car has a book of its
        own, holding only what that subscriber taught it: someone else's
        "home" must never end up as their address, nor theirs in anyone's.
        """
        if car is None or not car.is_subscriber:
            return self.routebook
        book = self._books.get(car.car_id)
        if book is None:
            book = RouteBook({}, {}, load_yaml(self._learned_file(car)))
            self._books[car.car_id] = book
        return book

    def _learned_file(self, car: Optional[Car]) -> Path:
        if car is not None and car.is_subscriber:
            return self.settings.car_learned_locations_file(car.car_id)
        return self.settings.learned_locations_file

    def _learn(self, car: Car, name: str, address: str, **extra) -> None:
        """Store a learned place in the car's own book and reload it."""
        save_location(self._learned_file(car), name, address, **extra)
        if car.is_subscriber:
            self._books.pop(car.car_id, None)
        else:
            self.routebook = self._load_routebook()

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
                    t("car.unknown_list", hint=hint, cars=self._car_list(own))
                    if own else t("car.unknown", hint=hint)
                )
            return car
        if not own:
            raise UnknownCarError(t("car.not_registered", phone=sender))
        if len(own) == 1:
            return own[0]
        active = self.active_cars.get(sender)
        car = next((c for c in own if c.car_id == active), None)
        if car is None:
            raise EngineError(t("car.several", cars=self._car_list(own), example=own[0].car_id))
        return car

    @staticmethod
    def _car_list(cars: List[Car]) -> str:
        return ", ".join(f"{c.label} [{c.car_id}]" for c in cars) or t("car.none")

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
        cmd = _CAR_ALIASES.get(cmd, cmd)
        if cmd not in ("cars", "car", "name") + _HELP_WORDS:
            return None
        own = self.cars.cars_for(sender)
        if not own:
            raise UnknownCarError(t("car.not_registered", phone=sender))
        if cmd in _HELP_WORDS:
            return Reply(t("help.user", label=own[0].label))
        if cmd == "name":
            return self._name_last_unnamed(sender, " ".join(parts[1:]).strip())
        if cmd == "car" and len(parts) > 1:
            car = self.resolve_car(sender, " ".join(parts[1:]))
            if car not in own:
                raise EngineError(t("car.only_own"))
            self.active_cars.set(sender, car.car_id)
            return Reply(t("car.active", label=car.label, id=car.car_id))
        active = self.active_cars.get(sender) if len(own) > 1 else own[0].car_id
        lines = [t("car.list")]
        for c in own:
            mark = t("car.active_mark") if c.car_id == active else ""
            lines.append(f"- {c.label} [{c.car_id}]{mark}")
        if len(own) > 1:
            lines.append(t("car.switch_hint"))
        return Reply("\n".join(lines))

    def _name_last_unnamed(self, sender: str, place: str) -> Reply:
        """``name <place>``: give the last automatically logged, unnamed end
        position a name, so future trips there resolve to it."""
        car = self.resolve_car(sender)
        store = StateStore(self.settings.state_file(car.car_id))
        state = store.seed_if_empty(car.seed_address, car.seed_odometer)
        if not place:
            return Reply(t("name.usage"))
        if not state.last_unnamed:
            return Reply(t("name.nothing"))
        info = state.last_unnamed
        self._learn(car, place, info["address"], lat=info.get("lat"), lon=info.get("lon"))
        state.last_unnamed = None
        store.save(state)
        return Reply(t("name.saved", place=place, address=info["address"]))

    # --- vehicle telemetry ------------------------------------------------
    def _place_for(self, car: Car, report: VehicleTripReport) -> tuple[Optional[str], str, bool]:
        """Where a reported trip ended: ``(known_name, address, is_private)``.

        Order: a known place name from the source, a known geofence/zone name,
        the nearest known location within radius of the coordinates, then
        reverse geocoding (or the bare coordinates) with ``known_name`` None.
        """
        book = self.book(car)
        for hint in (report.place, report.zone):
            if hint and book.is_known(hint):
                return hint, book.address_for(hint), book.is_private_place(hint)
        if report.latitude is not None and report.longitude is not None:
            hit = book.nearest(
                report.latitude, report.longitude, float(self.settings.place_radius_m)
            )
            if hit is not None:
                name, address, _dist = hit
                return name, address, book.is_private_place(name)
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

        name, address, is_private = self._place_for(car, report)
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
            extra = [("vehicle.unknown_place", {})]
        else:
            state.last_unnamed = None
            extra = []
        store.save(state)
        # Keep the facts: the template that announces a car-logged trip is
        # filled from them, and this is the only reply that ever reaches it.
        # Keep the parts: every driver of the car gets it in their language.
        parts = list(reply.parts or [str(reply)]) + extra
        return Reply(render_parts(parts), logged=True, notice=bool(extra) or reply.notice,
                     facts=reply.facts, parts=parts)

    # --- onboarding ---------------------------------------------------
    def reload_cars(self) -> None:
        self.cars = CarRegistry(
            load_yaml(self.settings.cars_file),
            fallback_seed_address=self.settings.seed_address,
            fallback_seed_odometer=self.settings.seed_odometer,
        )
        # Plugin selection lives in cars.yaml; rebuild buses on next use.
        self._buses.clear()
        self._books.clear()

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
        set; else the legacy per-number override; else the default. A
        subscriber's car always gets RIT_SUBSCRIBER_CAP_PLUGIN."""
        name = self.settings.subscriber_cap_plugin if car.is_subscriber else car.cap_plugin
        if name:
            inst = self._cap_instances.get(name)
            if inst is None:
                inst = registry.get_private_cap_plugin(name)()
                self._cap_instances[name] = inst
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
            return t("admin.help")
        if cmd == "list":
            cars = self.cars.all()
            lines = [t("admin.list", n=len(cars), max=self.settings.max_users)]
            for car in cars:
                plugins = []
                if car.event_plugins is not None:
                    plugins.append(f"events={','.join(car.event_plugins) or 'none'}")
                if car.cap_plugin:
                    plugins.append(f"cap={car.cap_plugin}")
                extra = f" [{'; '.join(plugins)}]" if plugins else ""
                lines.append(t("admin.list_line", label=car.label, id=car.car_id,
                               phones=", ".join(car.phones) or t("admin.no_numbers"),
                               seed=car.seed_odometer, extra=extra))
            return "\n".join(lines)
        if cmd == "remove":
            if not args:
                return t("admin.remove_usage")
            result = remove_car_from_yaml(self.settings.cars_file, args[0])
            if not result:
                return t("admin.remove_none", what=args[0])
            self.reload_cars()
            parts = []
            if result.removed_cars:
                parts.append(t("admin.removed_cars",
                               cars=", ".join(f"'{c}'" for c in result.removed_cars)))
            detached = [c for c in result.detached_from if c not in result.removed_cars]
            if detached:
                parts.append(t("admin.detached", phone=normalize_phone(args[0]),
                               cars=", ".join(f"'{c}'" for c in detached)))
            return " ".join(parts)
        if cmd in ("assign", "unassign"):
            if len(args) < 2:
                return t("admin.assign_usage", cmd=cmd)
            phone, car_id = normalize_phone(args[0]), args[1]
            try:
                if cmd == "assign":
                    changed = add_phone_to_car_yaml(self.settings.cars_file, car_id, phone)
                else:
                    changed = remove_phone_from_car_yaml(self.settings.cars_file, car_id, phone)
            except ValueError as exc:
                return t("admin.assign_failed", cmd=cmd, error=exc)
            self.reload_cars()
            self.onboarding.remove(phone)
            if cmd == "assign":
                return t("admin.assigned" if changed else "admin.already_assigned",
                         phone=phone, car=car_id)
            return t("admin.unassigned" if changed else "admin.not_assigned",
                     phone=phone, car=car_id)
        if cmd == "pending":
            reqs = self.onboarding.list()
            if not reqs:
                return t("admin.no_pending")
            lines = [t("admin.pending")]
            for r in reqs:
                lines.append(f"- {r.phone}: \"{r.first_message[:40]}\" ({r.requested_at})")
            lines.append(t("admin.pending_hint"))
            return "\n".join(lines)
        if cmd == "deny":
            if not args:
                return t("admin.deny_usage")
            phone = normalize_phone(args[0])
            return t("admin.denied" if self.onboarding.remove(phone) else "admin.no_request",
                     phone=phone)
        if cmd == "approve":
            if len(args) < 3:
                return t("admin.approve_usage")
            phone = normalize_phone(args[0])
            # label may be quoted; support simple quoted first token or single word
            rest = args[1:]
            label, seed_odo, address = self._parse_approve_args(rest)
            if seed_odo is None:
                return t("admin.no_seed")
            if len(self.cars.all()) >= self.settings.max_users:
                return t("admin.full", max=self.settings.max_users)
            existing = [c.car_id for c in self.cars.all()]
            car_id = slugify_car_id(label, existing)
            try:
                add_car_to_yaml(
                    self.settings.cars_file, car_id, label,
                    address or self.settings.seed_address, seed_odo, phone,
                )
            except ValueError as exc:
                return t("admin.add_failed", error=exc)
            self.onboarding.remove(phone)
            self.reload_cars()
            return t("admin.approved", phone=phone, label=label, id=car_id, seed=seed_odo,
                     address=address or self.settings.seed_address)
        return t("admin.unknown")

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
                raise EngineError(t("export.no_trips", label=car.label))
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
            same_place = bool(
                state.last_address
                and self.book(car).address_for(parsed.destination)
                == self.book(car).address_for(state.last_address)
            )
            if same_place:
                return Reply(t("trip.already_at", label=car.label, odo=parsed.end_odo,
                               place=state.last_address))
            return Reply(t("trip.already", label=car.label, odo=parsed.end_odo))

        if parsed.end_odo < state.last_odometer:
            raise EngineError(t("trip.lower", label=car.label, odo=parsed.end_odo,
                                last=state.last_odometer))

        # Unknown, non-private destination without an address: ask for it.
        if (
            not parsed.is_private
            and not self.book(car).is_known(parsed.destination)
        ):
            state.pending = {
                "end_odo": parsed.end_odo,
                "destination": parsed.destination,
                "is_private": parsed.is_private,
                "note": parsed.note,
                "raw": parsed.raw,
                "sender": normalize_phone(sender),
            }
            store.save(state)
            return Reply(t("trip.ask_address", destination=parsed.destination))

        return self._commit_trip(
            car, store, state, now,
            end_odo=parsed.end_odo,
            destination=parsed.destination,
            is_private=parsed.is_private,
            note=parsed.note,
            raw_message=parsed.raw,
            sender=sender,
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
            return Reply(t("loc.not_waiting"))

        resolved = address or reverse_geocode(
            latitude, longitude, self.settings.google_maps_api_key
        )
        if not resolved:
            resolved = f"{latitude},{longitude}"
        return self._save_location_and_commit(car, store, state, now, resolved, sender)

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
            return Reply(t("addr.reply"))

        if answer.lower() in _CANCEL_WORDS:
            state.pending = None
            store.save(state)
            return Reply(t("trip.cancelled", destination=destination, odo=pending.get("end_odo")))

        try:
            as_trip = parse_message(answer)
        except ParseError:
            as_trip = None
        if as_trip is not None and as_trip.end_odo >= state.last_odometer:
            state.pending = None
            store.save(state)
            inner = self.handle_text(answer, sender, now, car=car)
            return Reply(
                t("trip.dropped", destination=destination, odo=pending.get("end_odo")) + inner,
                logged=getattr(inner, "logged", False),
                notice=True,
            )

        if self.book(car).is_known(answer):
            address = self.book(car).address_for(answer)
        elif not _HAS_LETTER_RE.search(answer):
            return Reply(t("addr.invalid"))
        else:
            address = answer
        return self._save_location_and_commit(car, store, state, now, address, sender)

    def _save_location_and_commit(
        self, car: Car, store: StateStore, state: State,
        now: datetime, address: str, sender: str = "",
    ) -> str:
        pending = state.pending or {}
        destination = pending.get("destination", "")
        # Learn the location so it is not asked again. Learned entries live in
        # data/, separate from the committed seed config.
        self._learn(car, destination, address)
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
            # Whoever sent the reading drove, not whoever supplied the address.
            sender=pending.get("sender") or sender,
        )

    def _commit_trip(
        self, car: Car, store: StateStore, state: State, now: datetime,
        *, end_odo: int, destination: str, is_private: bool,
        learned: str = "", note: str = "", raw_message: str = "",
        sender: str = "",
    ) -> str:
        origin_name = state.last_address or car.seed_address
        start_odo = state.last_odometer
        trip_km = end_odo - start_odo

        book = self.book(car)
        start_address = book.address_for(origin_name)
        end_address = book.address_for(destination)

        # The Belastingdienst wants the driven route recorded when it was not
        # the most usual one. The raw ledger has no field for a route, so
        # looking one up here reports it once and then loses it; that decision
        # belongs to export time, where the whole year is available. Enable
        # RIT_ROUTE_ON_DEVIATION to have it mentioned in the reply anyway.
        route_str = ""
        if self.settings.route_on_deviation:
            route_info = book.lookup(origin_name, destination)
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
            sender=normalize_phone(sender),
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

        parts: list = [
            ("trip.summary", {"label": car.label, "origin": origin_name, "destination": destination,
                              "km": trip_km, "purpose": Key(f"purpose.{trip_type.value}")}),
            ("trip.odometer", {"odo": end_odo}),
        ]
        if learned:
            parts.append(("trip.saved_address", {"destination": destination, "address": learned}))
        if route_str:
            parts.append(("trip.route", {"route": route_str}))
        if cap and cap.message:
            parts.append(cap.message)
        return Reply(
            render_parts(parts),
            parts=parts,
            logged=True,
            notice=bool(
                learned or route_str
                or (cap and cap.message and getattr(cap, "important", False))
            ),
            facts={
                "label": car.label, "origin": origin_name, "destination": destination,
                "km": trip_km, "purpose": trip_type.value, "odometer": end_odo,
            },
        )
