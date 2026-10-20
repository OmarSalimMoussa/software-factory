# Health

How the warden knows the factory and the product are well: the laptop's vitals, the probes of prod and of slot 0,
which is staging, the pings to Healthchecks.io, prod's and slot 0's errors imported as requests, and the read-only
telemetry and prod's records agents ask for.

Beside the vitals, and never part of their level, it lists the merges waiting for their check: amber once a
bundle's worth waits or the oldest has waited too long. The vitals hold back new work; this never does, since the
work that clears those merges has to start.
