# Warden

The one system service that starts sessions and keeps the factory's state. Each tick it schedules work, watches
sessions, probes staging and prod, imports errors and user reports, and keeps cost, vitals and permits. Every request
from a session or the owner passes through it, and everything the dashboard shows comes from it.
