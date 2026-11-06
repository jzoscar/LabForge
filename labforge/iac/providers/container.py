from __future__ import annotations

from typing import TYPE_CHECKING

from labforge.iac.providers.remote import docker_argv
from labforge.iac.spec import Container

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext
    from labforge.iac.planner import Change


class ContainerProvider:
    def apply(self, change: Change, ctx: ApplyContext) -> dict:
        c: Container = change.model()  # type: ignore[assignment]
        docker = docker_argv(ctx.host(change.env, c.host))

        argv = docker + ["run", "-d", "--name", c.name, "--restart", c.restart,
                         "--label", f"labforge.address={change.address}",
                         "--label", f"labforge.hash={change.hash}"]
        for p in c.ports:
            argv += ["-p", p]
        for k, v in sorted(c.env.items()):
            argv += ["-e", f"{k}={v}"]
        secret_env: dict[str, str] = {}
        for k, ref in sorted(c.secret_env.items()):
            argv += ["-e", k]  # docker reads the value from its own environment
            secret_env[k] = ctx.secrets.get(ref)
        for v in c.volumes:
            argv += ["-v", v]
        for k, v in sorted(c.labels.items()):
            argv += ["--label", f"{k}={v}"]
        argv.append(c.image)
        argv += c.command or []

        ctx.runner.run(docker + ["pull", c.image])
        ctx.runner.run(docker + ["rm", "-f", c.name], check=False)
        ctx.runner.run(argv, env=secret_env)
        return {"host": c.host, "image": c.image}

    def destroy(self, change: Change, ctx: ApplyContext) -> None:
        c: Container = change.model()  # type: ignore[assignment]
        ctx.runner.run(docker_argv(ctx.host(change.env, c.host)) + ["rm", "-f", c.name], check=False)
