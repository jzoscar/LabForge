"""Out-of-band power control for physical machines (Redfish or IPMI)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Bmc, Host

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext


def redfish_base(bmc: Bmc) -> str:
    return bmc.address if bmc.address.startswith(("http://", "https://")) else f"https://{bmc.address}"


class RedfishClient:
    def __init__(self, bmc: Bmc, secrets: SecretResolver, timeout: float = 15):
        self.bmc = bmc
        self.client = httpx.Client(
            base_url=redfish_base(bmc),
            auth=secrets.credentials(bmc.credentials_ref),
            verify=bmc.verify_tls,
            timeout=timeout,
        )

    def __enter__(self) -> RedfishClient:
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def _get(self, path: str) -> dict:
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()

    def system_path(self) -> str:
        if self.bmc.system_id:
            return f"/redfish/v1/Systems/{self.bmc.system_id}"
        return self._get("/redfish/v1/Systems")["Members"][0]["@odata.id"]

    def system(self) -> dict:
        return self._get(self.system_path())

    def reset(self, reset_type: str) -> None:
        r = self.client.post(f"{self.system_path()}/Actions/ComputerSystem.Reset", json={"ResetType": reset_type})
        r.raise_for_status()


def ipmi_argv(bmc: Bmc, user: str) -> list[str]:
    # -E reads the password from IPMI_PASSWORD so it never appears in argv or logs.
    return ["ipmitool", "-I", "lanplus", "-H", bmc.address, "-U", user, "-E"]


def ensure_power(ctx: ApplyContext, host: Host, desired: str) -> str:
    bmc = host.bmc
    assert bmc is not None
    if bmc.type == "ipmi":
        user, password = ctx.secrets.credentials(bmc.credentials_ref)
        base, env = ipmi_argv(bmc, user), {"IPMI_PASSWORD": password}
        current = ctx.runner.run(base + ["chassis", "power", "status"], env=env).stdout.strip().lower()
        if ctx.dry_run or not current.endswith(desired):
            ctx.runner.run(base + ["chassis", "power", "on" if desired == "on" else "soft"], env=env)
        return desired

    ctx.log(("[dry-run] " if ctx.dry_run else "") + f"redfish {bmc.address}: ensure power {desired}")
    if ctx.dry_run:
        return desired
    with RedfishClient(bmc, ctx.secrets) as rf:
        state = rf.system().get("PowerState", "Unknown")
        ctx.log(f"    PowerState={state}")
        if state.lower() != desired:
            rf.reset("On" if desired == "on" else "GracefulShutdown")
            ctx.log(f"    reset requested -> {desired}")
    return desired
