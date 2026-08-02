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

## Important note

The core is a faithful recorder: it stores each trip exactly as reported (the
distance is always the real odometer difference) and never fabricates, invents,
or reclassifies trips. The shipped `PrivateCapPlugin` default (`warn`) only
reports a message.

You are responsible for the behaviour of any plugin you install. A plugin that
alters tax-relevant records must reflect reality; this project takes no
responsibility for third-party plugins.
