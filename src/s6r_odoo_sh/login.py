# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""One-time interactive GitHub login via Playwright (optional ``login`` extra).

Opens a real browser window (Playwright's own, isolated from the user's daily
browser) so the human completes the GitHub OAuth login once, then persists the
session as a Playwright ``storage_state`` that the httpx runtime reuses.
"""

import os
import sys

from playwright.sync_api import sync_playwright


def interactive_login(project, state_path, base_url="https://www.odoo.sh", browser="firefox", timeout=300):
    """Open a browser for an interactive GitHub login and save the session to ``state_path``."""
    base_url = base_url.rstrip("/")
    with sync_playwright() as p:
        launcher = getattr(p, browser)
        instance = launcher.launch(headless=False)
        context = instance.new_context()
        page = context.new_page()
        page.goto("%s/project/%s/builds" % (base_url, project))
        print("Log in to GitHub in the browser window that just opened...", file=sys.stderr)
        page.wait_for_url("%s/project/**" % base_url, timeout=timeout * 1000)
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        context.storage_state(path=state_path)
        instance.close()
