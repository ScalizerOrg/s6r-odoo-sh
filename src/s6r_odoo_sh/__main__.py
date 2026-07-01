# Copyright (C) 2026 - Scalizer (<https://www.scalizer.fr>).
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).
"""Allow ``python -m s6r_odoo_sh``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
