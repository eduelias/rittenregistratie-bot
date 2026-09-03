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

Enable with `RIT_EVENT_PLUGINS=*` (default: all installed) or a comma-separated
list of entry-point names; empty disables all event plugins.

## Important note

The core is a faithful recorder: it stores each trip exactly as reported (the
distance is always the real odometer difference) and never fabricates, invents,
or reclassifies trips. The shipped `PrivateCapPlugin` default (`warn`) only
reports a message.

You are responsible for the behaviour of any plugin you install. A plugin that
alters tax-relevant records must reflect reality; this project takes no
responsibility for third-party plugins.
