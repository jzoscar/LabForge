from __future__ import annotations

from typing import TYPE_CHECKING

from labforge.iac.providers.remote import ssh_argv
from labforge.iac.providers.terraform import run_terraform
from labforge.iac.spec import LoadBalancer

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext
    from labforge.iac.planner import Change

HAPROXY_TMP = "/tmp/labforge-haproxy.cfg"
HAPROXY_INSTALL = (
    f"cat > {HAPROXY_TMP} && sudo haproxy -c -q -f {HAPROXY_TMP} "
    f"&& sudo install -m 644 {HAPROXY_TMP} /etc/haproxy/haproxy.cfg "
    "&& sudo systemctl reload-or-restart haproxy"
)


def render_haproxy(lb: LoadBalancer, targets: list[tuple[str, str]]) -> str:
    lines = [
        "# Managed by LabForge - changes will be overwritten",
        "global",
        "    log /dev/log local0",
        "    maxconn 4096",
        "",
        "defaults",
        "    log global",
        "    timeout connect 5s",
        "    timeout client 60s",
        "    timeout server 60s",
        "",
    ]
    hc = lb.health_check
    for ls in lb.listeners:
        mode = "http" if ls.protocol == "http" else "tcp"  # https is TLS passthrough
        name = f"{lb.name}-{ls.port}"
        lines += [
            f"frontend {name}",
            f"    bind {lb.vip or '*'}:{ls.port}",
            f"    mode {mode}",
            f"    default_backend {name}",
            "",
            f"backend {name}",
            f"    mode {mode}",
            "    balance roundrobin",
        ]
        if hc and hc.protocol == "http":
            lines.append(f"    option httpchk GET {hc.path}")
        check = f" check port {hc.port}" if hc else " check"
        port = ls.target_port or ls.port
        lines += [f"    server {t} {addr}:{port}{check}" for t, addr in targets]
        lines.append("")
    return "\n".join(lines)


def _tf_inputs(lb: LoadBalancer, ctx: ApplyContext, env: str) -> dict:
    return {
        "name": lb.name,
        "vip": lb.vip,
        "listeners": [ls.model_dump(exclude_none=True) for ls in lb.listeners],
        "targets": {t: ctx.host(env, t).address for t in lb.targets},
    }


class LoadBalancerProvider:
    def apply(self, change: Change, ctx: ApplyContext) -> dict:
        lb: LoadBalancer = change.model()  # type: ignore[assignment]
        outputs: dict = {"vip": lb.vip, "listeners": [ls.port for ls in lb.listeners], "targets": lb.targets}
        if lb.driver == "haproxy":
            assert lb.host
            targets = [(t, str(ctx.host(change.env, t).address)) for t in lb.targets]
            config = render_haproxy(lb, targets)
            ctx.runner.run(ssh_argv(ctx.host(change.env, lb.host), ctx.secrets, HAPROXY_INSTALL), input=config)
            outputs["haproxyHost"] = lb.host
        elif lb.driver == "terraform":
            assert lb.terraform
            outputs["terraform"] = run_terraform(ctx, lb.terraform, change.address, "apply", _tf_inputs(lb, ctx, change.env))
        else:
            ctx.log(f"    recorded LB with {len(lb.listeners)} listener(s), {len(lb.targets)} target(s)")
        return outputs

    def destroy(self, change: Change, ctx: ApplyContext) -> None:
        lb: LoadBalancer = change.model()  # type: ignore[assignment]
        if lb.driver == "haproxy":
            ctx.log(f"    haproxy on {lb.host} left running; remove the service manually if no longer needed")
        elif lb.driver == "terraform":
            assert lb.terraform
            run_terraform(ctx, lb.terraform, change.address, "destroy", _tf_inputs(lb, ctx, change.env))
        else:
            ctx.log("    deregistered")
