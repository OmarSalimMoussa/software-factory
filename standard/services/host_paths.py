"""Where a host keeps its shared services, and the Docker they run on beside every workspace and slot.

The server set-up writes the folder; deploys and the factory's slots read it. Light on purpose: the
factory's own paths import it, and its guard imports those at every tool call.
"""

from __future__ import annotations

from pathlib import Path

FOLDER = Path("/srv/factory/services")
DOCKER_SOCKET = "/var/run/docker.sock"
