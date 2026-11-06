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
        secret_env = {k: ctx.secrets.get(ref) for k, ref in sorted(c.secret_env.items())}

        ctx.runner.run(docker + ["pull", c.image])
        for name in c.instance_names:
            ctx.runner.run(docker + ["rm", "-f", name], check=False)
            ctx.runner.run(self._run_argv(docker, c, name, change), env=secret_env)
        # Scaled down (or switched from one container to replicas): remove what is no longer declared.
        if change.old_spec:
            old = Container.model_validate(change.old_spec)
            old_docker = docker_argv(ctx.host(change.env, old.host))
            for name in set(old.instance_names) - set(c.instance_names) if old.host == c.host else old.instance_names:
                ctx.runner.run(old_docker + ["rm", "-f", name], check=False)
        return {"host": c.host, "image": c.image, "instances": c.instance_names}

    @staticmethod
    def _run_argv(docker: list[str], c: Container, name: str, change: Change) -> list[str]:
        argv = docker + ["run", "-d", "--name", name, "--restart", c.restart,
                         "--label", f"labforge.address={change.address}",
                         "--label", f"labforge.hash={change.hash}"]
        for p in c.ports:
            argv += ["-p", p]
        for k, v in sorted(c.env.items()):
            argv += ["-e", f"{k}={v}"]
        for k in sorted(c.secret_env):
            argv += ["-e", k]  # docker reads the value from its own environment
        for v in c.volumes:
            argv += ["-v", v]
        for k, v in sorted(c.labels.items()):
            argv += ["--label", f"{k}={v}"]
        return argv + [c.image, *(c.command or [])]

    def destroy(self, change: Change, ctx: ApplyContext) -> None:
        c: Container = change.model()  # type: ignore[assignment]
        docker = docker_argv(ctx.host(change.env, c.host))
        for name in c.instance_names:
            ctx.runner.run(docker + ["rm", "-f", name], check=False)
