# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""httpx-based client for the odoo.sh dashboard JSON API.

The odoo.sh dashboard is a GitHub-OAuth-gated Odoo instance. Its project portal
is a JavaScript SPA (``paas_master``) served as an empty HTML shell; all data is
fetched by the client through the dashboard's own ``/app/*`` JSON routes:

* ``POST /app/projects`` → ``{hosting_user_id, repos: [{id, name, technical_name,
  …}]}`` — the accessible projects (name → repo id / technical_name);
* ``POST /app/project/<technical_name>/branches`` → every branch
  (``id``, ``name``, ``stage``, ``last_build_id = [build_id, host_slug]``, …);
* ``POST /app/branch/<branch_id>/builds`` → ``[{branch_info, builds: [{id, url,
  status, result, …}]}]`` — the branch's builds (the SSH host is derived from
  ``build.url``);
* ``POST /app/build/<build_id>/dump`` — trigger a downloadable dump / backup;
* ``POST /app/project/<technical_name>/backups`` — the repository backups.

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
            repo = self._repo(client, project)
            branches = self._app(client, "/app/project/%s/branches" % repo["technical_name"]) or []
            self._save_cookies(client)
            return [self._branch(b) for b in branches]

    def get_branch(self, project, branch, auto_login=False):
        """Return the current build of ``branch`` (id, host slug, status, reconstructed SSH host)."""
        try:
            with self._session() as client:
                repo = self._repo(client, project)
                branches = self._app(client, "/app/project/%s/branches" % repo["technical_name"]) or []
                entry = next((b for b in branches if b.get("name") == branch), None)
                if entry is None:
                    return {"error": "branch not found", "branch": branch,
                            "available": sorted(b.get("name") for b in branches if b.get("name"))}
                builds = self._builds(client, entry.get("id"))
                self._save_cookies(client)
                return self._build(project, self._branch(entry), builds)
        except NeedLogin:
            if not auto_login:
                raise
            self.login(project)
            return self.get_branch(project, branch, auto_login=False)

    def get_ssh_host(self, project, branch, auto_login=False):
        """Return only the SSH host string of the branch's current build, or None."""
        return (self.get_branch(project, branch, auto_login=auto_login) or {}).get("ssh_host")

    def build_status(self, project, branch, commit=None, build_id=None):
        """Return a build of ``branch``: ``{build_id, status, result, status_info, commit,
        start_datetime, run_time}``.

        Selects the build matching ``build_id`` or ``commit`` within the branch's build list
        and sources **every** field from that single build (falling back to the latest build,
        then to the branch's ``last_build_*``). This avoids mixing a build id from one build
        with the status/commit of another — which diverge while a build is starting/finishing.
        """
        with self._session() as client:
            repo = self._repo(client, project)
            branches = self._app(client, "/app/project/%s/branches" % repo["technical_name"]) or []
            entry = next((b for b in branches if b.get("name") == branch), None)
            if not entry:
                raise ValueError("branch %r not found in project %r" % (branch, project))
            builds = self._builds(client, entry.get("id"))
            build = None
            if build_id:
                build = next((b for b in builds if b.get("id") == build_id), None)
            elif commit:
                build = next((b for b in builds if commit in (b.get("head_commit_url") or "")), None)
            if build is None:
                build = builds[0] if builds else {}
            last_build_id = entry.get("last_build_id") or [None]
            result_value = build.get("result")
            return {
                "build_id": build.get("id") or last_build_id[0],
                "status": build.get("status") or entry.get("last_build_status"),
                "result": result_value if result_value is not None else entry.get("last_build_result"),
                "status_info": build.get("status_info"),
                "commit": build.get("head_commit_url"),
                "start_datetime": build.get("start_datetime"),
                "run_time": build.get("run_time"),
            }

    def wait_for_build(self, project, branch, after_build_id=None, commit=None,
                       timeout=1800, interval=10, on_start=None):
        """Wait for a build of ``branch`` to start, then finish (build + unit tests).

        Locates the target build (matching ``commit``, newer than ``after_build_id``, else the
        latest) in the branch's build list, calls ``on_start(build)`` once, then tracks **that
        specific build id** until it leaves a running state. Returns its final info. Tracking a
        fixed build id (rather than ``builds[0]``) is robust while the build list reorders as
        builds start/finish. Returns the final build info (see :meth:`build_status`).
        """
        deadline = time.monotonic() + timeout
        target_id = None
        while target_id is None:
            info = self.build_status(project, branch, commit=commit)
            bid = info.get("build_id") or 0
            if bid and (after_build_id is None or bid > after_build_id) \
                    and (not commit or commit in (info.get("commit") or "")):
                target_id = bid
                if on_start:
                    on_start(info)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("no matching build started for %r/%r within %ss" % (project, branch, timeout))
            time.sleep(interval)
        while True:
            info = self.build_status(project, branch, build_id=target_id)
            if info.get("status") not in _RUNNING_STATES:
                return info
            if time.monotonic() >= deadline:
                raise TimeoutError("build %s did not finish within %ss" % (target_id, timeout))
            time.sleep(interval)

    def list_backups(self, project):
        """Return the repository's backups (``type``, ``backup_datetime_utc``, ``branch``, ``path``, …)."""
        with self._session() as client:
            repo = self._repo(client, project)
            return self._app(client, "/app/project/%s/backups" % repo["technical_name"]) or []

    def create_backup(self, project, branch=None, comment="", build_id=None):
        """Create a persistent backup of a build (``build_id`` or ``branch``'s current build)."""
        build_id = self._resolve_build_id(project, branch, build_id)
        with self._session() as client:
            return self._app(client, "/app/build/%s/dump" % build_id,
                             {"backup_only": True, "comment": comment})

    def start_dump(self, project, branch=None, test_dump=True, filestore=False,
                   backup_datetime_utc=None, build_id=None):
        """Trigger generation of a downloadable dump for a build (``build_id`` or ``branch``, async)."""
        build_id = self._resolve_build_id(project, branch, build_id)
        params = {"test_dump": test_dump, "filestore": filestore}
        if backup_datetime_utc:
            params["backup_datetime_utc"] = backup_datetime_utc
        with self._session() as client:
            return self._app(client, "/app/build/%s/dump" % build_id, params)

    def dump_notifications(self, project):
        """Return the project's 'Database dump ready' notifications, oldest first.

        Each item is ``{id, create_date, name, url}`` where ``url`` is the ready-to-use
        download link odoo.sh published. The notifications are the repository's unseen
        ``notification_counts.items`` (a dump raises one when it is ready to download).
        """
        with self._session() as client:
            repo = self._repo(client, project)
            items = ((repo.get("notification_counts") or {}).get("items")) or []
            dumps = []
            for item in items:
                if item.get("notif_type") != "dump":
                    continue
                url = self._download_button_url(item)
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

    def _app(self, client, route, params=None):
        """POST a ``/app/*`` JSON-RPC route and return its ``result``.

        Raises :class:`NeedLogin` when the session is gone (odoo.sh answers with the HTML
        SPA shell / a login redirect, or a SessionExpired JSON error) and :class:`RuntimeError`
        on any other JSON error or unexpected (non-JSON) response.
        """
        r = client.post(self.base_url + route,
                        json={"jsonrpc": "2.0", "method": "call", "params": params or {}})
        if "application/json" not in (r.headers.get("content-type") or ""):
            if self._is_login_url(str(r.url)) or r.status_code in (200, 401, 403):
                raise NeedLogin("session expired or not logged in: %s" % r.url)
            raise RuntimeError("unexpected HTTP %s from %s (odoo.sh API changed?)" % (r.status_code, route))
        data = r.json()
        if data.get("error"):
            err = data["error"]
            edata = err.get("data") or {}
            msg = edata.get("message") or err.get("message") or str(err)
            if err.get("code") == 100 or "SessionExpired" in (edata.get("name") or "") \
                    or "AccessDenied" in (edata.get("name") or ""):
                raise NeedLogin(msg)
            raise RuntimeError(msg)
        return data.get("result")

    def _repo(self, client, project):
        """Return the accessible odoo.sh repo dict for the ``project`` name.

        ``project`` is the odoo.sh **project name** — the slug shown in the dashboard URL
        (``/project/<project_name>``) and stored as scalidev's "odoo.sh project" setting,
        i.e. the API's ``project_name`` field. That is NOT the GitHub repo ``name`` (the two
        can differ). Raises :class:`NeedLogin` if the session is gone, or :class:`RuntimeError`
        — listing the accessible project names — if none matches.
        """
        repos = (self._app(client, "/app/projects") or {}).get("repos") or []
        for repo in repos:
            if repo.get("project_name") == project:
                return repo
        available = ", ".join(sorted(r.get("project_name") for r in repos if r.get("project_name")))
        raise RuntimeError("no accessible odoo.sh project named %r (available: %s)" % (project, available))

    def _repository_id(self, client, project):
        """Return the numeric odoo.sh repository id of ``project``."""
        return self._repo(client, project)["id"]

    def _builds(self, client, branch_id):
        """Return the build list of a branch (``/app/branch/<id>/builds`` → ``builds``)."""
        if not branch_id:
            return []
        res = self._app(client, "/app/branch/%s/builds" % branch_id)
        if isinstance(res, list):
            res = res[0] if res else {}
        return (res or {}).get("builds") or []

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
        repo = self._repo(client, project)
        branches = self._app(client, "/app/project/%s/branches" % repo["technical_name"]) or []
        entry = next((b for b in branches if b.get("name") == branch), None)
        if not entry:
            raise ValueError("branch %r not found in project %r" % (branch, project))
        build_id = (entry.get("last_build_id") or [None])[0]
        builds = self._builds(client, entry.get("id"))
        worker_url = builds[0].get("worker_url") if builds else None
        return repo["id"], build_id, worker_url

    @staticmethod
    def _is_login_url(url):
        return "/web/login" in url or "github.com" in url or "/oauth/" in url

    @staticmethod
    def _download_button_url(item):
        """Return the download URL of a 'dump ready' notification, from its buttons."""
        groups = []
        if isinstance(item.get("buttons"), list):
            groups.append(item["buttons"])
        bottom_left = item.get("bottom_left") or {}
        if isinstance(bottom_left.get("buttons"), list):
            groups.append(bottom_left["buttons"])
        for buttons in groups:
            for b in buttons:
                if not isinstance(b, dict):
                    continue
                url = b.get("url") or b.get("href") or b.get("link")
                if url and (b.get("type") in (None, "download")
                            or "download" in (b.get("name") or "").lower() or "dump" in url):
                    return url
        return None

    @staticmethod
    def _branch(b):
        """Normalize a ``/app/project/<tn>/branches`` entry to the public branch shape."""
        return {"name": b.get("name"), "stage": b.get("stage"),
                "branch_id": b.get("id"), "last_build_id": b.get("last_build_id"),
                "status": b.get("last_build_status"), "result": b.get("last_build_result"),
                "slug": b.get("slug")}

    def _build(self, project, entry, builds=None):
        """Build the public 'current build' dict, deriving the SSH host from the build URL.

        ``entry`` is a normalized branch (see :meth:`_branch`); ``builds`` is its build list.
        The SSH host is ``<build_id>@<hostname of build.url>``, falling back to the
        ``last_build_id`` host slug on ``.dev.odoo.com`` when no build URL is available.
        """
        builds = builds or []
        current = builds[0] if builds else {}
        last_build_id = list(entry.get("last_build_id") or []) + [None, None]
        build_id = current.get("id") or last_build_id[0]
        slug = last_build_id[1]
        url = current.get("url")
        host = urlparse(url).hostname if url else ("%s.dev.odoo.com" % slug if slug else None)
        return {
            "project": project, "branch": entry.get("name"), "stage": entry.get("stage"),
            "build_id": build_id, "host_slug": slug,
            "status": current.get("status") or entry.get("status"),
            "result": current.get("result") or entry.get("result"),
            "ssh_host": "%s@%s" % (build_id, host) if build_id and host else None,
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
