from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labforge.iac.workspace import Workspace


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_url: str = "sqlite:///labforge.db"
    iac_path: Path = Path("infra")
    # Scratch space for generated files (terraform var files, etc.)
    data_dir: Path = Path(".labforge")
    # Required on mutating API calls and agent reports when set.
    api_token: str | None = None
    monitor_enabled: bool = True
    monitor_interval: int = 30
    agent_stale_after: int = 120
    retention_hours: int = 24
    # Real (non dry-run) applies from the web UI are off unless explicitly enabled.
    ui_apply: bool = False
    # Editing, importing and deleting IaC files from the web UI.
    ui_edit: bool = True
    # Base URL people reach LabForge on; used for links in notifications.
    public_url: str = "http://127.0.0.1:8000"
    # Verifies Slack button clicks (Slack app "Signing Secret").
    slack_signing_secret: str | None = None

    def workspace(self) -> Workspace:
        from labforge.iac.workspace import Workspace

        root = self.iac_path if self.iac_path.is_dir() or self.iac_path.suffix not in {".yaml", ".yml"} \
            else self.iac_path.parent
        return Workspace(root, self.data_dir / "history")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_url=os.getenv("LABFORGE_DB_URL", cls.db_url),
            iac_path=Path(os.getenv("LABFORGE_IAC_PATH", str(cls.iac_path))),
            data_dir=Path(os.getenv("LABFORGE_DATA_DIR", str(cls.data_dir))),
            api_token=os.getenv("LABFORGE_API_TOKEN") or None,
            monitor_enabled=_bool("LABFORGE_MONITOR", cls.monitor_enabled),
            monitor_interval=int(os.getenv("LABFORGE_MONITOR_INTERVAL", cls.monitor_interval)),
            agent_stale_after=int(os.getenv("LABFORGE_AGENT_STALE_AFTER", cls.agent_stale_after)),
            retention_hours=int(os.getenv("LABFORGE_RETENTION_HOURS", cls.retention_hours)),
            ui_apply=_bool("LABFORGE_UI_APPLY", cls.ui_apply),
            ui_edit=_bool("LABFORGE_UI_EDIT", cls.ui_edit),
            public_url=os.getenv("LABFORGE_PUBLIC_URL", cls.public_url).rstrip("/"),
            slack_signing_secret=os.getenv("LABFORGE_SLACK_SIGNING_SECRET") or None,
        )
