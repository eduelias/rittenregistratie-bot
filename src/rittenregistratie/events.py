"""Event bus: the core emits events, plugins subscribe.

Handlers run synchronously, in registration order. Each handler receives the
payload returned by the previous one, so several plugins can chain. A handler
that returns ``None`` leaves the payload unchanged. With no handlers at all,
``emit`` returns the payload as given: the default behaviour is the identity.

Events emitted by the core
--------------------------
``export.pre_generate``
    Emitted when a spreadsheet is requested, before anything is written.
    Payload::

        {"car_id": str, "year": int, "data_dir": str, "config_dir": str,
         "trips": [ {ledger row as dict}, ... ]}

    The handler returns a payload of the same shape. Only ``trips`` is read
    back; it is validated (see :mod:`rittenregistratie.export`) before the
    workbook is generated. The raw ledger itself is never modified.

``export.post_generate``
    Emitted after the workbook has been written. Payload::

        {"car_id": str, "year": int, "path": str, "rows": int}

    The return value is ignored.

Registering a plugin
--------------------
Expose a callable under the entry-point group ``rittenregistratie.events``.
It is called once at startup with the bus::

    def register(bus):
        bus.on("export.pre_generate", my_handler)

``RIT_EVENT_PLUGINS`` selects which installed plugins are loaded: ``*`` (all,
the default), empty (none), or a comma-separated list of entry-point names.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, Iterable, List, Optional

log = logging.getLogger("rittenregistratie.events")

EXPORT_PRE_GENERATE = "export.pre_generate"
EXPORT_POST_GENERATE = "export.post_generate"

ENTRY_POINT_GROUP = "rittenregistratie.events"

Handler = Callable[[Any], Any]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = {}

    def on(self, event: str, handler: Handler) -> Handler:
        """Subscribe ``handler`` to ``event``. Returns the handler (decorator-friendly)."""
        self._handlers.setdefault(event, []).append(handler)
        return handler

    def handlers(self, event: str) -> List[Handler]:
        return list(self._handlers.get(event, []))

    def emit(self, event: str, payload: Any) -> Any:
        """Run every handler for ``event`` in order and return the final payload."""
        for handler in self._handlers.get(event, []):
            result = handler(payload)
            if result is not None:
                payload = result
        return payload


def load_event_plugins(
    bus: EventBus, selection: Optional[Iterable[str]] = None,
    group: str = ENTRY_POINT_GROUP,
) -> List[str]:
    """Load event plugins from entry points and let them subscribe to ``bus``.

    ``selection``: ``None`` loads every installed plugin; an iterable loads only
    those names (an empty iterable loads nothing). Returns the names loaded. A
    plugin that fails to import or register is logged and skipped so one broken
    plugin cannot take the bot down.
    """
    wanted = None if selection is None else {str(n).strip() for n in selection if str(n).strip()}
    loaded: List[str] = []
    for ep in entry_points(group=group):
        if wanted is not None and ep.name not in wanted:
            continue
        try:
            register = ep.load()
            register(bus)
            loaded.append(ep.name)
        except Exception:  # pragma: no cover - defensive
            log.exception("Event plugin %r failed to register; skipped.", ep.name)
    if wanted:
        missing = sorted(wanted - set(loaded))
        if missing:
            log.warning("Event plugins requested but not installed: %s", ", ".join(missing))
    return loaded
