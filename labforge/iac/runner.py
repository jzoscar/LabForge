"""Command execution with dry-run support. Every side effect of an apply goes through here."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


class CommandError(Exception):
    pass


@dataclass
class CmdResult:
    rc: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    def __init__(self, dry_run: bool, log: Callable[[str], None]):
        self.dry_run = dry_run
        self.log = log

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 900,
        check: bool = True,
    ) -> CmdResult:
        """``env`` is merged into the process environment and never logged (use it for secrets)."""
        self.log(("[dry-run] " if self.dry_run else "") + "$ " + shlex.join(argv))
        if self.dry_run:
            return CmdResult(0)
        try:
            p = subprocess.run(
                argv,
                input=input,
                env={**os.environ, **env} if env else None,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise CommandError(f"command not found: {argv[0]}") from e
        except subprocess.TimeoutExpired as e:
            raise CommandError(f"timed out after {timeout}s: {argv[0]}") from e
        for line in (p.stdout + p.stderr).splitlines()[-40:]:
            self.log("    " + line)
        if check and p.returncode != 0:
            raise CommandError(f"{argv[0]} exited with {p.returncode}")
        return CmdResult(p.returncode, p.stdout, p.stderr)
