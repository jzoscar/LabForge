from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
        )
