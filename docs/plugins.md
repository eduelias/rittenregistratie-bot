# Writing plugins

Plugins are ordinary Python packages that register classes under one of the
entry-point groups. Install the package alongside the core and select it via the
`RIT_*` environment variables.

## Groups and interfaces

All interfaces live in `rittenregistratie.plugins.base`:

- `OdometerSource.get_reading() -> OdometerReading | None`
- `TrajectoryProvider.get_trajectory(origin, destination) -> TrajectoryResult`
- `DeltaAllocator.allocate(delta_pool_km, locations, routes, year, now) -> list[VirtualTrip]`
- `PrivateCapPlugin.on_evaluate(year, private_total_km, cap_km, trips) -> CapAction`

## Registering

In your plugin package's `pyproject.toml`:

```toml
[project.entry-points."rittenregistratie.delta"]
my_allocator = "my_pkg.allocator:MyAllocator"

[project.entry-points."rittenregistratie.privatecap"]
my_cap = "my_pkg.cap:MyCapPlugin"
```

Then set:

```
RIT_DELTA_ALLOCATOR=my_allocator
RIT_PRIVATE_CAP_PLUGIN=my_cap
```

## Ethical / legal note

The core deliberately ships inert defaults (`noop`, `warn`). Any plugin that
*generates* or *reclassifies* trips affects tax-relevant records. Keep such
logic in a separate, private package and make sure the output still reflects
reality. This project takes no responsibility for the behaviour of third-party
plugins.
