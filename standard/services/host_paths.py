"""Where a host keeps its shared services, and the Docker they run on beside every workspace and slot.

The server set-up writes the folder, and the factory's install checks it is there. Light on purpose: the
factory's own paths import it, and its guard imports those at every tool call. Docker's local driver keeps each
volume's files in DOCKER_VOLUMES/<volume>/_data.
"""

from __future__ import annotations

from pathlib import Path

FOLDER = Path("/srv/factory/services")
DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_VOLUMES = Path("/var/lib/docker/volumes")
