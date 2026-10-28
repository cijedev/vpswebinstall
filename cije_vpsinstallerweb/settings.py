"""Runtime settings. Live installs are opt-in and off by default."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cije_vpsinstallerweb.paths import ROOT


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Process configuration loaded from environment variables."""

    app_name: str = "cije-vpsinstallerweb"
    token: str = ""
    host: str = "127.0.0.1"
    port: int = 18765
    data_dir: Path = Path("/tmp/cije-vpsinstallerweb")
    install_root: Path = Path("/tmp/cije-vpsinstallerweb/fake-root")
    dry_run: bool = True
    allow_live: bool = False
    cookie_name: str = "cije_web_token"
    token_generated: bool = False

    @property
    def config_path(self) -> Path:
        return self.data_dir / "vps-setup.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "web.log"

    @property
    def state_path(self) -> Path:
        if self.uses_fake_root:
            return self.install_root / "var/lib/debian-vps-setup/state.json"
        return Path("/var/lib/debian-vps-setup/state.json")

    @property
    def report_path(self) -> Path:
        if self.uses_fake_root:
            return self.install_root / "root/vps-setup-report.txt"
        return Path("/root/vps-setup-report.txt")

    @property
    def saved_cli_config_path(self) -> Path:
        if self.uses_fake_root:
            return self.install_root / "root/vps-setup.json"
        return Path("/root/vps-setup.json")

    @property
    def uses_fake_root(self) -> bool:
        return Path(self.install_root) != Path("/")

    @property
    def live_mode(self) -> bool:
        """True only when the operator explicitly allowed a real-host install."""
        return self.allow_live and not self.dry_run and Path(self.install_root) == Path("/")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.uses_fake_root:
            self.install_root.mkdir(parents=True, exist_ok=True)
            (self.install_root / "root").mkdir(parents=True, exist_ok=True)
            (self.install_root / "var/lib/debian-vps-setup").mkdir(parents=True, exist_ok=True)
            (self.install_root / "var/log").mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("CIJE_WEB_DATA", str(ROOT / "data"))).expanduser()
    allow_live = _truthy(os.environ.get("CIJE_WEB_ALLOW_LIVE"), False)
    dry_run = _truthy(os.environ.get("CIJE_WEB_DRY_RUN"), True)
    raw_root = os.environ.get("CIJE_WEB_ROOT", str(data_dir / "fake-root"))
    install_root = Path(raw_root).expanduser()

    if not allow_live:
        dry_run = True
        if install_root == Path("/"):
            install_root = data_dir / "fake-root"

    token = os.environ.get("CIJE_WEB_TOKEN", "").strip()
    generated = False
    if not token:
        token = secrets.token_urlsafe(24)
        generated = True

    settings = Settings(
        token=token,
        host=os.environ.get("CIJE_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.environ.get("CIJE_WEB_PORT", "18765")),
        data_dir=data_dir,
        install_root=install_root,
        dry_run=dry_run,
        allow_live=allow_live,
        token_generated=generated,
    )
    settings.ensure_dirs()
    return settings
