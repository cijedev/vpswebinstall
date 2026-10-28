Vendored Cije VPS Installer (CLI)
=================================

This directory contains a copy of the **Cije VPS Installer** Python package
(`debian_vps_setup` 1.1.0) so `cije-vpsinstallerweb` can import it without
rewriting install logic.

Do not treat this web repository as a replacement for the CLI project. The
canonical CLI lives in its own tree (`cije-vps-installer` / `debian-vps-setup`).
Update this vendor copy when the CLI package changes.

Import path: `debian_vps_setup` (this folder is on `sys.path` via the web app).
