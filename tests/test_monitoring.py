# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""Tests for the Monitor-tab (Grafana proxy) methods of OdooShClient.

odoo.sh's ``/_grafana/*`` routes ignore the requested time window (confirmed live:
see client.py's docstrings) and return a generic server error for any invalid
branch — these tests fake the transport (``_session``/``_app``) to exercise the
client-side filtering and error-mapping logic without a real network call.
"""

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from s6r_odoo_sh.client import (BranchNotFoundError, MonitorUnavailableError, OdooShClient,
                                _split_database_key, _split_route_key, _top_n_filestore)


@contextlib.contextmanager
def _noop_session():
    yield None


def _client(app_responses):
    """A client whose ``_app``/``_session``/``_repo`` are faked.

    ``app_responses``: {route: response} returned by ``_app`` regardless of params (routes used
    here never need per-call variation).
    """
    client = OdooShClient(state_path="/nonexistent")
    client._session = _noop_session
    client._app = lambda client, route, params=None: app_responses[route]
    client._save_cookies = lambda client: None
    client._repo = lambda client, project: {"technical_name": "repo"}
    return client


# odoo.sh over-returns at least 24h before start_ts and up to "now" after end_ts
# (confirmed live) — the fake response below reproduces that shape.
_PAD_ROWS = [
    {"time": 1_000_000_000, "duration_sum": 300, "mem_user_sum": 1},  # before the window
    {"time": 1_000_300_000, "duration_sum": 300, "mem_user_sum": 2},  # in window
    {"time": 1_000_600_000, "duration_sum": 300, "mem_user_sum": 3},  # in window
    {"time": 1_000_900_000, "duration_sum": 120, "mem_user_sum": 4},  # in window, partial
    {"time": 1_001_200_000, "duration_sum": 300, "mem_user_sum": 5},  # after the window
]


def test_get_monitoring_metrics_filters_window_and_flags_partial():
    client = _client({
        "/_grafana/search": [42],
        "/_grafana/query/pad": {"data": _PAD_ROWS},
    })
    result = client.get_monitoring_metrics("proj", "branch", 1_000_300_000, 1_001_000_000, scopes=("pad",))
    pad = result["pad"]
    assert [row["time"] for row in pad] == [1_000_300_000, 1_000_600_000, 1_000_900_000]
    assert pad[0].get("partial") is None
    assert pad[1].get("partial") is None
    assert pad[2]["partial"] is True  # duration_sum (120) < nominal (300)


def test_get_monitoring_annotations_filters_window():
    client = _client({
        "/_grafana/search": [42],
        "/_grafana/annotations": {"annotations": [
            {"time": 1_000_000_000, "text": "too early", "color": "#E02F44"},
            {"time": 1_000_500_000, "text": "in window", "color": "#E02F44"},
            {"time": 2_000_000_000, "text": "too late", "color": "#2596be"},
        ]},
    })
    result = client.get_monitoring_annotations("proj", "branch", 1_000_300_000, 1_001_000_000)
    assert [a["text"] for a in result] == ["in window"]


def test_get_monitoring_annotations_sorts_deploys_then_notices_newest_first():
    client = _client({
        "/_grafana/search": [42],
        "/_grafana/annotations": {"annotations": [
            {"time": 1_000_000_000, "text": "old notice", "color": "#2596be"},
            {"time": 1_000_900_000, "text": "new deploy", "color": "#E02F44"},
            {"time": 1_000_100_000, "text": "old deploy", "color": "#E02F44"},
            {"time": 1_000_800_000, "text": "new notice", "color": "#2596be"},
        ]},
    })
    result = client.get_monitoring_annotations("proj", "branch", 0, 2_000_000_000)
    assert [a["text"] for a in result] == ["new deploy", "old deploy", "new notice", "old notice"]


def _fail_search_only(app_responses):
    """``_app`` that raises odoo.sh's opaque error for ``/_grafana/search`` but serves every
    other route (here, the branches lookup) from ``app_responses`` normally."""
    def _app(client, route, params=None):
        if route == "/_grafana/search":
            raise RuntimeError("SH-xxxxx")
        return app_responses[route]
    return _app


def test_resolve_monitoring_build_id_checked_branch_not_found():
    branches = [{"name": "main", "stage": "production"}, {"name": "maint", "stage": "dev"}]
    client = _client({"/app/project/repo/branches": branches})
    client._app = _fail_search_only({"/app/project/repo/branches": branches})
    with pytest.raises(BranchNotFoundError, match="not found") as excinfo:
        with client._session() as c:
            client._resolve_monitoring_build_id_checked(c, "proj", "nope")
    message = str(excinfo.value)
    assert "2 branches total" in message
    assert "monitorable production/staging branches: main" in message


def test_resolve_monitoring_build_id_checked_dev_branch():
    branches = [{"name": "some_dev_branch", "stage": "dev"}]
    client = _client({"/app/project/repo/branches": branches})
    client._app = _fail_search_only({"/app/project/repo/branches": branches})
    with pytest.raises(MonitorUnavailableError, match="dev branch"):
        with client._session() as c:
            client._resolve_monitoring_build_id_checked(c, "proj", "some_dev_branch")


def test_split_database_key_handles_mixed_units():
    # The unit between parens varies per row (KB/MB/GB) — must not assume "MB" everywhere.
    parsed = _split_database_key("some_table (index=291 MB,toast=224 KB)", 1024 * 10)
    assert parsed == {"table": "some_table", "size_mb": 10.0, "index_mb": 291.0, "toast_mb": 0.22}


def test_split_route_key_separates_route_and_count():
    parsed = _split_route_key("/web/dataset/call_button (count=1576)", 145.3)
    assert parsed == {"route": "/web/dataset/call_button", "count": 1576, "max_s": 145.3}


def test_top_n_filestore_folds_the_tail_into_other():
    entries = [{"mimetype": "m%d" % i, "file_count": 1, "size_mb": 20 - i} for i in range(12)]
    result = _top_n_filestore(entries, n=10)
    assert len(result) == 11
    assert result[-1]["mimetype"] == "other (2 types)"
    assert result[-1]["file_count"] == 2
    assert result[-1]["size_mb"] == 19  # sizes 10 + 9 (the two smallest, folded)
