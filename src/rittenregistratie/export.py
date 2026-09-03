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
from typing import Any, Dict, List, Optional

from .config import Settings
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


class ExportService:
    def __init__(self, settings: Settings, bus: EventBus):
        self.settings = settings
        self.bus = bus

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
        handlers = len(self.bus.handlers(EXPORT_PRE_GENERATE))
        result = self.bus.emit(EXPORT_PRE_GENERATE, payload)
        if not isinstance(result, dict) or "trips" not in result:
            raise ExportError("export.pre_generate handler returned an invalid payload")
        rows = validate_trips(result["trips"])
        if not rows:
            raise ExportError("export.pre_generate handler returned no trips")

        out_dir = out_dir or self.settings.exports_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        path = ExcelWriter(out_dir, car_id).write_all(year, [raw_to_trip(r) for r in rows])

        self.bus.emit(EXPORT_POST_GENERATE, {
            "car_id": car_id, "year": year, "path": str(path), "rows": len(rows),
        })
        return ExportResult(car_id=car_id, year=year, path=path, rows=len(rows), handlers=handlers)
