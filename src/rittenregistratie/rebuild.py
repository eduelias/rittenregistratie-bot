"""Rebuild the compliant spreadsheet(s) from the immutable raw ledger.

The raw ledger (data/raw-ledger-<car_id>.jsonl) holds every trip exactly as the
user reported it. This tool regenerates the per-year Excel files purely from that
pristine data — useful to restore an unmodified spreadsheet or to verify what the
records would look like without any post-processing.

Usage:
    python -m rittenregistratie.rebuild <car_id> [--data-dir DIR] [--out DIR]

By default it reads/writes under the configured data dir. Use --out to write the
rebuilt files to a separate directory (so existing files are not overwritten).
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .excel_writer import ExcelWriter
from .models import Trip, TripSource, TripType
from .raw_ledger import RawLedger


def rebuild(car_id: str, data_dir: Path, out_dir: Path) -> int:
    ledger = RawLedger(data_dir / f"raw-ledger-{car_id}.jsonl")
    trips = ledger.read_all()
    writer = ExcelWriter(out_dir, car_id)
    for rt in trips:
        writer.append_trip(Trip(
            date=datetime.fromisoformat(rt.timestamp),
            trip_type=TripType.PRIVATE if rt.is_private else TripType.BUSINESS,
            start_address=rt.start_address,
            end_address=rt.end_address,
            start_odo=rt.start_odo,
            end_odo=rt.end_odo,
            route="",
            private_detour_km=0,
            source=TripSource.WHATSAPP,
        ))
    return len(trips)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild spreadsheet from raw ledger")
    ap.add_argument("car_id")
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--out", default=None, type=Path,
                    help="output dir (default: same as data-dir)")
    args = ap.parse_args(argv)
    out = args.out or args.data_dir
    out.mkdir(parents=True, exist_ok=True)
    n = rebuild(args.car_id, args.data_dir, out)
    print(f"Rebuilt {n} trips for '{args.car_id}' into {out}/trips-{args.car_id}-*.xlsx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
