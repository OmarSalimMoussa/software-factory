from __future__ import annotations

from warden_fakes import FakeClock

from runtime.warden import codes, ops
from runtime.warden.context import Context


def test_an_operation_a_stopped_warden_left_running_reads_failed_at_its_step(ctx: Context) -> None:
    op_id = ops.start(ctx, "settings", ["Save the new values", "Commit settings.toml to the factory repo"])
    ops.step(ctx, op_id, 0, ops.DONE, "2 values in effect")
    ops.step(ctx, op_id, 1, ops.RUNNING)

    ops.recover(ctx.places, ctx.clock)

    op = ops.get(ctx, {"id": op_id}, None)["op"]
    assert op["state"] == ops.FAILED
    assert [s["state"] for s in op["steps"]] == [ops.DONE, ops.FAILED]
    assert op["steps"][1]["detail"].startswith("the warden restarted during this step")


def test_an_admitted_device_expires_after_its_days(ctx: Context) -> None:
    token = codes.admit_device(ctx, {}, None)["token"]

    assert codes.verify_device(ctx, {"token": token}, None)["device"] == "ok"
    assert codes.verify_device(ctx, {"token": token + "x"}, None) == {"device": "expired"}
    assert isinstance(ctx.clock, FakeClock)
    ctx.clock.t += ctx.settings().get_int("dashboard.device_days") * 86400
    assert codes.verify_device(ctx, {"token": token}, None) == {"device": "expired"}
