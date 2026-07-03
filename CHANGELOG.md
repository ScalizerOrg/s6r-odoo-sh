# Changelog

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
