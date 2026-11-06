from __future__ import annotations

from typing import TYPE_CHECKING

from labforge.iac.providers.bmc import ensure_power
from labforge.iac.providers.remote import ssh_argv, winrm_run
from labforge.iac.spec import Host

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext
    from labforge.iac.planner import Change


class HostProvider:
    def apply(self, change: Change, ctx: ApplyContext) -> dict:
        host: Host = change.model()  # type: ignore[assignment]
        outputs: dict = {"address": host.address, "os": host.platform.os, "arch": host.platform.arch}

        if host.bmc and host.power:
            outputs["power"] = ensure_power(ctx, host, host.power)

        bootstrap_changed = change.action == "create" or any(p.startswith("bootstrap") for p in change.diff)
        if host.bootstrap and bootstrap_changed:
            if host.power == "off":
                ctx.log("    host is powered off; skipping bootstrap")
            else:
                for cmd in host.bootstrap:
                    if host.connection.type == "winrm":
                        winrm_run(ctx, host, cmd)
                    else:
                        ctx.runner.run(ssh_argv(host, ctx.secrets, cmd))
                outputs["bootstrapped"] = True
        elif host.bootstrap:
            ctx.log("    bootstrap unchanged; skipping")
        return outputs

    def destroy(self, change: Change, ctx: ApplyContext) -> None:
        host: Host = change.model()  # type: ignore[assignment]
        if host.on_destroy == "poweroff" and host.bmc:
            ensure_power(ctx, host, "off")
        # Physical machines are never wiped; they just leave the inventory.
        ctx.log("    deregistered from inventory")
