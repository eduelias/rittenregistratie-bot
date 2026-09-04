# Writing plugins

Plugins are ordinary Python packages that register classes under one of the
entry-point groups. Install the package alongside the core and select it via the
`RIT_*` environment variables.

## Groups and interfaces

All interfaces live in `rittenregistratie.plugins.base`:

- `OdometerSource.get_reading() -> OdometerReading | None` — where odometer
  readings come from.
- `TrajectoryProvider.get_trajectory(origin, destination) -> TrajectoryResult` —
  how the route between two addresses is resolved.
- `PrivateCapPlugin.on_evaluate(year, private_total_km, cap_km, trips) -> CapAction`
  — how to report on private km against the yearly cap.

## Registering

In your plugin package's `pyproject.toml`:

```toml
[project.entry-points."rittenregistratie.trajectory"]
my_trajectory = "my_pkg.trajectory:MyProvider"

[project.entry-points."rittenregistratie.privatecap"]
my_cap = "my_pkg.cap:MyCapPlugin"
```

Then set:

```
RIT_TRAJECTORY_PROVIDER=my_trajectory
RIT_PRIVATE_CAP_PLUGIN=my_cap
```

## Events

The core owns an `EventBus` (`rittenregistratie.events`). Plugins subscribe at
startup; the core emits. Handlers run synchronously in registration order and
each receives the payload returned by the previous one. Returning `None` leaves
the payload unchanged. With no handlers the payload passes through untouched.

Register a callable under the `rittenregistratie.events` group:

```toml
[project.entry-points."rittenregistratie.events"]
my_events = "my_pkg.events:register"
```

```python
def register(bus):
    bus.on("export.pre_generate", on_pre_generate)
    bus.on("export.post_generate", on_post_generate)

def on_pre_generate(payload):
    # payload: {"car_id", "year", "data_dir", "config_dir",
    #           "trips": [ {ledger row as dict}, ... ]}
    trips = [dict(t) for t in payload["trips"]]
    ...  # transform the list of plain dicts
    return {**payload, "trips": trips}

def on_post_generate(payload):
    # payload: {"car_id", "year", "path", "rows"}; return value ignored
    ...
```

Each trip dict has the ledger fields: `timestamp` (ISO-8601), `start_odo`,
`end_odo` (int), `start_address`, `end_address`, `is_private` (bool), and
optionally `route`, `private_detour_km`, `note`, `destination_raw`,
`raw_message`. The core validates the returned list (types, `end >= start`,
sorted by time, non-decreasing odometer chain) and aborts the export with an
error to the user if it fails. The raw ledger is never modified by an export.

Event plugins are selected **per car**. Each car has its own `EventBus`;
`register(bus)` is called once per car that selects the plugin, and every
payload carries `car_id`. Select in `cars.yaml`:

```yaml
mercedes:
  event_plugins: ["reallocate"]   # only this car
van:
  event_plugins: []               # none for this car
other:                            # key absent -> global RIT_EVENT_PLUGINS
```

The global `RIT_EVENT_PLUGINS` (`*` all installed, empty none, or a
comma-separated list) is the default for cars without the key. The same
applies to `cap_plugin` in `cars.yaml` versus `RIT_PRIVATE_CAP_PLUGIN`.

### Vehicle-telemetry hooks (`hook.<plugin>`)

A plugin that brings trips in from a car subscribes to `hook.<plugin>`:

```python
from rittenregistratie.events import hook_event
from rittenregistratie.models import VehicleTripReport

def register(bus):
    bus.on(hook_event("homeassistant"), on_hook)

def on_hook(payload):
    # payload: {"car_id", "plugin", "body": <JSON the source posted>, "headers": {x-*}}
    b = payload["body"]
    if b.get("ignition") != "off":
        return None                      # not a finished trip: ignored
    return VehicleTripReport(
        end_odo=int(b["odometer"]), start_odo=b.get("start_odometer"),
        latitude=b.get("lat"), longitude=b.get("lon"), zone=b.get("zone", ""),
    )
```

The source POSTs to `/hooks/homeassistant/<car_id>` with header
`X-Hook-Secret: <RIT_HOOK_SECRET>`. Only cars listing the plugin in their
`event_plugins` receive the event (404 otherwise). Returning `None` means
"nothing to log"; returning a report makes the core write a ledger row,
resolve the end place from known locations' coordinates, and notify the car's
numbers. The core never guesses: an unknown place is stored as its address and
the user is asked to name it.

### Reference plugin and dry runs

`examples/rittenregistratie-events-example/` is a complete, installable event
plugin: a `pyproject.toml` with the entry point, `register(bus)`, and one
handler per event with the typed payloads from `rittenregistratie.events`
(`PreGeneratePayload`, `PostGeneratePayload`, `TripRow`, `with_trips`). Copy
it, keep the signatures, replace the bodies.

```bash
pip install -e examples/rittenregistratie-events-example   # picked up automatically
```

Develop against the real ledger without sending anything:

```bash
python -m rittenregistratie.export mercedes 2026            # all installed plugins
python -m rittenregistratie.export mercedes --plugins example
python -m rittenregistratie.export mercedes --no-plugins   # pass-through baseline
python -m rittenregistratie.export mercedes 2026 --json    # print the validated rows
```

Each run prints the plugins loaded, the row count before and after, and the
path of the generated workbook (a temp dir unless `--out` is given). A plugin
that returns malformed rows fails here with the same message the user would
see on WhatsApp.

## Important note

The core is a faithful recorder: it stores each trip exactly as reported (the
distance is always the real odometer difference) and never fabricates, invents,
or reclassifies trips. The shipped `PrivateCapPlugin` default (`warn`) only
reports a message.

You are responsible for the behaviour of any plugin you install. A plugin that
alters tax-relevant records must reflect reality; this project takes no
responsibility for third-party plugins.
