from __future__ import annotations

from typing import TYPE_CHECKING

from labforge.iac.providers.terraform import run_terraform
from labforge.iac.spec import Network

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext
    from labforge.iac.planner import Change


def _tf_inputs(net: Network) -> dict:
    return {"name": net.name, "cidr": net.cidr, "subnets": [s.model_dump(exclude_none=True) for s in net.subnets]}


class NetworkProvider:
    def apply(self, change: Change, ctx: ApplyContext) -> dict:
        net: Network = change.model()  # type: ignore[assignment]
        outputs: dict = {"type": net.type, "cidr": net.cidr, "subnets": {s.name: s.cidr for s in net.subnets}}
        if net.driver == "terraform":
            assert net.terraform
            outputs["terraform"] = run_terraform(ctx, net.terraform, change.address, "apply", _tf_inputs(net))
        else:
            ctx.log(f"    recorded {net.type} {net.cidr} with {len(net.subnets)} subnet(s)")
        return outputs

    def destroy(self, change: Change, ctx: ApplyContext) -> None:
        net: Network = change.model()  # type: ignore[assignment]
        if net.driver == "terraform":
            assert net.terraform
            run_terraform(ctx, net.terraform, change.address, "destroy", _tf_inputs(net))
        else:
            ctx.log("    deregistered")
