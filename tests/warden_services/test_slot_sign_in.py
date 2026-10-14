from __future__ import annotations

import pytest
from warden_fakes import FakeLauncher

from runtime.shared import settings
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Context, StackResult
from runtime.warden.owner import slot_sign_in

ME = "owner@example.com"


@pytest.mark.parametrize(
    ("address", "printed", "opened"),
    [
        (
            ME,
            StackResult(0, "Sign in at http://127.0.0.1:8100/auth/once?t=x\n", "l"),
            "http://127.0.0.1:8100/auth/once?t=x",
        ),
        (ME, StackResult(0, "http://evil.example/auth/once?t=x\n", "l"), None),
        (ME, StackResult(0, "http://127.0.0.1:8101/auth/once?t=x\n", "l"), None),
        (ME, StackResult(1, "", "l"), None),
        ("", StackResult(0, "http://127.0.0.1:8100/auth/once?t=x\n", "l"), None),
    ],
    ids=["slot-0", "another-host", "another-slot", "no-link", "no-address"],
)
def test_open_hands_back_only_a_link_into_slot_0_on_this_laptop(
    ctx: Context, places: Places, launcher: FakeLauncher, address: str, printed: StackResult, opened: str | None
) -> None:
    places.overrides.parent.mkdir(parents=True, exist_ok=True)
    places.overrides.write_text(settings.overrides_toml({"slots": {"owner_address": address}}))
    launcher.stack["sign-in"] = printed

    if opened is None:
        with pytest.raises(Refused):
            slot_sign_in.open_slot(ctx, {}, None)
    else:
        assert slot_sign_in.open_slot(ctx, {}, None)["open"] == opened
        assert ("stack", (0, ["sign-in", ME], slot_sign_in.SESSION)) in launcher.calls
