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
    parser.add_argument("--build", type=int,
                        help="target a specific build id (instead of the branch's current build)")
    parser.add_argument("--state", default=os.environ.get("ODOO_SH_STATE"),
                        help="path to the persisted session (or env ODOO_SH_STATE)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="odoo.sh base URL")
    parser.add_argument("--browser", default="firefox", help="Playwright browser for login (firefox/chromium)")
    parser.add_argument("--login", action="store_true", help="force an interactive login first")
    parser.add_argument("--list", action="store_true", help="list all branches instead of one build")
    parser.add_argument("--ssh-host", action="store_true", help="print only the SSH host of the build")
    parser.add_argument("--backups", action="store_true", help="list the repository backups")
    parser.add_argument("--create-backup", action="store_true", help="create a backup of the branch's current build")
    parser.add_argument("--comment", default="", help="comment for --create-backup")
    parser.add_argument("--dump-notifs", action="store_true", help="list the ready 'database dump' notifications")
    parser.add_argument("--create-dump", nargs="?", const="", default=None, metavar="PATH",
                        help="trigger a dump of the branch, wait for it, and download it "
                             "(to PATH/dir, or the Downloads folder when PATH is omitted)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for the dump (--create-dump)")
    parser.add_argument("--download-dump", nargs="?", const="", default=None, metavar="PATH",
                        help="download an already-prepared dump from notifications (no new dump); "
                             "to PATH/dir or Downloads, use --backup-datetime to pick which")
    parser.add_argument("--backup-datetime", help="select the ready dump by its backup_datetime_utc")
    parser.add_argument("--filestore", action="store_true", help="include the filestore in the dump")
    parser.add_argument("--prod", action="store_true", help="production dump instead of a neutralized test dump")
    parser.add_argument("--build-status", action="store_true", help="print the branch's current build status")
    parser.add_argument("--wait-build", action="store_true",
                        help="wait for a build to start then finish (build + unit tests), and print its result")
    parser.add_argument("--commit", help="match the build by commit SHA (with --wait-build)")
    parser.add_argument("--after-build", type=int, help="wait for a build newer than this id (with --wait-build)")
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

    if args.backups:
        print(json.dumps(client.list_backups(args.project), indent=2, default=str, ensure_ascii=False))
        return 0

    if args.dump_notifs:
        print(json.dumps(client.dump_notifications(args.project), indent=2, default=str, ensure_ascii=False))
        return 0

    if args.download_dump is not None:
        res = client.download_ready_dump(args.project, dest=(args.download_dump or None),
                                         backup_datetime_utc=args.backup_datetime, build_id=args.build)
        print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
        return 0

    test_dump = not args.prod

    if args.create_backup:
        if not (args.branch or args.build):
            parser.error("--create-backup needs a branch or --build")
        print(json.dumps(client.create_backup(args.project, args.branch, comment=args.comment,
                                               build_id=args.build), indent=2, default=str, ensure_ascii=False))
        return 0

    if args.create_dump is not None:
        if not (args.branch or args.build):
            parser.error("--create-dump needs a branch or --build")
        result = client.create_dump(args.project, args.branch, dest=(args.create_dump or None),
                                    test_dump=test_dump, filestore=args.filestore,
                                    build_id=args.build, timeout=args.timeout)
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0

    if not args.branch:
        parser.error("a branch is required (or use --list / --backups / --dump-notifs)")

    if args.build_status:
        print(json.dumps(client.build_status(args.project, args.branch), indent=2, default=str, ensure_ascii=False))
        return 0

    if args.wait_build:
        def on_start(b):
            print("build %s started (%s)" % (b.get("build_id"), b.get("status")), file=sys.stderr)

        res = client.wait_for_build(args.project, args.branch, after_build_id=args.after_build,
                                    commit=args.commit, timeout=args.timeout, on_start=on_start)
        print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
        return 0

    if args.ssh_host:
        print(client.get_ssh_host(args.project, args.branch, auto_login=True) or "")
    else:
        print(json.dumps(client.get_branch(args.project, args.branch, auto_login=True),
                         indent=2, default=str, ensure_ascii=False))
    return 0
