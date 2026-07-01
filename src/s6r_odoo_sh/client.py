# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""httpx-based client for the odoo.sh dashboard JSON API.

The odoo.sh dashboard is a GitHub-OAuth-gated Odoo instance whose generic
``/web/dataset/call_kw`` is locked down (``paas_master`` restrict_api). This
client uses the dashboard's own JSON routes instead:

* the authenticated builds page embeds ``odoo.sh_repo_id = <id>`` in its HTML;
* ``POST /project/json/builds_per_branch {"repository_id": id}`` returns every
  branch with ``last_build_id = [build_id, host_slug]``.

Runtime needs only httpx: it reuses a persisted browser session (Playwright
``storage_state``) and, if the odoo.sh session has expired but the GitHub
session is still alive, the GitHub OAuth code flow replays transparently while
following redirects. The interactive first login lives in :mod:`s6r_odoo_sh.login`
and requires the optional ``login`` extra (Playwright).
"""

import json
import os
import re

import httpx

DEFAULT_BASE_URL = "https://www.odoo.sh"
_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0"
_REPO_ID_RE = re.compile(r"odoo\.sh_repo_id\s*=\s*(\d+)")


def default_state_path():
    """Return the default path for the persisted browser session (XDG-friendly, project-agnostic)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "odoo-sh", "state.json")


class NeedLogin(Exception):
    """Raised when no valid session is available and an interactive login is required."""


class OdooShClient:
    """Resolve odoo.sh builds over httpx, using a persisted (Playwright) browser session."""

    def __init__(self, state_path=None, base_url=DEFAULT_BASE_URL, login_browser="firefox", login_timeout=300):
        self.state_path = state_path or default_state_path()
        self.base_url = base_url.rstrip("/")
        self.login_browser = login_browser
        self.login_timeout = login_timeout

    def list_branches(self, project):
        """Return every branch of the project's repository (name, stage, last_build_id, status, result)."""
        with self._session() as client:
            repo_id = self._repository_id(client, project)
            resp = self._json_call(client, "/project/json/builds_per_branch", {"repository_id": repo_id})
            self._save_cookies(client)
            return [self._branch(x.get("branch_info") or {}) for x in (resp.get("result") or [])]

    def get_branch(self, project, branch, auto_login=False):
        """Return the current build of ``branch`` (id, host slug, status, reconstructed SSH host)."""
        try:
            branches = self.list_branches(project)
        except NeedLogin:
            if not auto_login:
                raise
            self.login(project)
            branches = self.list_branches(project)
        for entry in branches:
            if entry.get("name") == branch:
                return self._build(project, entry)
        return {"error": "branch not found", "branch": branch,
                "available": sorted(b["name"] for b in branches if b.get("name"))}

    def get_ssh_host(self, project, branch, auto_login=False):
        """Return only the SSH host string of the branch's current build, or None."""
        return (self.get_branch(project, branch, auto_login=auto_login) or {}).get("ssh_host")

    def login(self, project):
        """Run the one-time interactive GitHub login (requires the optional ``login`` extra)."""
        from .login import interactive_login

        interactive_login(project, self.state_path, base_url=self.base_url,
                          browser=self.login_browser, timeout=self.login_timeout)

    def _session(self):
        if not os.path.exists(self.state_path):
            raise NeedLogin("no persisted session at %s (run login first)" % self.state_path)
        cookies = httpx.Cookies()
        for c in self._load_state().get("cookies", []):
            cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."), path=c.get("path", "/"))
        return httpx.Client(cookies=cookies, follow_redirects=True, timeout=30, headers={"User-Agent": _UA})

    def _repository_id(self, client, project):
        r = client.get("%s/project/%s/builds" % (self.base_url, project))
        if self._is_login_url(str(r.url)):
            raise NeedLogin("session expired: %s" % r.url)
        m = _REPO_ID_RE.search(r.text)
        if not m:
            raise RuntimeError("could not find repository id for project %r" % project)
        return int(m.group(1))

    def _json_call(self, client, route, params):
        r = client.post(self.base_url + route,
                        json={"id": 0, "jsonrpc": "2.0", "method": "call", "params": params})
        data = r.json()
        if data.get("error"):
            err = data["error"]
            raise RuntimeError((err.get("data") or {}).get("message") or err.get("message") or str(err))
        return data

    @staticmethod
    def _is_login_url(url):
        return "/web/login" in url or "github.com" in url or "/oauth/" in url

    @staticmethod
    def _branch(bi):
        return {"name": bi.get("name"), "stage": bi.get("stage"), "last_build_id": bi.get("last_build_id"),
                "status": bi.get("last_build_status"), "result": bi.get("last_build_result")}

    def _build(self, project, entry):
        build_id, slug = (list(entry.get("last_build_id") or []) + [None, None])[:2]
        return {
            "project": project, "branch": entry.get("name"), "stage": entry.get("stage"),
            "build_id": build_id, "host_slug": slug,
            "status": entry.get("status"), "result": entry.get("result"),
            "ssh_host": "%s@%s.dev.odoo.com" % (build_id, slug) if build_id and slug else None,
        }

    def _load_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def _save_cookies(self, client):
        """Persist refreshed cookies (renewed odoo.sh/GitHub sessions) back into the stored state."""
        state = self._load_state()
        by_key = {(c["name"], c["domain"].lstrip(".")): c for c in state.get("cookies", [])}
        for c in client.cookies.jar:
            key = (c.name, (c.domain or "").lstrip("."))
            if key in by_key:
                by_key[key]["value"] = c.value
            else:
                state.setdefault("cookies", []).append({
                    "name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/",
                    "secure": bool(c.secure), "httpOnly": False, "sameSite": "Lax",
                    "expires": c.expires or -1,
                })
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)
