# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""Command-line interface for s6r-odoo-sh."""

import argparse
import json
import os
import subprocess
import sys

from .client import DEFAULT_BASE_URL, OdooShClient


def main(argv=None):
    """Entry point for the ``s6r-odoo-sh`` console script."""
    parser = argparse.ArgumentParser(prog="s6r-odoo-sh",
                                     description="Resolve the current odoo.sh build of a branch.")
    parser.add_argument("branch", nargs="?", help="branch name (e.g. migration_19_v1)")
    parser.add_argument("-p", "--project", default=os.environ.get("ODOO_SH_PROJECT"),
                        help="odoo.sh project name (or env ODOO_SH_PROJECT)")
    parser.add_argument("--state", default=os.environ.get("ODOO_SH_STATE"),
                        help="path to the persisted session (or env ODOO_SH_STATE)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="odoo.sh base URL")
    parser.add_argument("--browser", default="firefox", help="Playwright browser for login (firefox/chromium)")
    parser.add_argument("--login", action="store_true", help="force an interactive login first")
    parser.add_argument("--list", action="store_true", help="list all branches instead of one build")
    parser.add_argument("--ssh-host", action="store_true", help="print only the SSH host of the build")
    parser.add_argument("--install-browser", action="store_true",
                        help="download the Playwright browser needed for login, then exit")
    args = parser.parse_args(argv)

    if args.install_browser:
        return subprocess.call([sys.executable, "-m", "playwright", "install", args.browser])

    client = OdooShClient(state_path=args.state, base_url=args.base_url, login_browser=args.browser)

    if args.login:
        if not args.project:
            parser.error("--login needs -p/--project")
        client.login(args.project)

    if not args.project:
        parser.error("no project given (use -p/--project or set ODOO_SH_PROJECT)")

    if args.list:
        print(json.dumps(client.list_branches(args.project), indent=2, default=str, ensure_ascii=False))
        return 0

    if not args.branch:
        parser.error("a branch is required (or use --list)")

    if args.ssh_host:
        print(client.get_ssh_host(args.project, args.branch, auto_login=True) or "")
    else:
        print(json.dumps(client.get_branch(args.project, args.branch, auto_login=True),
                         indent=2, default=str, ensure_ascii=False))
    return 0
