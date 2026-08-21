# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""One-time interactive GitHub login via Playwright (optional ``login`` extra).

Opens a real browser window (Playwright's own, isolated from the user's daily
browser) so the human completes the GitHub OAuth login once, then persists the
session as a Playwright ``storage_state`` that the httpx runtime reuses.
"""

import os
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .client import NeedLogin, OdooShClient


def _is_authenticated(base_url, state_path):
    """True once the session just persisted at ``state_path`` is accepted by the /app/* API.

    Reuses :class:`OdooShClient`'s own httpx call — the same check the rest of this package
    already trusts — rather than probing from inside the browser page, which would need to
    reproduce odoo.sh's exact CSRF/CORS expectations for an in-page ``fetch``.
    """
    client = OdooShClient(state_path=state_path, base_url=base_url)
    try:
        with client._session() as http_client:
            client._app(http_client, "/app/projects")
        return True
    except NeedLogin:
        return False


def interactive_login(project, state_path, base_url="https://www.odoo.sh", browser="firefox", timeout=300,
                      interval=2):
    """Open a browser for an interactive GitHub login and save the session to ``state_path``."""
    base_url = base_url.rstrip("/")
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with sync_playwright() as p:
        launcher = getattr(p, browser)
        instance = launcher.launch(headless=False)
        context = instance.new_context()
        page = context.new_page()
        # The bare /project route is the actual auth gate — it 302-redirects an unauthenticated
        # visitor straight to GitHub's OAuth login (confirmed live: a cookie-less request to
        # /project/<x>/builds instead renders the full public dashboard with no login prompt at
        # all, since branch/build data is publicly viewable once you know the slug).
        page.goto("%s/project" % base_url)
        print("Log in to GitHub in the browser window that just opened...", file=sys.stderr)
        # Poll the actual session rather than the URL: GitHub's OAuth callback redirects back to
        # /project, but persisting storage_state on every attempt and re-checking it against the
        # real /app/* API is what actually confirms the login succeeded.
        deadline = time.monotonic() + timeout
        try:
            while True:
                context.storage_state(path=state_path)
                if _is_authenticated(base_url, state_path):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the odoo.sh login to complete.")
                page.wait_for_timeout(interval * 1000)
        except PlaywrightError as e:
            raise RuntimeError("The odoo.sh login window was closed before the login completed.") from e
        finally:
            try:
                instance.close()
            except PlaywrightError:
                pass
