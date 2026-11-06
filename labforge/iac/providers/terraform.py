"""Delegate a resource to an existing Terraform module (cloud VPCs, managed LBs, switches...)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from labforge.iac.providers.base import workspace
from labforge.iac.spec import TerraformRef

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext


def run_terraform(
    ctx: ApplyContext,
    tf: TerraformRef,
    address: str,
    action: Literal["apply", "destroy"],
    inputs: dict,
) -> dict:
    module = (ctx.base_dir / tf.module).resolve()
    ws = workspace(address)
    var_file = (ctx.workdir / "tfvars" / f"{ws}.tfvars.json").resolve()
    if not ctx.dry_run:
        var_file.parent.mkdir(parents=True, exist_ok=True)
        var_file.write_text(json.dumps({**inputs, **tf.vars}, indent=2))

    tf_cmd = ["terraform", f"-chdir={module}"]
    ctx.runner.run(tf_cmd + ["init", "-input=false"])
    ctx.runner.run(tf_cmd + ["workspace", "select", "-or-create", ws])
    ctx.runner.run(tf_cmd + [action, "-auto-approve", "-input=false", f"-var-file={var_file}"])
    if action != "apply" or ctx.dry_run:
        return {}
    out = ctx.runner.run(tf_cmd + ["output", "-json"]).stdout
    return {k: v.get("value") for k, v in json.loads(out or "{}").items() if not v.get("sensitive")}
