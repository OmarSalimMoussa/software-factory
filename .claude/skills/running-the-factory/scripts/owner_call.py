"""Run one owner verb on the warden's owner socket and print its answer as JSON, as the dashboard sends it.

On the server, as its admin: `python3 -I owner_call.py VERB '{"arg": "value"}'`. It runs from the installed copy
of the repo and reads the factory's own code from there.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from runtime.cli.factory import Call, ask
from runtime.shared.paths import Places

verb, args = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
print(json.dumps(ask(Places.system().owner_socket, Call(verb, args)), indent=1))
