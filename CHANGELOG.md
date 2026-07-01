# Changelog

## 0.1.0

Initial release.

- `OdooShClient`: resolve the current build (number + SSH host) of an odoo.sh
  branch via the dashboard JSON routes, over httpx.
- Transparent GitHub OAuth code-flow replay when the odoo.sh session expired but
  the GitHub session is still valid.
- One-time interactive login via Playwright (optional `login` extra).
- `resolve()` convenience function and `s6r-odoo-sh` CLI (`--list`, `--ssh-host`,
  `--login`, `--install-browser`).
