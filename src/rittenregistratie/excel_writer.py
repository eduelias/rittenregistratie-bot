"""Per-year Excel writer with a Belastingdienst-compliant schema.

Columns capture the mandatory 'sluitende rittenregistratie' fields:
date, begin/end odometer, begin/end address, the driven route (plus a note when
it deviates from the usual route) and the business/private character.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .models import Trip

HEADERS: List[str] = [
    "Date",
    "Time",
    "Type",
    "StartAddress",
    "EndAddress",
    "StartOdo",
    "EndOdo",
    "TripKm",
    "Route",
    "DeviationNote",
    "Source",
    "PrivateKmYTD",
]


class ExcelWriter:
    def __init__(self, data_dir: Path, car_id: str):
        self.data_dir = data_dir
        self.car_id = car_id
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_year(self, year: int) -> Path:
        return self.data_dir / f"trips-{self.car_id}-{year}.xlsx"

    def _open(self, year: int) -> tuple[Workbook, Worksheet, Path]:
        path = self._path_for_year(year)
        if path.exists():
            wb = load_workbook(path)
            ws = wb.active
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Ritten"
            ws.append(HEADERS)
        return wb, ws, path

    def append_trip(self, trip: Trip) -> Path:
        year = trip.date.year
        wb, ws, path = self._open(year)
        ws.append(
            [
                trip.date.strftime("%Y-%m-%d"),
                trip.date.strftime("%H:%M"),
                trip.trip_type.value,
                trip.start_address,
                trip.end_address,
                trip.start_odo,
                trip.end_odo,
                trip.trip_km,
                trip.route,
                trip.deviation_note,
                trip.source.value,
                trip.private_km_ytd,
            ]
        )
        wb.save(path)
        return path
