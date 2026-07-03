# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""s6r-odoo-sh: resolve the current build (number + SSH host) of an odoo.sh branch."""

from .client import DEFAULT_BASE_URL, NeedLogin, OdooShClient, default_download_dir, default_state_path

__version__ = "0.2.1"


def resolve(project, branch, auto_login=True, **kwargs):
    """One-shot convenience: return the current build of ``branch`` for ``project``.

    Extra keyword arguments are forwarded to :class:`~s6r_odoo_sh.client.OdooShClient`
    (``state_path``, ``base_url``, ``login_browser``, ``login_timeout``).
    """
    return OdooShClient(**kwargs).get_branch(project, branch, auto_login=auto_login)


__all__ = ["OdooShClient", "NeedLogin", "resolve", "default_state_path", "default_download_dir",
           "DEFAULT_BASE_URL", "__version__"]
