"""Punto de entrada del servidor, para poder ejecutarlo con `python -m app.server`.

Toda la lógica está en `cli.main`. Acá solo se traduce lo que devuelve en el código de
salida del proceso, que es lo que mira el shell.
"""

from __future__ import annotations

import sys

from app.server.main.cli import main

if __name__ == "__main__":
    sys.exit(main())
