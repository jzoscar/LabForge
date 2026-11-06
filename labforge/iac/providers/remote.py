"""Remote access helpers shared by providers and monitoring: SSH, Docker-over-SSH, WinRM."""

from __future__ import annotations

from typing import TYPE_CHECKING

from labforge.iac.providers.base import ProviderError
from labforge.iac.secrets import SecretResolver
from labforge.iac.spec import Host

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext


def ssh_argv(host: Host, secrets: SecretResolver, remote: str | None = None) -> list[str]:
    c = host.connection
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if c.port:
        argv += ["-p", str(c.port)]
    if c.key_ref:
        argv += ["-i", secrets.get(c.key_ref)]
    argv.append(f"{c.user}@{host.address}" if c.user else str(host.address))
    if remote:
        argv.append(remote)
    return argv


def docker_argv(host: Host) -> list[str]:
    """Docker CLI prefix targeting the host. ``connection: none`` means the local docker daemon.
    Docker-over-SSH uses your ssh config / ssh-agent for keys (``keyRef`` is not passed)."""
    c = host.connection
    if c.type == "none":
        return ["docker"]
    if c.type != "ssh":
        raise ProviderError(f"host {host.name}: docker needs an ssh connection")
    target = f"{c.user}@{host.address}" if c.user else str(host.address)
    if c.port:
        target += f":{c.port}"
    return ["docker", "-H", f"ssh://{target}"]


def winrm_run(ctx: ApplyContext, host: Host, script: str) -> str:
    c = host.connection
    ctx.log(("[dry-run] " if ctx.dry_run else "") + f"winrm {c.user or 'Administrator'}@{host.address}> {script}")
    if ctx.dry_run:
        return ""
    try:
        import winrm  # type: ignore[import-not-found]
    except ImportError as e:
        raise ProviderError("WinRM support needs: pip install 'labforge[windows]'") from e
    scheme = "https" if c.https else "http"
    port = c.port or (5986 if c.https else 5985)
    session = winrm.Session(
        f"{scheme}://{host.address}:{port}/wsman",
        auth=(c.user or "Administrator", ctx.secrets.get(c.password_ref or "")),
        transport=c.transport,
        server_cert_validation="ignore",
    )
    r = session.run_ps(script)
    out = r.std_out.decode(errors="replace")
    for line in (out + r.std_err.decode(errors="replace")).splitlines()[-40:]:
        ctx.log("    " + line)
    if r.status_code != 0:
        raise ProviderError(f"winrm command exited with {r.status_code}")
    return out
