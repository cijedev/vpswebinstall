"""CLI entry: python -m cije_vpsinstallerweb"""

from __future__ import annotations

import argparse

import uvicorn

from cije_vpsinstallerweb import __version__
from cije_vpsinstallerweb.app import create_app
from cije_vpsinstallerweb.settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cije-vpsinstallerweb",
        description="Web UI for the Cije VPS Installer (vendored debian_vps_setup).",
    )
    parser.add_argument("--host", help="bind address (default: CIJE_WEB_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="bind port (default: CIJE_WEB_PORT or 18765)")
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    print(f"cije-vpsinstallerweb {__version__}")
    print(f"listening on http://{settings.host}:{settings.port}")
    print(f"mode: {'LIVE' if settings.live_mode else 'dry-run / fake-root'}")
    print(f"install root: {settings.install_root}")
    if settings.token_generated:
        print(f"CIJE_WEB_TOKEN was not set; generated token:\n  {settings.token}")
        print("Set CIJE_WEB_TOKEN in the environment for a stable secret.")
    else:
        print("auth: CIJE_WEB_TOKEN is set")

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
