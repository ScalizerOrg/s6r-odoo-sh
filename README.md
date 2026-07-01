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

## CLI

```bash
# one-time interactive login (opens a browser window)
s6r-odoo-sh --login -p grandiflora BRANCH

# current build of a branch (headless afterwards)
s6r-odoo-sh migration_19_v1 -p grandiflora
s6r-odoo-sh migration_19_v1 -p grandiflora --ssh-host   # host string only
s6r-odoo-sh --list          -p grandiflora              # all branches

# ODOO_SH_PROJECT / ODOO_SH_STATE env vars are honored as defaults
```

Example output:

```json
{
  "project": "grandiflora",
  "branch": "migration_19_v1",
  "stage": "dev",
  "build_id": 34305175,
  "host_slug": "grandiflora-migration-19-v1-34305175",
  "status": "done",
  "result": "success",
  "ssh_host": "34305175@grandiflora-migration-19-v1-34305175.dev.odoo.com"
}
```

## Python API

```python
from s6r_odoo_sh import OdooShClient, resolve

# one-shot (auto-login on first use)
build = resolve("grandiflora", "migration_19_v1")
print(build["build_id"], build["ssh_host"])

# reusable client, custom session path
client = OdooShClient(state_path="/path/to/state.json")
host = client.get_ssh_host("grandiflora", "migration_19_v1", auto_login=True)
for b in client.list_branches("grandiflora"):
    print(b["name"], b["last_build_id"])
```

`OdooShClient(state_path=None, base_url="https://www.odoo.sh", login_browser="firefox", login_timeout=300)` —
`state_path` defaults to `$XDG_CONFIG_HOME/odoo-sh/state.json`. `NeedLogin` is
raised when no valid session exists and `auto_login` is not set.

## License

LGPL-3.0-or-later.
