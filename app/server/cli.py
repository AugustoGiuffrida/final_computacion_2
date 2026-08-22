"""Línea de comandos del servidor: sus argumentos, su registro y su apagado.

Separa el "cómo se lanza" del "qué hace", que vive en `server.py`. Acá está todo lo que
tiene que ver con el proceso en sí: sus argumentos, su registro de actividad y su
terminación ordenada.

Se ejecuta con `python -m app.server`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from app.common import config
from app.server.server import ImageServer

EXIT_OK = 0 #Terminó de forma ordenada.
EXIT_FAILURE = 1 #No pudo arrancar: el puerto está ocupado, la dirección no existe.

USAGE_EXAMPLES = """Ejemplos:
  %(prog)s
      escucha en el puerto por defecto, en todas las interfaces (IPv4 e IPv6)

  %(prog)s --port 9100
      escucha en otro puerto

  %(prog)s --host 127.0.0.1
      solo acepta conexiones de esta misma máquina

  %(prog)s --verbose
      además del registro normal, muestra el detalle de cada mensaje"""


def build_parser() -> argparse.ArgumentParser:
    """Arma el parser de la línea de comandos del servidor.

    Returns:
        El parser configurado, listo para `parse_args`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.server",
        description="Servidor del servicio de anonimización y sanitización de imágenes.",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--host",
        default=config.LISTEN_ON_ALL_INTERFACES,
        help=(
            "dirección en la que escuchar; si se omite, escucha en todas las interfaces "
            "disponibles, IPv4 e IPv6 a la vez"
        ),
    )
    parser.add_argument(
        "--port", 
        type=port_number, 
        default=config.DEFAULT_PORT,
        help=f"puerto en el que escuchar (por defecto: {config.DEFAULT_PORT})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="mostrar el detalle de cada mensaje además del registro normal",
    )

    return parser


def port_number(raw_value: str) -> int:
    """Valida que el valor sea un puerto TCP posible, de 1 a 65535.

    Args:
        raw_value: El texto tal como lo escribió el usuario.

    Returns:
        El puerto como entero.

    Raises:
        argparse.ArgumentTypeError: Si no es un entero o está fuera del rango. argparse
            la traduce en un mensaje de uso y termina con código 2.
    """
    try:
        port = int(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw_value}' no es un número") from None

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("un puerto va de 1 a 65535")
    return port


def configure_logging(verbose: bool) -> None:
    """Configura el registro de actividad del servidor.

    Un servidor no tiene a nadie mirándolo: el registro es la única forma de saber qué
    está pasando. Se escribe con hora para poder reconstruir una secuencia después.

    Args:
        verbose: Si es True, incluye los mensajes de depuración.

    Returns:
        None.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def install_shutdown_handlers(stop_requested: asyncio.Event) -> None:
    """Hace que las señales de terminación pidan un apagado ordenado.

    Sin esto, un Ctrl+C interrumpe el proceso en cualquier punto: quedan conexiones a
    medio cerrar y archivos a medio escribir. Con esto, la señal solo **activa un evento**
    y el servidor se entera por el camino normal, terminando lo que estaba haciendo.

    Se atienden dos señales: SIGINT es la del Ctrl+C, y SIGTERM la que envía el sistema
    —o Docker— para pedir que un proceso termine.

    Args:
        stop_requested: Evento que se activa al recibir cualquiera de las dos señales.

    Returns:
        None.
    """
    # El event loop guarda una cola de tareas listas para avanzar y un registro de qué
    # está esperando cada corrutina suspendida. `add_signal_handler` es método suyo.
    loop = asyncio.get_running_loop()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        # Reemplaza el comportamiento por defecto de la señal, que sería matar el proceso.
        loop.add_signal_handler(signal_number, stop_requested.set)


async def run_server(arguments: argparse.Namespace) -> None:
    """Levanta el servidor y lo mantiene funcionando hasta que se pida detenerlo.

    Args:
        arguments: Los argumentos ya parseados de la línea de comandos.

    Returns:
        None. Vuelve cuando el servidor terminó de detenerse.

    Raises:
        OSError: Si no se puede abrir el socket de escucha.
    """
    # Bandera que despierta a las corrutinas suspendidas en wait() cuando pasa a True.
    stop_requested = asyncio.Event()
    install_shutdown_handlers(stop_requested)

    server = ImageServer(arguments.host, arguments.port)
    await server.serve_until_stopped(stop_requested)


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del servidor.

    Args:
        argv: Argumentos de la línea de comandos. Si es None se usan los del proceso; se
            pasan explícitos solo desde las pruebas.

    Returns:
        El código de salida del proceso.
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    configure_logging(arguments.verbose)

    try:
        asyncio.run(run_server(arguments))
    except OSError as error:
        logging.error("no se pudo levantar el servidor: %s", error)
        return EXIT_FAILURE

    return EXIT_OK
