"""Permite ejecutar el cliente visual como `python -m app.tui`."""

from __future__ import annotations

import sys

from app.tui.cli import main

if __name__ == "__main__":
    sys.exit(main())
