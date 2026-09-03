"""Reference event plugin: the complete core <-> plugin communication pattern.

Install next to the core (``pip install -e examples/rittenregistratie-events-example``)
and it is picked up automatically. It does two harmless things so you can see
the round-trip working:

- on ``export.pre_generate`` it tidies whitespace in the addresses and returns
  the payload; the row count and every value the user typed stay the same;
- on ``export.post_generate`` it logs where the workbook was written.

Copy this package, keep ``register`` and the two signatures, replace the bodies.
Only ``payload["trips"]`` is read back by the core; return it as a list of
dicts in the ledger shape (see ``rittenregistratie.events.TripRow``). The core
validates the result and refuses the export if it is malformed.
"""
from __future__ import annotations

import logging

from rittenregistratie.events import (
    EXPORT_POST_GENERATE,
    EXPORT_PRE_GENERATE,
    EventBus,
    PostGeneratePayload,
    PreGeneratePayload,
    with_trips,
)

log = logging.getLogger("rittenregistratie.events.example")


def register(bus: EventBus) -> None:
    """Entry point: called once at startup. Subscribe here."""
    bus.on(EXPORT_PRE_GENERATE, on_pre_generate)
    bus.on(EXPORT_POST_GENERATE, on_post_generate)


def on_pre_generate(payload: PreGeneratePayload) -> PreGeneratePayload:
    """Receive the year's trips as plain dicts; return the same shape."""
    trips = []
    for row in payload["trips"]:
        row = dict(row)  # never mutate the input in place
        row["start_address"] = " ".join(str(row["start_address"]).split())
        row["end_address"] = " ".join(str(row["end_address"]).split())
        trips.append(row)
    log.info(
        "example plugin: %s/%s, %d trips passed through",
        payload["car_id"], payload["year"], len(trips),
    )
    return with_trips(payload, trips)


def on_post_generate(payload: PostGeneratePayload) -> None:
    """Observe the finished workbook. Return value is ignored."""
    log.info(
        "example plugin: workbook for %s/%s written to %s (%d rows)",
        payload["car_id"], payload["year"], payload["path"], payload["rows"],
    )
