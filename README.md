# s6r-odoo-sh

Resolve the **current build** of an [odoo.sh](https://www.odoo.sh) branch — its
build number and its live SSH host — from Python.

Odoo.sh recycles old dev builds, so the SSH host of a branch changes on every
rebuild and any host cached elsewhere quickly dies. The current build lives only
in the odoo.sh dashboard (a GitHub-OAuth-gated Odoo instance), whose generic RPC
is locked down. `s6r-odoo-sh` reads it through the dashboard's own JSON routes.

## How it works

* **Runtime is httpx-only** (light). It reuses a persisted browser session and,
  if the odoo.sh session expired but the GitHub session is still alive, the
  GitHub OAuth code flow replays transparently while following redirects.
* **Login is interactive and one-time** (the "PyCharm / `gh auth login`" model):
  a browser window opens once for the GitHub login, the session is persisted,
  and every subsequent call runs headless. This step needs the optional `login`
  extra (Playwright); the runtime does not.

## Install

```bash
pip install s6r-odoo-sh                 # runtime only (httpx)
pip install "s6r-odoo-sh[login]"        # + Playwright, for the one-time login
s6r-odoo-sh --install-browser           # download the Playwright browser (once)
```

### Developing against a consumer project (editable install)

When working on this package alongside a project that depends on it (e.g.
`scalidev`), install it in **editable mode** from this checkout instead of a
published release:

```bash
pip install -e ~/Projects/Interne/s6r-odoo-sh
```

This points the consumer's Python environment straight at this repo's
`src/s6r_odoo_sh/` — no copy is made into `site-packages/`. Any edit made here
takes effect the next time a process in that environment imports the package
(just restart it; no `pip install` needed after the first time). Check with:

```bash
pip show s6r-odoo-sh   # look for "Editable project location: ..."
```

To go back to the published version pinned in the consumer's
`requirements.txt`, reinstall normally there (e.g.
`pip install --no-build-isolation -r requirements.txt`).

## CLI

```bash
# one-time interactive login (opens a browser window)
s6r-odoo-sh --login -p myproject BRANCH

# current build of a branch (headless afterwards)
s6r-odoo-sh migration_19_v1 -p myproject
s6r-odoo-sh migration_19_v1 -p myproject --ssh-host   # host string only
s6r-odoo-sh --list          -p myproject              # all branches

# ODOO_SH_PROJECT / ODOO_SH_STATE env vars are honored as defaults
```

Example output:

```json
{
  "project": "myproject",
  "branch": "migration_19_v1",
  "stage": "dev",
  "build_id": 34305175,
  "host_slug": "myproject-migration-19-v1-34305175",
  "status": "done",
  "result": "success",
  "ssh_host": "34305175@myproject-migration-19-v1-34305175.dev.odoo.com"
}
```

## Python API

```python
from s6r_odoo_sh import OdooShClient, resolve

# one-shot (auto-login on first use)
build = resolve("myproject", "migration_19_v1")
print(build["build_id"], build["ssh_host"])

# reusable client, custom session path
client = OdooShClient(state_path="/path/to/state.json")
host = client.get_ssh_host("myproject", "migration_19_v1", auto_login=True)
for b in client.list_branches("myproject"):
    print(b["name"], b["last_build_id"])
```

`OdooShClient(state_path=None, base_url="https://www.odoo.sh", login_browser="firefox", login_timeout=300)` —
`state_path` defaults to `$XDG_CONFIG_HOME/odoo-sh/state.json`. `NeedLogin` is
raised when no valid session exists and `auto_login` is not set.

## Watching a build (CI: build + unit tests)

```bash
s6r-odoo-sh --build-status -p myproject BRANCH        # current status/result/status_info

# after `git push`: wait for the new build to start, then for the tests to finish
s6r-odoo-sh --wait-build -p myproject BRANCH --commit "$(git rev-parse HEAD)"
s6r-odoo-sh --wait-build -p myproject BRANCH --after-build 34345516   # or by baseline build id
```

```python
res = client.wait_for_build("myproject", "BRANCH", commit="abc123…")
# → {build_id, status: "done", result: "success"|"failed"|"warning", status_info, run_time, …}
```

`status_info` carries the test outcome (e.g. which modules failed). Match the build you pushed
with `commit` (SHA) or `after_build_id` (a build id captured before pushing).

## Backups & database dumps

```bash
# create & download a dump of a branch in one go: triggers it, waits for the
# "Database dump ready" notification, then downloads the ZIP
s6r-odoo-sh --create-dump          -p myproject BRANCH   # → Downloads folder, auto-named
s6r-odoo-sh --create-dump ./db.zip -p myproject BRANCH   # → explicit path (file or dir)
#   --filestore  include the filestore   --prod  production dump (default: neutralized test dump)

# download an ALREADY-prepared dump (no new dump) — pick it by build id or by date
s6r-odoo-sh --download-dump -p myproject --build 34345516
s6r-odoo-sh --download-dump -p myproject --backup-datetime "2026-07-02 13:40:53"

# create a persistent backup of a branch's current build
s6r-odoo-sh --create-backup -p myproject BRANCH --comment "before migration"

# any command targeting a build accepts --build <id> instead of a branch
s6r-odoo-sh --create-dump -p myproject --build 34345516

# inspect
s6r-odoo-sh --backups     -p myproject   # repository backups
s6r-odoo-sh --dump-notifs -p myproject   # ready "database dump" notifications (with URLs)
```

```python
res = client.create_dump("myproject", "BRANCH", "db.zip")   # → {notif_id, url, build_id, backup_datetime_utc, dest}
client.create_backup("myproject", "BRANCH", comment="…")
client.dump_notifications("myproject")                      # ready dumps + their download URLs
client.download_url(url, "db.zip")                          # download any dump URL
```

Dumps are asynchronous: odoo.sh assigns the `backup_datetime_utc` and publishes a
notification carrying the authoritative download URL, which `create_dump` waits for.
It only accepts a notification newer than the ones present before the request **and**
whose build id and `test_dump`/`filestore` options match what it triggered, so a
different in-flight dump is not picked up by mistake. (odoo.sh returns no request id,
so two identical dumps of the same build launched at the same instant cannot be told
apart — an inherent, documented limitation.) The download is a ZIP (`dump.sql` +
`filestore/`); the worker authorizes it from the session via a redirect, and prepared
test dumps stay available for ~2h.

## License

LGPL-3.0-or-later.
