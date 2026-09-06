"""Argumentos del cliente visual: solo lo que no se puede cambiar sin reiniciar.

La identidad y la conexión van acá porque la sesión se abre una vez, al arrancar, y dura
toda la ejecución. Lo demás —qué imagen, qué operación, con qué parámetros— se elige en la
pantalla, que es para lo que existe.

Los validadores se reutilizan del cliente de terminal: un puerto inválido tiene que
rechazarse igual en las dos interfaces.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.client.cli import port_number
from app.common import config

EXIT_OK = 0


def build_parser() -> argparse.ArgumentParser:
    """Arma el analizador de argumentos del cliente visual.

    Returns:
        El analizador, con la identidad, la conexión y la raíz del árbol de archivos.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.tui",
        description="Cliente visual del servicio de anonimización de imágenes.",
    )
    parser.add_argument("-u", "--user", required=True, help="con qué nombre te presentás")
    parser.add_argument(
        "--host", default=config.DEFAULT_HOST,
        help=f"dirección del servidor (por defecto: {config.DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port", type=port_number, default=config.DEFAULT_PORT,
        help=f"puerto del servidor (por defecto: {config.DEFAULT_PORT})",
    )
    parser.add_argument(
        "--dir", type=Path, default=Path.cwd(), dest="directory",
        help="dónde arranca el árbol de imágenes (por defecto: el directorio actual)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path.cwd(), dest="downloads",
        help="dónde guardar los resultados descargados (por defecto: el directorio actual)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del cliente visual.

    Args:
        argv: Argumentos de la línea de comandos. Se pasan explícitos solo desde las
            pruebas.

    Returns:
        El código de salida del proceso.
    """
    arguments = build_parser().parse_args(argv)

    # La aplicación se importa después de analizar los argumentos, y no arriba del módulo,
    # para que `--help` y un argumento inválido respondan sin cargar Textual entero.
    from app.tui.application import ImagesApp

    ImagesApp(
        user=arguments.user,
        host=arguments.host,
        port=arguments.port,
        directory=arguments.directory,
        downloads=arguments.downloads,
    ).run()

    return EXIT_OK
