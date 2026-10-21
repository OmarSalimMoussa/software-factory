"""One line on the machine's CPU over the next minute: how busy and how much the host stole, beside the warden's own
average, the cores used outside the factory's slice, the slice's current cap, and the load.

On the server, with sudo, as the warden's CPU record is root's: `sudo python3 -I cpu_sample.py`.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.warden.health import cpu_budget


def ticks() -> list[int]:
    return [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]


before = ticks()
time.sleep(60)
spent = [b - a for a, b in zip(before, ticks(), strict=True)]
whole = sum(spent)
busy = (spent[0] + spent[1] + spent[2] + spent[5] + spent[6]) / whole
steal = spent[7] / whole
budget = store.read_json(Places.system().cpu)
quota, period = (cpu_budget.slice_dir(cpu_budget.CGROUPS) / "cpu.max").read_text().split()
cap = "none" if quota == "max" else f"{int(quota) / int(period):.2f} cores"
load = Path("/proc/loadavg").read_text().split()[0]
print(f"60s busy {busy:.0%} steal {steal:.0%} | avg {budget['use']:.0%} outside {budget['outside']:.2f} cores | "
      f"factory cap {cap} | load {load}")  # fmt: skip
