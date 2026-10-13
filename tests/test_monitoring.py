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

from s6r_odoo_sh.client import OdooShClient


@contextlib.contextmanager
def _noop_session():
    yield None


def _client(app_responses, branch_info=None):
    """A client whose ``_app``/``_session``/``get_branch`` are faked.

    ``app_responses``: {route: response} returned by ``_app`` regardless of params
    (routes used here never need per-call variation). ``branch_info``: the dict
    ``get_branch`` returns (defaults to a found production branch).
    """
    client = OdooShClient(state_path="/nonexistent")
    client._session = _noop_session
    client._app = lambda client, route, params=None: app_responses[route]
    client._save_cookies = lambda client: None
    client.get_branch = lambda project, branch, auto_login=False: branch_info or {"stage": "production"}
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


def _raise_sh_error(client, route, params=None):
    raise RuntimeError("SH-xxxxx")


def test_resolve_monitoring_build_id_checked_branch_not_found():
    client = _client({"/_grafana/search": []}, branch_info={"error": "branch not found", "available": ["main"]})
    client._app = _raise_sh_error
    with pytest.raises(ValueError, match="not found"):
        with client._session() as c:
            client._resolve_monitoring_build_id_checked(c, "proj", "nope")


def test_resolve_monitoring_build_id_checked_dev_branch():
    client = _client({}, branch_info={"stage": "dev"})
    client._app = _raise_sh_error
    with pytest.raises(ValueError, match="dev branch"):
        with client._session() as c:
            client._resolve_monitoring_build_id_checked(c, "proj", "some_dev_branch")
