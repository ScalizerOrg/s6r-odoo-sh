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

The Monitor tab is a separate, reverse-proxied Grafana instance, fetched through its own
routes instead (same cookie-based session, no additional auth):

* ``POST /_grafana/search`` → resolve a branch to its current monitoring build id;
* ``POST /_grafana/query/pad`` / ``POST /_grafana/query/pa`` → time-series metrics
  (CPU/memory/IOPS/HTTP/mail; database/container/toast size);
* ``POST /_grafana/query/global`` → point-in-time snapshot (storage/filestore/database
  breakdown, log error/warning/memory-limit counts, slowest routes) — always relative to
  *now*, regardless of any requested time range, and refreshed only every ~24h;
* ``POST /_grafana/annotations`` → deploy/build markers and retention/aggregation
  notices (same ``{build_id, start_ts, end_ts}`` params as the ``query/*`` routes).

Confirmed live against the dashboard's own JSON definition
(``GET /monitoring/api/public/dashboards/<uid>``): only these three scopes
(``pad``/``pa``/``global``) exist, and odoo.sh/Grafana itself never re-buckets
these series for a wider time range or zoom level — every panel plots the raw
points returned by the API as-is.

Runtime needs only httpx: it reuses a persisted browser session (Playwright
``storage_state``) and, if the odoo.sh session has expired but the GitHub
session is still alive, the GitHub OAuth code flow replays transparently while
following redirects. The interactive first login lives in :mod:`s6r_odoo_sh.login`
and requires the optional ``login`` extra (Playwright).
"""

import datetime
import difflib
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
# Matches a route logged with its query string, e.g. "/auth_oauth/signin?access_token=... (count=1)",
# as found in query/global's slowest_routes_* — the query string can carry live OAuth tokens/PII.
_ROUTE_QUERY_RE = re.compile(r"^([^?\s]+)\?[^\s]*(\s*\(count=\d+\))$")
# "<table> (index=291 MB,toast=47 MB)" -> table / index size+unit / toast size+unit, as found in
# query/global's "database" dict.
_DB_TABLE_KEY_RE = re.compile(r"^(?P<table>.+) \(index=(?P<index>[\d.]+) (?P<index_unit>\w+),"
                              r"toast=(?P<toast>[\d.]+) (?P<toast_unit>\w+)\)$")
# "<mimetype> (file count=186307)" -> mimetype / file count, as found in query/global's "filestore" dict.
_FILESTORE_KEY_RE = re.compile(r"^(?P<mimetype>.+) \(file count=(?P<count>\d+)\)$")
# "<route> (count=186)" -> route / request count, as found in query/global's "slowest_routes_*" dict
# (value is the max execution time in seconds, per the dashboard's own fieldConfig.unit: "s").
_ROUTE_COUNT_KEY_RE = re.compile(r"^(?P<route>.+) \(count=(?P<count>\d+)\)$")
# Stages whose branches have an active Monitor tab worth suggesting on a "branch not found" error.
_MONITORABLE_STAGES = ("production", "staging")
# Grafana's declared unit for every storage/database/filestore panel (fieldConfig.unit: "kbytes")
# — confirmed from the dashboard's own JSON definition, not a guess.
_KB_TO_MB = 1024.0
_UNIT_TO_MB = {"KB": 1 / 1024.0, "MB": 1.0, "GB": 1024.0}
# Color of the "data before this period has been aggregated" system notices mixed into
# /_grafana/annotations — distinguishes them from real deploy markers (confirmed live).
RETENTION_NOTICE_COLOR = "#2596be"


def _to_epoch_ms(value):
    """Return ``value`` as an epoch-milliseconds int (accepts a ``datetime`` or a number)."""
    if isinstance(value, datetime.datetime):
        return int(value.timestamp() * 1000)
    return int(value)


def _to_iso_utc(epoch_ms):
    """Return an epoch-milliseconds timestamp as an ISO-8601 UTC string."""
    return datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.timezone.utc).isoformat()


def _redact_routes(routes):
    """Strip query strings from ``slowest_routes_*`` keys (may carry live tokens/PII)."""
    return {_ROUTE_QUERY_RE.sub(r"\1\2", route): v for route, v in (routes or {}).items()}


def _kb_to_mb(value_kb):
    """Convert a raw kilobyte value (as returned by odoo.sh) to megabytes, rounded."""
    return round(value_kb / _KB_TO_MB, 2)


def _split_database_key(key, size_kb):
    """Parse a ``query/global`` "database" key into ``{table, size_mb, index_mb, toast_mb}``.

    odoo.sh formats the key as ``"<table> (index=X <unit>,toast=Y <unit>)"``, with the unit
    varying per row (KB/MB/GB) — confirmed live (e.g. ``toast=224 KB`` next to ``toast=7 GB``).
    Falls back to a bare ``{table, size_mb}`` for rows that don't match (e.g. "Other Tables").
    """
    m = _DB_TABLE_KEY_RE.match(key)
    if not m:
        return {"table": key, "size_mb": _kb_to_mb(size_kb)}
    index_mb = float(m.group("index")) * _UNIT_TO_MB[m.group("index_unit")]
    toast_mb = float(m.group("toast")) * _UNIT_TO_MB[m.group("toast_unit")]
    return {"table": m.group("table"), "size_mb": _kb_to_mb(size_kb),
            "index_mb": round(index_mb, 2), "toast_mb": round(toast_mb, 2)}


def _split_filestore_key(key, size_kb):
    """Parse a ``query/global`` "filestore" key into ``{mimetype, file_count, size_mb}``."""
    m = _FILESTORE_KEY_RE.match(key)
    if not m:
        return {"mimetype": key, "size_mb": _kb_to_mb(size_kb)}
    return {"mimetype": m.group("mimetype"), "file_count": int(m.group("count")),
            "size_mb": _kb_to_mb(size_kb)}


def _top_n_filestore(entries, n=10):
    """Keep the ``n`` largest filestore entries (by size) and fold the rest into "other"."""
    ordered = sorted(entries, key=lambda e: e["size_mb"], reverse=True)
    if len(ordered) <= n:
        return ordered
    head, tail = ordered[:n], ordered[n:]
    head.append({"mimetype": "other (%d types)" % len(tail), "file_count": sum(e["file_count"] for e in tail),
                "size_mb": round(sum(e["size_mb"] for e in tail), 2)})
    return head


def _split_route_key(key, max_seconds):
    """Parse a ``query/global`` "slowest_routes_*" key into ``{route, count, max_s}``."""
    m = _ROUTE_COUNT_KEY_RE.match(key)
    if not m:
        return {"route": key, "max_s": max_seconds}
    return {"route": m.group("route"), "count": int(m.group("count")), "max_s": max_seconds}


class BranchMonitoringError(ValueError):
    """Base for the branch-validation errors raised by ``_resolve_monitoring_build_id_checked``."""


class BranchNotFoundError(BranchMonitoringError):
    """The branch doesn't exist in the project at all."""


class MonitorUnavailableError(BranchMonitoringError):
    """The branch exists but has no monitoring build (e.g. most dev branches)."""


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

    def get_monitoring_metrics(self, project, branch, start, end, build_id=None, scopes=("pad", "pa")):
        """Return the Monitor tab's time-series metrics for ``branch`` over ``[start, end]``.

        ``start``/``end`` accept a :class:`datetime.datetime` or an epoch-milliseconds number.
        Behind odoo.sh's Monitor tab is a reverse-proxied Grafana instance (``/_grafana/*``) fed
        by two time-series "scopes": ``pad`` (~5 min buckets — CPU/memory/IOPS/HTTP/mail) and
        ``pa`` (coarser buckets, ~3h — database/container/toast disk size; the dashboard's own
        panel description claims "every 1h", but the real cadence observed live is ~3h — trust
        the data, not the label). Every numeric ``*_sum`` field is a **sum over the bucket**, not
        an average — divide by the bucket's ``duration_sum`` (in **seconds**) to get a plottable
        average, e.g. ``mem_user_sum / duration_sum`` MB. The bucket width itself **adapts to the
        requested range** (confirmed live: ~300s over a 1-day window, ~1800s over the dashboard's
        default 2-day window) — don't assume a fixed value. odoo.sh/Grafana itself never
        re-buckets these series for a wider range or zoom level beyond that server-side width
        choice (confirmed from the dashboard's own JSON definition) — any further re-aggregation
        (e.g. scalidev's ``resolution`` parameter) is client-side only.

        The route itself **ignores the requested window**: confirmed live (CLI and a raw
        ``fetch()`` from the Monitor tab page itself) that ``pad`` data starts exactly 24h
        before ``start_ts`` and continues past ``end_ts`` all the way to "now", regardless of
        how far in the past ``end_ts`` is. This method filters the response to the requested
        ``[start, end)`` window before returning it, and flags the last ``pad`` row as
        ``"partial": true`` when its ``duration_sum`` is below the response's own nominal bucket
        width (can happen at the edge of "now", or past a retention/aggregation boundary — see
        :meth:`get_monitoring_annotations`).

        Returns ``{"build_id": int, "pad": [...], "pa": [...]}`` (only the requested ``scopes``
        are present), each series a list of per-bucket dicts as returned by Grafana, ready to
        feed a chart with the exact same data as the Memory/CPU/IOPS/HTTP/Mails/Storage panels.
        """
        start_ts, end_ts = _to_epoch_ms(start), _to_epoch_ms(end)
        with self._session() as client:
            if build_id is None:
                build_id = self._resolve_monitoring_build_id_checked(client, project, branch)
            params = {"build_id": build_id, "start_ts": start_ts, "end_ts": end_ts}
            result = {"build_id": build_id}
            for scope in scopes:
                rows = (self._app(client, "/_grafana/query/%s" % scope, params) or {}).get("data") or []
                # odoo.sh returns "time" as a float even though it's always a whole millisecond
                # count — normalize to int (pad already comes back as int, pa doesn't).
                rows = [dict(row, time=int(row["time"])) for row in rows if start_ts <= row["time"] < end_ts]
                durations = [row["duration_sum"] for row in rows if "duration_sum" in row]
                if durations:
                    # Bucket width adapts to the requested range (confirmed live: ~300s over a
                    # 1-day window, ~1800s over the dashboard's default 2-day window) — derive the
                    # nominal width from the response itself rather than assuming a fixed value.
                    nominal = max(set(durations), key=durations.count)
                    if rows[-1]["duration_sum"] < nominal:
                        rows[-1] = dict(rows[-1], partial=True)
                result[scope] = rows
            self._save_cookies(client)
            return result

    def get_monitoring_snapshot(self, project, branch, build_id=None, redact=True):
        """Return the Monitor tab's point-in-time snapshot for ``branch``.

        Covers the Storage/Filestore/Database breakdown pie charts and the "odoo.log
        statistics" row (error/warning/memory-limit/serialization-failure counts and the
        slowest routes), by calling the same ``/_grafana/query/global`` route as the dashboard.

        Unlike :meth:`get_monitoring_metrics`, **this ignores any notion of a selected time
        range** — confirmed live by replaying the same query with different end timestamps and
        getting an identical response: the panels are literally titled "Last 24h"/"Last 7
        days" in Grafana and always mean exactly that, relative to *now*, not to a chosen
        period. There is no way to get an equivalent snapshot for an arbitrary past day through
        this endpoint; only :meth:`get_monitoring_metrics` (``pad``/``pa``) supports arbitrary
        historical ranges. The dashboard's own panel descriptions say this data "is updated
        every 24h for the production DB" — so it can lag reality by up to a day despite being
        "now"-relative.

        Returns ``{"database": [...], "storage": {...}, "filestore": [...], "log_counter_daily":
        {...}, "log_counter_weekly": {...}, "slowest_routes_daily": [...],
        "slowest_routes_weekly": [...], "timestamp": str}``. odoo.sh's own ``error_messages``
        key is dropped — confirmed always empty ``{}`` across every branch/build tested.
        ``storage`` sizes (``filestore_size``/``database_size``/...) are converted from odoo.sh's
        raw kilobytes (confirmed from the dashboard's own ``fieldConfig.unit: "kbytes"``) to
        megabytes, in ``*_mb``-suffixed keys. ``database``/``slowest_routes_*`` are lists of
        ``{table, size_mb, index_mb, toast_mb}``/``{route, count, max_s}`` dicts (parsed out of
        odoo.sh's mixed text/number keys) instead of its raw ``"<table> (index=X MB,toast=Y
        MB)": size`` mapping. ``filestore`` is the same kind of list, kept to its 10 largest
        mimetypes plus one ``"other (N types)"`` entry summing the rest (odoo.sh returns ~35
        mimetypes, most negligible). ``timestamp`` is ISO-8601 UTC.

        ``slowest_routes_daily``/``slowest_routes_weekly`` route names can carry a live request's
        full query string (e.g. ``/auth_oauth/signin?access_token=...``), which has been observed
        to include real OAuth tokens and end-user identities. ``redact=True`` (default) strips
        the query string from every route name before returning. These lists also carry a
        spurious Grafana header row (``"Route": "Max execution time (Execution count)"``),
        dropped here.
        """
        with self._session() as client:
            if build_id is None:
                build_id = self._resolve_monitoring_build_id_checked(client, project, branch)
            # start_ts/end_ts are required by the route but ignored server-side (see docstring);
            # any value works, so a fixed recent window is passed for clarity.
            now_ms = int(time.time() * 1000)
            params = {"build_id": build_id, "start_ts": now_ms - 7 * 86400000, "end_ts": now_ms}
            result = (self._app(client, "/_grafana/query/global", params) or [{}])[0]
            self._save_cookies(client)

        for key in ("slowest_routes_daily", "slowest_routes_weekly"):
            routes = dict(result.get(key) or {})
            routes.pop("Route", None)
            if redact:
                routes = _redact_routes(routes)
            result[key] = [_split_route_key(k, v) for k, v in routes.items()]

        storage = dict(result.get("storage") or {})
        for key in ("filestore_size", "database_size", "logs_size", "remaining_size",
                    "repositories_size", "sessions_size"):
            if key in storage:
                storage[key.replace("_size", "_mb")] = _kb_to_mb(storage.pop(key))
        result["storage"] = storage

        result["database"] = [_split_database_key(k, v) for k, v in (result.get("database") or {}).items()]
        filestore = [_split_filestore_key(k, v) for k, v in (result.get("filestore") or {}).items()]
        result["filestore"] = _top_n_filestore(filestore)
        result.pop("error_messages", None)

        if "timestamp" in result:
            result["timestamp"] = _to_iso_utc(result["timestamp"])
        return result

    def get_monitoring_annotations(self, project, branch, start, end, build_id=None):
        """Return the Monitor tab's deploy/build annotations for ``branch`` over ``[start, end]``.

        These are the vertical markers Grafana overlays on the time-series panels, fetched via
        ``POST /_grafana/annotations`` with the same ``{build_id, start_ts, end_ts}`` params as
        :meth:`get_monitoring_metrics` (confirmed live from the Monitor tab's own network
        traffic — the generic Grafana public-dashboard annotations endpoint is a red herring
        that never returns odoo.sh deploy markers). ``start``/``end`` accept a
        :class:`datetime.datetime` or an epoch-milliseconds number.

        Like :meth:`get_monitoring_metrics`, the route **ignores the requested window** —
        confirmed live: a 2h window still returns retention notices from weeks earlier. This
        method filters the response to ``[start, end)`` before returning it.

        Returns a list of ``{"time": str (ISO UTC), "text": str, "color": str}``, **deploys first
        (most recent to oldest), then retention notices (most recent to oldest)** — odoo.sh
        itself returns them in no particular order. ``text`` contains raw HTML (``<a>``/``<li>``
        links to the GitHub commit/PR). Two kinds of annotations are mixed together: real deploys
        (commit/PR info) and, with ``color == RETENTION_NOTICE_COLOR`` (module constant,
        ``"#2596be"``), system notices that the data before that point in time has been
        aggregated to a coarser resolution — not a deploy, treat it as a retention boundary
        instead.
        """
        start_ts, end_ts = _to_epoch_ms(start), _to_epoch_ms(end)
        with self._session() as client:
            if build_id is None:
                build_id = self._resolve_monitoring_build_id_checked(client, project, branch)
            params = {"build_id": build_id, "start_ts": start_ts, "end_ts": end_ts}
            result = self._app(client, "/_grafana/annotations", params) or {}
            self._save_cookies(client)
        annotations = [dict(a, time=_to_iso_utc(a["time"])) for a in (result.get("annotations") or [])
                       if start_ts <= a["time"] < end_ts]
        deploys = sorted((a for a in annotations if a["color"] != RETENTION_NOTICE_COLOR),
                         key=lambda a: a["time"], reverse=True)
        notices = sorted((a for a in annotations if a["color"] == RETENTION_NOTICE_COLOR),
                         key=lambda a: a["time"], reverse=True)
        return deploys + notices

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
        download link odoo.sh published. Notifications (dump-ready and otherwise, both seen
        and unseen — odoo.sh keeps them all) come from ``/app/project/<project>/get_info``'s
        ``notifications[<repo_id>].items``. ``/app/projects``' own ``notification_counts``
        (used by :meth:`_repo`) only ever carries accurate *counts* — its ``items`` list is
        always empty — so it cannot be used to resolve an actual download URL.
        """
        with self._session() as client:
            info = self._app(client, "/app/project/%s/get_info" % project) or {}
            repo_id = (info.get("active_repo") or {}).get("id")
            items = ((info.get("notifications") or {}).get(str(repo_id)) or {}).get("items") or []
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

    def _resolve_monitoring_build_id(self, client, project, branch, build=None):
        """Return the build id the Monitor tab resolves ``project``/``branch``/``build`` to.

        Mirrors the dashboard's own ``/_grafana/search`` call. ``build=None`` (default)
        resolves to the branch's *current* build — passing an explicit build number/commit is
        not supported by this route (odoo.sh answers with a generic server error for anything
        but the current build), so historical ranges are queried with the current build id too
        (the metrics themselves remain available further back than that build's own start time).
        """
        res = self._app(client, "/_grafana/search", {"project": project, "branch": branch, "build": build})
        if not res:
            raise RuntimeError("could not resolve a monitoring build id for %r/%r" % (project, branch))
        return res[0]

    def _resolve_monitoring_build_id_checked(self, client, project, branch, build=None):
        """Like :meth:`_resolve_monitoring_build_id`, but replaces odoo.sh's opaque ``SH-xxxxx``
        server error with a :class:`BranchNotFoundError`/:class:`MonitorUnavailableError` when
        the branch doesn't exist or has no monitoring build (e.g. a dev branch) — confirmed live
        on both cases. Fetches the branch list itself (rather than :meth:`get_branch`, whose
        "branch not found" payload only carries bare names) so the error can suggest the closest
        matches and the monitorable (production/staging) branches, instead of dumping every
        branch name — a project can have hundreds of dev branches (confirmed live: ~600 on one
        project), which blew past 8000 characters in a flat list.
        """
        try:
            return self._resolve_monitoring_build_id(client, project, branch, build=build)
        except RuntimeError:
            repo = self._repo(client, project)
            branches = self._app(client, "/app/project/%s/branches" % repo["technical_name"]) or []
            entry = next((b for b in branches if b.get("name") == branch), None)
            if entry is None:
                names = sorted(b.get("name") for b in branches if b.get("name"))
                closest = difflib.get_close_matches(branch, names, n=5)
                monitorable = sorted(b.get("name") for b in branches if b.get("stage") in _MONITORABLE_STAGES)
                raise BranchNotFoundError(
                    "branch %r not found in project %r (%d branches total; closest matches: %s; "
                    "monitorable production/staging branches: %s)" % (
                        branch, project, len(names), ", ".join(closest) or "none",
                        ", ".join(monitorable) or "none")) from None
            if entry.get("stage") == "dev":
                raise MonitorUnavailableError(
                    "Monitor unavailable for dev branch %r (no monitoring build)" % branch) from None
            raise

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
        """Return the build list of a branch (``/app/branch/<id>/builds`` → ``builds``),
        newest first — a branch built against multiple Odoo versions in parallel can have
        several builds in flight at once, so callers picking ``builds[0]`` as "the current
        build" must not rely on the API's own ordering.
        """
        if not branch_id:
            return []
        res = self._app(client, "/app/branch/%s/builds" % branch_id)
        if isinstance(res, list):
            res = res[0] if res else {}
        builds = (res or {}).get("builds") or []
        return sorted(builds, key=lambda b: b.get("id") or 0, reverse=True)

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
        # worker_url must come from the SAME build as build_id — builds[0] can be a
        # higher-id but no-longer-live build (status "dropped"/"killed"), which would
        # otherwise pair the right build_id with the wrong (dead) worker.
        match = next((b for b in builds if b.get("id") == build_id), None)
        worker_url = (match or (builds[0] if builds else {})).get("worker_url")
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
        last_build_id = list(entry.get("last_build_id") or []) + [None, None]
        # entry's own last_build_id is odoo.sh's authoritative "current build" for this
        # branch — a branch can have several builds in its build list that are no longer
        # live (status "dropped"/"killed") yet still sort ahead by id, so builds[0] is only
        # a fallback, never trusted over an actual last_build_id match.
        current = next((b for b in builds if b.get("id") == last_build_id[0]), None) \
            or (builds[0] if builds else {})
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
