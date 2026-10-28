"""Thin wrappers around debian_vps_setup — no duplicated install logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cije_vpsinstallerweb.paths import VENDOR  # noqa: F401  # sys.path
from cije_vpsinstallerweb.settings import Settings
from debian_vps_setup.config import (
    CONTROL_PANELS,
    WEB_BUNDLES,
    WEBMAIL_CHOICES,
    WEBMAIL_LABELS,
    Mailbaby,
    OnePanel,
    SetupConfig,
    Ssl,
    Vnc,
    _from_mapping,
)
from debian_vps_setup.dns import records as dns_records
from debian_vps_setup.dns import render as render_dns
from debian_vps_setup.modules.backup import (
    BACKUP_NOTE,
    create_snapshot,
    list_snapshots,
    restore_snapshot,
)
from debian_vps_setup.modules.troubleshoot import troubleshoot_components
from debian_vps_setup.modules.verify import COMPONENTS, as_result_map, canonicalize_components, run_checks
from debian_vps_setup.pipeline import names as step_names
from debian_vps_setup.pipeline import select as select_steps
from debian_vps_setup.preflight import PreflightError, detect_ip, run_preflight
from debian_vps_setup.report import build_report, write_report
from debian_vps_setup.runner import CommandError, Runner
from debian_vps_setup.state import InstallState

from debian_vps_setup import __version__ as cli_version


def make_runner(settings: Settings, *, dry_run: bool | None = None) -> Runner:
    use_dry = settings.dry_run if dry_run is None else dry_run
    if not settings.live_mode:
        use_dry = True
    return Runner(dry_run=use_dry, root=str(settings.install_root))


def make_state(settings: Settings) -> InstallState:
    state = InstallState(settings.state_path)
    state.load()
    return state


def config_from_dict(data: dict[str, Any]) -> SetupConfig:
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    cfg = _from_mapping(
        SetupConfig,
        data,
        nested={"mailbaby": Mailbaby, "vnc": Vnc, "ssl": Ssl, "onepanel": OnePanel},
    )
    cfg.fill_defaults()
    return cfg


def load_config_file(path: Path) -> SetupConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return config_from_dict(raw)


def save_config_file(path: Path, cfg: SetupConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def default_config() -> SetupConfig:
    cfg = SetupConfig(domain="example.com")
    cfg.fill_defaults()
    return cfg


def load_or_default(settings: Settings) -> SetupConfig:
    for candidate in (settings.config_path, settings.saved_cli_config_path):
        if candidate.exists():
            return load_config_file(candidate)
    return default_config()


def validate_payload(data: dict[str, Any]) -> tuple[SetupConfig, list[str]]:
    cfg = config_from_dict(data)
    return cfg, cfg.validate()


def choices() -> dict[str, Any]:
    return {
        "control_panels": list(CONTROL_PANELS),
        "web_bundles": list(WEB_BUNDLES),
        "webmail": list(WEBMAIL_CHOICES),
        "webmail_labels": dict(WEBMAIL_LABELS),
        "steps": step_names(),
        "components": list(COMPONENTS),
        "cli_version": cli_version,
    }


def status_payload(settings: Settings) -> dict[str, Any]:
    state = make_state(settings)
    report_path = settings.report_path
    report = ""
    if report_path.exists():
        report = report_path.read_text(encoding="utf-8")
    return {
        "started_at": state.data.get("started_at"),
        "finished_at": state.data.get("finished_at"),
        "updated_at": state.data.get("updated_at"),
        "last_step": state.data.get("last_step"),
        "completed": list(state.data.get("completed") or []),
        "steps": step_names(),
        "report_path": str(report_path),
        "report": report,
        "has_report": bool(report),
    }


def dns_payload(cfg: SetupConfig, runner: Runner) -> dict[str, Any]:
    ip = detect_ip(runner)
    rows = dns_records(cfg, ip)
    return {
        "ip": ip,
        "text": render_dns(cfg, ip),
        "records": [{"type": rtype, "name": name, "value": value} for rtype, name, value in rows],
    }


def snapshot_payload(snap) -> dict[str, Any]:
    copied = snap.manifest.get("copied")
    if copied is None:
        copied = sum(1 for item in snap.manifest.get("files") or [] if item.get("existed"))
    return {
        "name": snap.name,
        "created_at": snap.created_at,
        "reason": snap.reason,
        "domain": snap.manifest.get("domain") or "",
        "copied": copied,
        "path": str(snap.path),
        "note": snap.manifest.get("note") or BACKUP_NOTE,
    }


def list_backup_payload(runner: Runner) -> dict[str, Any]:
    snaps = list_snapshots(runner)
    return {"note": BACKUP_NOTE, "snapshots": [snapshot_payload(s) for s in snaps]}


def check_to_dict(check) -> dict[str, Any]:
    return {
        "name": check.name,
        "passed": check.passed,
        "detail": check.detail,
        "component": check.component,
    }


def component_report_to_dict(report) -> dict[str, Any]:
    return {
        "component": report.component,
        "before": [check_to_dict(c) for c in report.before],
        "after": [check_to_dict(c) for c in report.after],
        "actions": [
            {"kind": a.kind, "target": a.target, "detail": a.detail} for a in report.actions
        ],
        "failed_before": [c.name for c in report.failed_before],
        "failed_after": [c.name for c in report.failed_after],
    }


def run_install_job(
    cfg: SetupConfig,
    settings: Settings,
    *,
    force: bool = False,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    no_backup: bool = False,
) -> dict[str, Any]:
    """Execute the same pipeline as the CLI `run` command."""
    runner = make_runner(settings)
    state = make_state(settings)
    state.mark_started()
    save_config_file(settings.config_path, cfg)

    try:
        facts = run_preflight(
            cfg,
            runner,
            require_root=settings.live_mode,
            enforce_debian=settings.live_mode,
        )
    except PreflightError as exc:
        return {"ok": False, "error": str(exc), "phase": "preflight"}

    if not no_backup:
        create_snapshot(runner, cfg, reason="pre-run")

    try:
        steps = select_steps(only, skip)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "phase": "select", "steps": step_names()}

    completed: list[str] = []
    for name, title, fn in steps:
        if state.is_done(name) and not force and not only:
            completed.append(name)
            continue
        try:
            fn(cfg, runner)
        except CommandError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "phase": "step",
                "failed_step": name,
                "title": title,
                "completed": list(state.data.get("completed") or []),
            }
        state.mark_done(name)
        completed.append(name)

    checks = run_checks(cfg, runner)
    results = as_result_map(checks)
    report = build_report(cfg, ip=facts.get("ip") or detect_ip(runner), results=results)
    report_path = write_report(runner, cfg, report)
    state.mark_finished()
    save_config_file(settings.config_path, cfg)

    failed = [name for name, passed in results.items() if not passed]
    return {
        "ok": not failed,
        "phase": "done",
        "completed": completed,
        "failed_checks": failed,
        "checks": [check_to_dict(c) for c in checks],
        "report_path": str(report_path),
        "dry_run": runner.dry_run,
        "root": str(runner.root),
    }


def run_test_job(cfg: SetupConfig, settings: Settings) -> dict[str, Any]:
    runner = make_runner(settings)
    checks = run_checks(cfg, runner)
    failed = [c.name for c in checks if not c.passed]
    return {
        "ok": not failed,
        "checks": [check_to_dict(c) for c in checks],
        "failed_checks": failed,
        "dry_run": runner.dry_run,
    }


def run_troubleshoot_job(
    cfg: SetupConfig,
    settings: Settings,
    components: list[str] | None,
) -> dict[str, Any]:
    runner = make_runner(settings)
    names = canonicalize_components(components)
    reports = troubleshoot_components(
        cfg,
        runner,
        names,
        dry_run=runner.dry_run,
        apply_fixes=not runner.dry_run,
        take_backup=not runner.dry_run,
    )
    any_fail = any(r.failed_after for r in reports)
    return {
        "ok": not any_fail,
        "components": names,
        "reports": [component_report_to_dict(r) for r in reports],
        "dry_run": runner.dry_run,
    }


def run_backup_job(cfg: SetupConfig | None, settings: Settings) -> dict[str, Any]:
    # Snapshots copy files under --root; keep dry_run off so a fake-root snapshot is real.
    runner = Runner(dry_run=False, root=str(settings.install_root))
    if settings.live_mode:
        runner = make_runner(settings, dry_run=False)
    snap = create_snapshot(runner, cfg, reason="manual")
    listed = list_backup_payload(runner)
    listed["created"] = snapshot_payload(snap)
    listed["ok"] = True
    return listed


def run_restore_job(settings: Settings, name: str) -> dict[str, Any]:
    # File copies stay under install_root. systemctl only runs when Runner.live_system.
    runner = Runner(dry_run=False, root=str(settings.install_root))
    code = restore_snapshot(runner, name)
    listing = list_backup_payload(runner)
    listing.update({"ok": code == 0, "restored": name, "returncode": code})
    return listing
