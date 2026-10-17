from __future__ import annotations

import pytest
from warden_fakes import FakeLauncher

from runtime.shared import settings
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context, StackResult
from runtime.warden.owner import slot_sign_in

ME = "owner@example.com"


STAGING = "http://203.0.113.7:8090"
LOOPBACK = "http://127.0.0.1:8100"


@pytest.mark.parametrize(
    ("address", "zero_origin", "printed", "opened"),
    [
        (ME, "", f"Sign in at {LOOPBACK}/auth/once?t=x", f"{LOOPBACK}/auth/once?t=x"),
        (ME, STAGING, f"Sign in at {STAGING}/auth/once?t=x", f"{STAGING}/auth/once?t=x"),
        (ME, "", "http://evil.example/auth/once?t=x", None),
        (ME, "", "http://127.0.0.1:8101/auth/once?t=x", None),
        (ME, "", "http://127.0.0.1:81000/auth/once?t=x", None),
        (ME, "", f"{STAGING}/auth/once?t=x", None),
        (ME, STAGING, f"{LOOPBACK}/auth/once?t=x", None),
        (ME, STAGING, "http://203.0.113.7:8091/auth/once?t=x", None),
        (ME, "", "", None),
        ("", "", f"{LOOPBACK}/auth/once?t=x", None),
    ],
    ids=["loopback", "staging", "another-host", "another-slot", "a-longer-port", "staging-unset",
         "loopback-once-public", "staging-another-port", "no-link", "no-address"],
)  # fmt: skip
def test_open_hands_back_only_a_link_on_slot_0s_own_address(
    ctx: Context,
    places: Places,
    launcher: FakeLauncher,
    address: str,
    zero_origin: str,
    printed: str,
    opened: str | None,
) -> None:
    places.overrides.parent.mkdir(parents=True, exist_ok=True)
    slots = {"owner_address": address, "zero_origin": zero_origin}
    places.overrides.write_text(settings.overrides_toml({"slots": slots}))
    launcher.stack["sign-in"] = StackResult(0 if printed else 1, printed + "\n", "l")

    if opened is None:
        with pytest.raises(Refused):
            slot_sign_in.open_slot(ctx, {}, None)
    else:
        assert slot_sign_in.open_slot(ctx, {}, None)["open"] == opened
        assert ("stack", (0, ["sign-in", ME], slot_sign_in.SESSION)) in launcher.calls
