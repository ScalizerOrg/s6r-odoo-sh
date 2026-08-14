# Changelog

## 0.2.2

- Port every dashboard call to odoo.sh's new `/app/*` JSON API. odoo.sh moved its
  project portal to a JavaScript SPA (Odoo 18): the builds page is now an empty HTML
  shell, so scraping `odoo.sh_repo_id` from the HTML broke with
  `could not find repository id for project '<name>'` on **every** project (the repo
  exists and the session is valid — the marker is simply gone). Resolution now uses
  the SPA's own routes, with the public API and return shapes unchanged:
  - repository via `POST /app/projects` (name → `id` + `technical_name`);
  - branches via `POST /app/project/<technical_name>/branches`;
  - builds via `POST /app/branch/<branch_id>/builds` — the SSH host is now derived
    from the build URL (`<build_id>@<hostname>`) instead of a hard-coded
    `.dev.odoo.com` slug, so it is correct across stages;
  - backups via `POST /app/project/<technical_name>/backups`;
  - dump / backup triggers via `POST /app/build/<build_id>/dump`;
  - dump-ready notifications from the repository's `notification_counts`.
- Move session-expiry detection into the `/app/*` layer (HTML shell / login redirect
  or a `SessionExpired` JSON error → `NeedLogin`): the old builds-page redirect check
  no longer applies, since the shell now returns `200` whether or not logged in.

## 0.2.1

- Fix `build_status` / `wait_for_build`: source every field (id, status, commit) from a single
  build in the branch's build list, selected by `build_id`/`commit`, instead of mixing a build id
  from `branch_info` with the status/commit of `builds[0]`. `wait_for_build` now tracks the matched
  build by its id until it finishes — previously, while the list reordered as builds start/finish,
  the commit match could test the wrong build and never complete even though the target was done.

## 0.2.0

- `list_backups(project)`: repository backups via `paas.repository.get_backups_info_public`.
- `create_backup(project, branch, comment="")`: persistent backup of a branch's current build
  (`POST /build/<id>/dump {backup_only: true}`).
- `create_dump(project, branch=None, dest=None, test_dump=True, filestore=False, build_id=None)`: full
  async flow — triggers the dump, waits for the "Database dump ready" notification (authoritative
  download URL), and downloads the ZIP (`dump.sql` + `filestore/`) to Downloads by default.
- `download_ready_dump(project, dest=None, backup_datetime_utc=None, build_id=None)`: download an
  already-prepared dump from the notifications, without triggering a new one.
- Also `start_dump`, `dump_notifications`, `wait_for_dump`, `download_url`, `default_download_dir`.
  Any build-targeting method accepts a `build_id` (else it resolves the branch's current build).
- `build_status(project, branch)` and `wait_for_build(project, branch, after_build_id=None,
  commit=None, on_start=None)`: watch a branch's build — wait for a (new) build to start after a
  push, then for it to finish (build + unit tests), returning `{build_id, status, result,
  status_info, commit, run_time}`.
- CLI: `--backups`, `--dump-notifs`, `--create-backup` (`--comment`), `--create-dump [PATH]`,
  `--download-dump [PATH]` (`--backup-datetime`), `--build-status`, `--wait-build` (`--commit`,
  `--after-build`), `--build <id>`, `--filestore`, `--prod`, `--timeout`.

## 0.1.0

Initial release.

- `OdooShClient`: resolve the current build (number + SSH host) of an odoo.sh
  branch via the dashboard JSON routes, over httpx.
- Transparent GitHub OAuth code-flow replay when the odoo.sh session expired but
  the GitHub session is still valid.
- One-time interactive login via Playwright (optional `login` extra).
- `resolve()` convenience function and `s6r-odoo-sh` CLI (`--list`, `--ssh-host`,
  `--login`, `--install-browser`).
