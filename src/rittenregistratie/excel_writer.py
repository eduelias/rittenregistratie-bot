"""Per-year Excel writer — the Belastingdienst per-trip fields.

Per the official rittenregistratie requirements, each trip row contains these
fields:

- Datum — date
- Beginstand — begin odometer
- Eindstand — end odometer
- Vertrekadres — departure address
- Aankomstadres — arrival address
- Route — the driven route, only when it is not the most usual route
- Privé/zakelijk — private or business
- Privé-omrijkilometers — private detour km when a trip mixes business and
  private kilometres

followed by the two the employer manual (Handboek Loonheffingen 2026, §23.3.18)
adds for employees and DGAs with a company car:

- Ritnummer — the trip's number on its day (1, 2, 3 ...)
- Bestuurder — who drove, only filled when several people drive the car

No other per-trip columns are added.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import List

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .models import Trip, TripType

# Extra columns for employees and DGAs: the employer manual (Handboek Loonheffingen,
# 23.3.18) also asks for a trip number per day and, with more drivers, who drove.
# Explanation per column, shown on the template's "Uitleg" sheet.
TEMPLATE_EXTRA = {
    "Ritnummer": "Voor werknemers en dga's: het volgnummer van de rit op die dag (1, 2, 3 ...).",
    "Bestuurder": "Voor werknemers en dga's, als meer mensen in de auto rijden: wie de rit reed.",
}

HEADERS: List[str] = [
    "Datum",
    "Beginstand",
    "Eindstand",
    "Vertrekadres",
    "Aankomstadres",
    "Route",
    "Privé/zakelijk",
    "Privé-omrijkilometers",
    *TEMPLATE_EXTRA,
]


def _pb(t: TripType) -> str:
    return "privé" if t is TripType.PRIVATE else "zakelijk"


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

    @staticmethod
    def _row(trip: Trip, number: int) -> list:
        """``number`` is the trip's Ritnummer: its place among that day's trips."""
        return [
            trip.date.strftime("%Y-%m-%d"),
            trip.start_odo,
            trip.end_odo,
            trip.start_address,
            trip.end_address,
            trip.route,  # only populated when the usual route was not taken
            _pb(trip.trip_type),
            trip.private_detour_km or "",
            number,
            trip.driver,
        ]

    def write_all(self, year: int, trips: List[Trip]) -> Path:
        """Write a fresh workbook for ``year`` holding exactly ``trips``, in
        the given order (which numbers each day's trips).

        Replaces any existing file for that year. Used by on-demand export.
        """
        path = self._path_for_year(year)
        wb = Workbook()
        ws = wb.active
        ws.title = "Ritten"
        ws.append(HEADERS)
        per_day: Counter = Counter()
        for trip in trips:
            per_day[trip.date.date()] += 1
            ws.append(self._row(trip, per_day[trip.date.date()]))
        wb.save(path)
        return path

    def append_trip(self, trip: Trip) -> Path:
        year = trip.date.year
        wb, ws, path = self._open(year)
        day = trip.date.strftime("%Y-%m-%d")
        number = 1 + sum(1 for (d,) in ws.iter_rows(min_row=2, max_col=1, values_only=True) if d == day)
        ws.append(self._row(trip, number))
        wb.save(path)
        return path


# Explanation per column on the template's "Uitleg" sheet (rrbot.nl/rittenregistratie-excel).
COLUMN_HELP = {
    "Datum": "De dag van de rit.",
    "Beginstand": "Kilometerstand bij vertrek. Gelijk aan de eindstand van de vorige rit, "
                  "anders zit er een gat in je registratie.",
    "Eindstand": "Kilometerstand bij aankomst.",
    "Vertrekadres": "Waar de rit begint: straat, huisnummer en plaats.",
    "Aankomstadres": "Waar de rit eindigt. Heen en terug zijn twee ritten.",
    "Route": "Alleen invullen als je niet de gebruikelijke route reed, bijvoorbeeld een "
             "omleiding of een stop onderweg.",
    "Privé/zakelijk": "Kies zakelijk of privé.",
    "Privé-omrijkilometers": "Alleen bij een zakelijke rit waarin je ook privé omreed: "
                             "hoeveel kilometer die omweg was.",
}

TEMPLATE_ROWS = 1000


def template_workbook(site_url: str = "https://rrbot.nl/") -> Workbook:
    """An empty rittenregistratie for people who keep it by hand: the same
    columns as the bot's export, an overview sheet that adds up the
    private kilometres and finds gaps in the odometer chain. Served by
    site.py as a free download."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    bold, head = Font(bold=True), PatternFill("solid", fgColor="DCF8C6")
    wb = Workbook()
    ws = wb.active
    ws.title = "Ritten"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font, cell.fill = bold, head
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJ", (12, 12, 12, 34, 34, 28, 14, 22, 11, 18)):
        ws.column_dimensions[col].width = width
    last = TEMPLATE_ROWS + 1
    for row in range(2, last + 1):
        ws.cell(row, 1).number_format = "dd-mm-yyyy"
        for col in (2, 3, 8, 9):
            ws.cell(row, col).number_format = "0"
    kind = DataValidation(type="list", formula1='"zakelijk,privé"', allow_blank=True)
    kind.add(f"G2:G{last}")
    odo = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
    odo.add(f"B2:C{last}")
    odo.add(f"H2:I{last}")
    ws.add_data_validation(kind)
    ws.add_data_validation(odo)

    ov = wb.create_sheet("Overzicht")
    ov.column_dimensions["A"].width = 38
    ov.column_dimensions["B"].width = 26
    b, c, g, h = (f"Ritten!{x}2:{x}{last}" for x in "BCGH")
    km = f"({c}-{b})*({c}<>\"\")"
    rows = [
        ("Kenteken", None),
        ("Merk en type", None),
        ("Jaar", None),
        (None, None),
        ("Kilometers totaal", f"=SUMPRODUCT({km})"),
        ("Kilometers privé (ritten + omrijden)", f"=SUMPRODUCT(({g}=\"privé\")*{km})+SUM({h})"),
        ("Kilometers zakelijk", "=B5-B6"),
        ("Grens privé per jaar", 500),
        ("Nog over tot de grens", "=B8-B6"),
        ("Gaten in de kilometerstand",
         f"=SUMPRODUCT((Ritten!B3:B{last}<>\"\")*(Ritten!B3:B{last}<>Ritten!C2:C{last - 1}))"),
    ]
    for label, value in rows:
        ov.append([label, value])
    for row in (1, 2, 3, 5, 6, 7, 8, 9, 10):
        ov.cell(row, 1).font = bold
    ov.append([])
    ov.append(["Gaten: het aantal ritten waarvan de beginstand niet gelijk is aan de eindstand "
               "van de rit erboven. Bij 0 sluit je kilometerstand."])
    ov.append(["De 500 km geldt per kalenderjaar. Gebruik je de auto een deel van het jaar? "
               "Ondernemers rekenen hun privé-km om naar een heel jaar, werknemers rekenen met "
               "een evenredig deel van 500 km. Zie de uitleg op de site."])

    ex = wb.create_sheet("Uitleg")
    ex.column_dimensions["A"].width = 24
    ex.column_dimensions["B"].width = 90
    ex.append(["Kolom", "Wat vul je in"])
    for cell in ex[1]:
        cell.font, cell.fill = bold, head
    for name, text in {**COLUMN_HELP, **TEMPLATE_EXTRA}.items():
        ex.append([name, text])
    ex.append([])
    ex.append(["Let op", "Dit sjabloon is geen belastingadvies. Controleer de actuele eisen op "
                         "belastingdienst.nl."])
    ex.append(["Meer uitleg", f"{site_url}rittenregistratie-eisen"])
    ex.append(["Zonder typen", f"{site_url} houdt je rittenregistratie bij via WhatsApp."])
    for row in ex.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    return wb
