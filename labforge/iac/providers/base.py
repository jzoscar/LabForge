from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from labforge.iac.applier import ApplyContext
    from labforge.iac.planner import Change


class ProviderError(Exception):
    pass


class Provider(Protocol):
    def apply(self, change: Change, ctx: ApplyContext) -> dict:
        """Create or update the resource; return outputs to record in state."""

    def destroy(self, change: Change, ctx: ApplyContext) -> None: ...


def workspace(address: str) -> str:
    return address.replace(".", "-")
