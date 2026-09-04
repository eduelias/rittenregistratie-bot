"""On-demand spreadsheet generation from the raw ledger.

The raw ledger (``data/raw-ledger-<car_id>.jsonl``) is the only place trips are
stored. A spreadsheet is a *view* of it, produced when asked for:

1. read the ledger rows for one car and one year;
2. emit ``export.pre_generate`` with those rows as plain dicts and take back
   whatever the handlers return (identity when no plugin is installed);
3. validate the returned rows (shape, types, closed odometer chain);
4. write a fresh workbook to ``data/exports/trips-<car_id>-<year>.xlsx``;
5. emit ``export.post_generate`` with the path and row count.

Nothing here writes to the ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .config import Settings
from .config import load_yaml as load_yaml_file
from .events import EXPORT_POST_GENERATE, EXPORT_PRE_GENERATE, EventBus
from .excel_writer import ExcelWriter
from .models import Trip, TripSource, TripType
from .raw_ledger import RawLedger

REQUIRED_FIELDS = (
    "timestamp", "start_odo", "end_odo", "start_address", "end_address", "is_private",
)


class ExportError(ValueError):
    """The export could not be produced (no data, or a plugin returned bad data)."""


@dataclass
class ExportResult:
    car_id: str
    year: int
    path: Path
    rows: int
    handlers: int  # number of pre_generate handlers that ran


def raw_to_trip(row: Dict[str, Any]) -> Trip:
    """Turn a (validated) ledger-shaped dict into a Trip for the Excel writer."""
    return Trip(
        date=datetime.fromisoformat(str(row["timestamp"])),
        trip_type=TripType.PRIVATE if row["is_private"] else TripType.BUSINESS,
        start_address=str(row["start_address"]),
        end_address=str(row["end_address"]),
        start_odo=int(row["start_odo"]),
        end_odo=int(row["end_odo"]),
        route=str(row.get("route") or ""),
        private_detour_km=int(row.get("private_detour_km") or 0),
        source=TripSource.WHATSAPP,
    )


def validate_trips(trips: Any) -> List[Dict[str, Any]]:
    """Check rows returned by ``export.pre_generate`` before they reach Excel.

    Each row must be a dict with the required fields, integer odometers with
    ``end_odo >= start_odo``, a parseable ISO timestamp and a boolean
    ``is_private``. Rows are sorted by timestamp and must form a non-decreasing
    odometer chain (each start is at or after the previous end). Raises
    :class:`ExportError` naming the offending row.
    """
    if not isinstance(trips, list):
        raise ExportError("plugin returned trips that are not a list")
    rows: List[Dict[str, Any]] = []
    for i, row in enumerate(trips, start=1):
        if not isinstance(row, dict):
            raise ExportError(f"row {i}: not an object")
        missing = [f for f in REQUIRED_FIELDS if f not in row]
        if missing:
            raise ExportError(f"row {i}: missing field(s) {', '.join(missing)}")
        try:
            start = int(row["start_odo"])
            end = int(row["end_odo"])
        except (TypeError, ValueError):
            raise ExportError(f"row {i}: odometer values must be integers") from None
        if isinstance(row["start_odo"], bool) or isinstance(row["end_odo"], bool):
            raise ExportError(f"row {i}: odometer values must be integers")
        if end < start:
            raise ExportError(f"row {i}: end_odo {end} is lower than start_odo {start}")
        if not isinstance(row["is_private"], bool):
            raise ExportError(f"row {i}: is_private must be true/false")
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]))
        except ValueError:
            raise ExportError(f"row {i}: timestamp is not ISO-8601") from None
        if not str(row["start_address"]).strip() or not str(row["end_address"]).strip():
            raise ExportError(f"row {i}: addresses must not be empty")
        clean = dict(row)
        clean["start_odo"], clean["end_odo"], clean["timestamp"] = start, end, ts.isoformat()
        rows.append(clean)
    rows.sort(key=lambda r: (r["timestamp"], r["start_odo"]))
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if cur["start_odo"] < prev["end_odo"]:
            raise ExportError(
                f"row {i + 1}: start_odo {cur['start_odo']} is lower than the "
                f"previous end_odo {prev['end_odo']} (odometer chain broken)"
            )
    return rows


BusProvider = Callable[[str], EventBus]


class ExportService:
    def __init__(self, settings: Settings, bus: Union[EventBus, BusProvider]):
        """``bus`` is one EventBus for every car, or a callable
        ``car_id -> EventBus`` so each car gets its own plugin selection."""
        self.settings = settings
        if isinstance(bus, EventBus):
            self._bus_for: BusProvider = lambda car_id: bus
        else:
            self._bus_for = bus

    def bus_for(self, car_id: str) -> EventBus:
        return self._bus_for(car_id)

    def ledger(self, car_id: str) -> RawLedger:
        return RawLedger(self.settings.raw_ledger_file(car_id))

    def years(self, car_id: str) -> List[int]:
        """Years that have at least one recorded trip, ascending."""
        years = {datetime.fromisoformat(t.timestamp).year for t in self.ledger(car_id).read_all()}
        return sorted(years)

    def trips_for_year(self, car_id: str, year: int) -> List[Dict[str, Any]]:
        return [
            asdict(t) for t in self.ledger(car_id).read_all()
            if datetime.fromisoformat(t.timestamp).year == year
        ]

    def generate(self, car_id: str, year: int, out_dir: Optional[Path] = None) -> ExportResult:
        trips = self.trips_for_year(car_id, year)
        if not trips:
            raise ExportError(f"No trips recorded for '{car_id}' in {year}.")

        payload = {
            "car_id": car_id,
            "year": year,
            "data_dir": str(self.settings.data_dir),
            "config_dir": str(self.settings.config_dir),
            "trips": trips,
        }
        bus = self.bus_for(car_id)
        handlers = len(bus.handlers(EXPORT_PRE_GENERATE))
        result = bus.emit(EXPORT_PRE_GENERATE, payload)
        if not isinstance(result, dict) or "trips" not in result:
            raise ExportError("export.pre_generate handler returned an invalid payload")
        rows = validate_trips(result["trips"])
        if not rows:
            raise ExportError("export.pre_generate handler returned no trips")

        out_dir = out_dir or self.settings.exports_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = ExcelWriter(out_dir, car_id).write_all(year, [raw_to_trip(r) for r in rows])

        bus.emit(EXPORT_POST_GENERATE, {
            "car_id": car_id, "year": year, "path": str(path), "rows": len(rows),
        })
        return ExportResult(car_id=car_id, year=year, path=path, rows=len(rows), handlers=handlers)


# --- CLI: dry-run the export pipeline (and any installed event plugins) -------

def main(argv=None) -> int:
    """``python -m rittenregistratie.export <car_id> [year ...] [--out DIR]``.

    Runs the same pipeline the WhatsApp ``excel`` command uses, against the
    real ledger, writing to ``--out`` (default: a temporary directory that is
    printed and kept). Use it to develop and check an event plugin without
    sending anything. ``--no-plugins`` runs the pass-through pipeline;
    ``--plugins a,b`` loads only those entry points.
    """
    import argparse
    import json
    import tempfile

    from .events import EventBus, load_event_plugins

    ap = argparse.ArgumentParser(prog="python -m rittenregistratie.export",
                                 description=main.__doc__.split("\n\n")[1])
    ap.add_argument("car_id")
    ap.add_argument("years", nargs="*", type=int, help="default: every year with trips")
    ap.add_argument("--out", type=Path, default=None, help="output dir (default: temp dir)")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--no-plugins", action="store_true", help="ignore installed event plugins")
    ap.add_argument("--plugins", default=None,
                    help="comma-separated entry-point names to load (overrides cars.yaml)")
    ap.add_argument("--json", action="store_true", help="also dump the validated rows as JSON lines")
    args = ap.parse_args(argv)

    kw = {}
    if args.data_dir:
        kw["data_dir"] = args.data_dir
    if args.config_dir:
        kw["config_dir"] = args.config_dir
    settings = Settings(**kw)

    from .cars import CarRegistry
    registry = CarRegistry(load_yaml_file(settings.cars_file))

    def bus_for(car_id: str) -> EventBus:
        bus = EventBus()
        if args.no_plugins:
            loaded: List[str] = []
        else:
            if args.plugins is not None:
                selection = [p for p in args.plugins.split(",") if p.strip()]
            else:
                car = registry.get(car_id)
                selection = (car.event_plugins if car is not None and car.event_plugins is not None
                             else settings.event_plugin_selection())
            loaded = load_event_plugins(bus, selection)
        print(f"event plugins for {car_id}: {loaded or 'none (pass-through)'}")
        return bus

    svc = ExportService(settings, bus_for)
    years = args.years or svc.years(args.car_id)
    if not years:
        print(f"No trips recorded for '{args.car_id}'.")
        return 1
    out_dir = args.out or Path(tempfile.mkdtemp(prefix="rittenregistratie-export-"))

    rc = 0
    for year in years:
        try:
            res = svc.generate(args.car_id, year, out_dir=out_dir)
        except ExportError as exc:
            print(f"{year}: FAILED: {exc}")
            rc = 2
            continue
        raw = len(svc.trips_for_year(args.car_id, year))
        print(f"{year}: {res.rows} rows (ledger had {raw}) -> {res.path}")
        if args.json:
            for row in validate_trips(svc.bus_for(args.car_id).emit(EXPORT_PRE_GENERATE, {
                "car_id": args.car_id, "year": year,
                "data_dir": str(settings.data_dir), "config_dir": str(settings.config_dir),
                "trips": svc.trips_for_year(args.car_id, year),
            })["trips"]):
                print(json.dumps(row, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
