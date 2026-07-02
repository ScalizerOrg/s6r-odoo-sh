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
import time
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_BASE_URL = "https://www.odoo.sh"
_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0"
_REPO_ID_RE = re.compile(r"odoo\.sh_repo_id\s*=\s*(\d+)")
_BUILD_IN_URL_RE = re.compile(r"/paas/build/(\d+)/")
# States where a build is still building / running its unit tests (not finished).
_RUNNING_STATES = frozenset({"pending", "queued", "waiting", "progress", "testing", "installing"})


def _parse_dump_url(url):
    """Extract ``build_id``, ``backup_datetime_utc``, ``test_dump`` and ``filestore`` from a dump URL."""
    parsed = urlparse(url)
    m = _BUILD_IN_URL_RE.search(parsed.path)
    query = parse_qs(parsed.query)

    def flag(name):
        return (query.get(name) or ["0"])[0] not in ("0", "false", "False", "")

    return {"build_id": int(m.group(1)) if m else None,
            "backup_datetime_utc": (query.get("backup_datetime_utc") or [None])[0],
            "test_dump": flag("test_dump"),
            "filestore": flag("filestore")}


def default_state_path():
    """Return the default path for the persisted browser session (XDG-friendly, project-agnostic)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "odoo-sh", "state.json")


def default_download_dir():
    """Return the user's Downloads directory (localized XDG dir, e.g. ``Téléchargements``).

    Reads ``XDG_DOWNLOAD_DIR`` from ``~/.config/user-dirs.dirs``; falls back to
    ``~/Downloads``, then ``~/Téléchargements``, then the home directory.
    """
    home = os.path.expanduser("~")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    try:
        with open(os.path.join(base, "user-dirs.dirs")) as f:
            for line in f:
                if line.strip().startswith("XDG_DOWNLOAD_DIR"):
                    value = line.split("=", 1)[1].strip().strip('"')
                    return value.replace("$HOME", home)
    except OSError:
        pass
    for name in ("Downloads", "Téléchargements"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def _dump_filename(project, branch, info):
    """Build a filesystem-safe dump filename from the project, branch and dump metadata."""
    stamp = (info.get("backup_datetime_utc") or "").replace(" ", "_").replace(":", "")
    stem = "_".join(str(p) for p in (project, branch, info.get("build_id"), stamp) if p)
    return re.sub(r"[^\w.-]+", "-", stem) + ".zip"


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

    def build_status(self, project, branch):
        """Return the current build of ``branch``: ``{build_id, status, result, status_info,
        commit, start_datetime, run_time}``."""
        with self._session() as client:
            repo_id = self._repository_id(client, project)
            result = self._json_call(client, "/project/json/builds_per_branch",
                                     {"repository_id": repo_id})["result"]
            entry = next((x for x in result if (x.get("branch_info") or {}).get("name") == branch), None)
            if not entry:
                raise ValueError("branch %r not found in project %r" % (branch, project))
            bi = entry["branch_info"]
            build = (entry.get("builds") or [{}])[0]
            result_value = build.get("result")
            return {
                "build_id": (bi.get("last_build_id") or [None])[0],
                "status": build.get("status") or bi.get("last_build_status"),
                "result": result_value if result_value is not None else bi.get("last_build_result"),
                "status_info": build.get("status_info"),
                "commit": build.get("head_commit_url"),
                "start_datetime": build.get("start_datetime"),
                "run_time": build.get("run_time"),
            }

    def wait_for_build(self, project, branch, after_build_id=None, commit=None,
                       timeout=1800, interval=10, on_start=None):
        """Wait for a build of ``branch`` to start, then finish (build + unit tests).

        Waits until a build matching ``commit`` (or newer than ``after_build_id``, else the
        current one) is present, calls ``on_start(build)`` once, then polls until the build
        leaves a running state. Returns the final build info (see :meth:`build_status`).
        Tracks the branch's latest build, which is what a single push produces.
        """
        deadline = time.monotonic() + timeout
        build = None
        while build is None:
            current = self.build_status(project, branch)
            bid = current.get("build_id") or 0
            if bid and (after_build_id is None or bid > after_build_id) \
                    and (not commit or commit in (current.get("commit") or "")):
                build = current
                if on_start:
                    on_start(build)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("no matching build started for %r/%r within %ss" % (project, branch, timeout))
            time.sleep(interval)
        while build.get("status") in _RUNNING_STATES:
            if time.monotonic() >= deadline:
                raise TimeoutError("build %s did not finish within %ss" % (build.get("build_id"), timeout))
            time.sleep(interval)
            build = self.build_status(project, branch)
        return build

    def list_backups(self, project):
        """Return the repository's backups (``type``, ``backup_datetime_utc``, ``path``, …)."""
        with self._session() as client:
            repo_id = self._repository_id(client, project)
            return self._call_kw(client, "paas.repository", "get_backups_info_public", [repo_id])

    def create_backup(self, project, branch=None, comment="", build_id=None):
        """Create a persistent backup of a build (``build_id`` or ``branch``'s current build)."""
        build_id = self._resolve_build_id(project, branch, build_id)
        with self._session() as client:
            return self._json_call(client, "/build/%s/dump" % build_id,
                                   {"backup_only": True, "comment": comment})["result"]

    def start_dump(self, project, branch=None, test_dump=True, filestore=False,
                   backup_datetime_utc=None, build_id=None):
        """Trigger generation of a downloadable dump for a build (``build_id`` or ``branch``, async)."""
        build_id = self._resolve_build_id(project, branch, build_id)
        params = {"test_dump": test_dump, "filestore": filestore}
        if backup_datetime_utc:
            params["backup_datetime_utc"] = backup_datetime_utc
        with self._session() as client:
            return self._json_call(client, "/build/%s/dump" % build_id, params)["result"]

    def dump_notifications(self, project):
        """Return the project's 'Database dump ready' notifications, oldest first.

        Each item is ``{id, create_date, name, url}`` where ``url`` is the ready-to-use
        download link odoo.sh published (correct worker, build id and backup_datetime_utc).
        """
        with self._session() as client:
            repo_id = self._repository_id(client, project)
            init = self._json_call(client, "/project/json/init",
                                   {"repository_id": repo_id, "customs_only": True})["result"]
            entry = (init.get("notifications") or {}).get(str(repo_id)) or {}
            dumps = []
            for item in entry.get("items", []):
                if item.get("notif_type") != "dump":
                    continue
                url = next((b.get("url") for b in (item.get("buttons") or []) if b.get("type") == "download"), None)
                if url:
                    dumps.append({"id": item.get("id"), "create_date": item.get("create_date"),
                                  "name": item.get("name"), "url": url})
            return sorted(dumps, key=lambda d: d["id"] or 0)

    def download_url(self, url, dest):
        """Download a dump URL (e.g. from a notification) to ``dest``. Returns ``dest``."""
        with self._session() as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        return dest

    def download_ready_dump(self, project, dest=None, backup_datetime_utc=None, build_id=None):
        """Download an already-prepared dump from the project's notifications — no new dump.

        Picks the notification matching ``build_id`` and/or ``backup_datetime_utc`` (or the
        most recent one), and writes to ``dest`` (a file, a directory, or Downloads by default).
        """
        notifs = self.dump_notifications(project)
        if build_id:
            notifs = [n for n in notifs if _parse_dump_url(n["url"]).get("build_id") == int(build_id)]
        if backup_datetime_utc:
            notifs = [n for n in notifs
                      if _parse_dump_url(n["url"]).get("backup_datetime_utc") == backup_datetime_utc]
        if not notifs:
            raise ValueError("no ready dump notification for %r%s%s" %
                             (project,
                              " build %s" % build_id if build_id else "",
                              " at %s" % backup_datetime_utc if backup_datetime_utc else ""))
        notif = notifs[-1]
        result = {"notif_id": notif["id"], "url": notif["url"]}
        result.update(_parse_dump_url(notif["url"]))
        target = self._resolve_dest(dest, project, "", result)
        self.download_url(notif["url"], target)
        result["dest"] = target
        return result

    def wait_for_dump(self, project, since_id=0, match=None, timeout=600, interval=5):
        """Poll notifications until a matching dump newer than ``since_id`` is ready; return it.

        ``match`` (optional) restricts candidates to notifications whose download URL has the
        given fields (e.g. ``{"build_id": …, "test_dump": True, "filestore": False}``), so a
        different in-flight dump's notification is not mistaken for the requested one.
        """
        deadline = time.monotonic() + timeout
        while True:
            candidates = [d for d in self.dump_notifications(project)
                          if (d["id"] or 0) > since_id and self._matches(d["url"], match)]
            if candidates:
                return candidates[-1]
            if time.monotonic() >= deadline:
                raise TimeoutError("no matching dump ready for %r within %ss" % (project, timeout))
            time.sleep(interval)

    @staticmethod
    def _matches(url, match):
        if not match:
            return True
        info = _parse_dump_url(url)
        return all(info.get(k) == v for k, v in match.items())

    def create_dump(self, project, branch=None, dest=None, test_dump=True, filestore=False,
                    build_id=None, timeout=600, interval=5):
        """Full flow: trigger a dump of a build (``build_id`` or ``branch``), wait, and download it.

        ``dest`` may be a file path or a directory; when omitted it defaults to the
        user's Downloads directory with an auto-generated name. Returns
        ``{notif_id, url, build_id, backup_datetime_utc, test_dump, filestore, dest}``.
        """
        build_id = self._resolve_build_id(project, branch, build_id)
        since_id = max((d["id"] or 0 for d in self.dump_notifications(project)), default=0)
        self.start_dump(project, test_dump=test_dump, filestore=filestore, build_id=build_id)
        match = {"build_id": build_id, "test_dump": bool(test_dump), "filestore": bool(filestore)}
        notif = self.wait_for_dump(project, since_id=since_id, match=match, timeout=timeout, interval=interval)
        result = {"notif_id": notif["id"], "url": notif["url"]}
        result.update(_parse_dump_url(notif["url"]))
        target = self._resolve_dest(dest, project, branch or "", result)
        self.download_url(notif["url"], target)
        result["dest"] = target
        return result

    @staticmethod
    def _resolve_dest(dest, project, branch, info):
        """Resolve ``dest`` to a file path (a file stays as-is; a dir or None → auto name)."""
        if dest and not os.path.isdir(dest):
            return dest
        directory = dest or default_download_dir()
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, _dump_filename(project, branch, info))

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

    def _call_kw(self, client, model, method, args=None, kwargs=None):
        r = client.post(self.base_url + "/web/dataset/call_kw",
                        json={"jsonrpc": "2.0", "method": "call",
                              "params": {"model": model, "method": method,
                                         "args": args or [], "kwargs": kwargs or {}}})
        data = r.json()
        if data.get("error"):
            err = data["error"]
            raise RuntimeError((err.get("data") or {}).get("message") or err.get("message") or str(err))
        return data["result"]

    def _resolve_build_id(self, project, branch=None, build_id=None):
        """Return ``build_id`` if given, else the current build id of ``branch``."""
        if build_id:
            return int(build_id)
        if not branch:
            raise ValueError("provide either build_id or branch")
        return self._current_build_id(project, branch)

    def _current_build_id(self, project, branch):
        """Return the current build id of ``branch``."""
        with self._session() as client:
            _repo_id, build_id, _worker = self._branch_build(client, project, branch)
            return build_id

    def _branch_build(self, client, project, branch):
        """Return ``(repo_id, build_id, worker_url)`` for a branch's current build."""
        repo_id = self._repository_id(client, project)
        result = self._json_call(client, "/project/json/builds_per_branch", {"repository_id": repo_id})["result"]
        entry = next((x for x in result if (x.get("branch_info") or {}).get("name") == branch), None)
        if not entry:
            raise ValueError("branch %r not found in project %r" % (branch, project))
        build_id = entry["branch_info"]["last_build_id"][0]
        builds = entry.get("builds") or []
        worker_url = builds[0].get("worker_url") if builds else None
        return repo_id, build_id, worker_url

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
