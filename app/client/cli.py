"""Línea de comandos del cliente: define los argumentos y decide qué hacer con ellos.

Es la puerta de entrada del cliente: parsea la línea de comandos, verifica las reglas que
`argparse` no puede expresar, ejecuta la acción pedida y traduce cualquier fallo esperable
en un mensaje legible y un código de salida.

La conversación con el servidor la resuelve `session.ClientSession` y la presentación
`console`; este módulo no hace ninguna de las dos cosas.

`argparse` no puede expresar que `--file` es obligatorio *solo* cuando la acción es
`submit`, así que esas reglas se verifican después de parsear y se informan con
`parser.error()`, que imprime el uso y sale con código 2 como cualquier herramienta de
Unix.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from rich.console import Console

from app.client import console as console_mode
from app.client import session
from app.common import config, messages, protocol

ACTIONS = ("submit", "status", "download", "history") #Las cuatro acciones, que son los cuatro pedidos.

EXIT_OK = 0 #Todo salió bien.
EXIT_FAILURE = 1 #El servidor rechazó el pedido, el trabajo falló o no se pudo conectar.
EXIT_BAD_USAGE = 2 #La línea de comandos está mal armada. Es la convención de argparse.
EXIT_INTERRUPTED = 130 #El usuario cortó con Ctrl+C. Es la convención de los shells de Unix.

USAGE_EXAMPLES = """Ejemplos:
  %(prog)s --user augusto --action submit --file foto.jpg --op inspect
      audita qué revela una foto, sin modificarla

  %(prog)s --user augusto --action submit --file foto.jpg --op sanitize --wait -o lista.jpg
      cubre caras, borra metadatos, comprime, y descarga el resultado

  %(prog)s --user augusto --action history --limit 20
      lista los últimos veinte trabajos"""


def build_parser() -> argparse.ArgumentParser:
    """Arma el parser completo, un grupo de argumentos por vez."""
    parser = argparse.ArgumentParser(
        prog="python -m app.client",
        description="Cliente del servicio de anonimización y sanitización de imágenes.",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    add_identity_arguments(parser)
    add_action_arguments(parser)
    add_submit_arguments(parser)
    add_operation_parameters(parser)
    add_query_arguments(parser)

    return parser


def add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    """Agrega quién sos y con qué servidor hablás."""
    group = parser.add_argument_group("identidad y conexión")
    group.add_argument(
        "--user", required=True,
        help="nombre de usuario con el que se declaran los trabajos (no se autentica)",
    )
    group.add_argument(
        "--host", default=config.DEFAULT_HOST,
        help=f"dirección o nombre del servidor, IPv4 o IPv6 (por defecto: {config.DEFAULT_HOST})",
    )
    group.add_argument(
        "--port", type=port_number, default=config.DEFAULT_PORT,
        help=f"puerto del servidor (por defecto: {config.DEFAULT_PORT})",
    )


def add_action_arguments(parser: argparse.ArgumentParser) -> None:
    """Agrega la elección de qué acción ejecutar."""
    group = parser.add_argument_group("acción")
    group.add_argument(
        "--action", choices=ACTIONS, required=True, help="qué hacer",
    )


def add_submit_arguments(parser: argparse.ArgumentParser) -> None:
    """Agrega lo propio del envío de una imagen."""
    group = parser.add_argument_group("envío de una imagen (--action submit)")
    group.add_argument("-f", "--file", type=Path, help="imagen a enviar (JPEG o PNG)")
    group.add_argument(
        "--op", choices=sorted(config.OPERATION_PARAMETERS),
        help="operación a aplicar sobre la imagen",
    )
    group.add_argument(
        "--wait", action="store_true",
        help="esperar a que el trabajo termine y descargar el resultado",
    )
    group.add_argument(
        "--timeout", type=positive_number, default=config.DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=(
            "segundos máximos de espera con --wait "
            f"(por defecto: {config.DEFAULT_WAIT_TIMEOUT_SECONDS}); "
            "rendirse no cancela el trabajo"
        ),
    )


def add_operation_parameters(parser: argparse.ArgumentParser) -> None:
    """Agrega los parámetros de las operaciones. Cuál corresponde a cuál lo dice `config`."""
    group = parser.add_argument_group("parámetros de la operación")
    group.add_argument(
        "--mode", choices=config.ANONYMIZE_MODES,
        help="cómo cubrir las caras detectadas (anonymize, sanitize)",
    )
    group.add_argument(
        "--strength", type=positive_integer,
        help="intensidad del difuminado o del pixelado (anonymize, sanitize)",
    )
    group.add_argument(
        "--format", choices=config.CONVERT_FORMATS, help="formato de salida (convert)",
    )
    group.add_argument(
        "--quality", type=quality_level,
        help="calidad de la recompresión, de 1 a 95 (convert, compress, sanitize)",
    )
    group.add_argument(
        "--max-size", type=positive_integer,
        help="lado máximo en píxeles; la imagen se reduce si lo supera (compress, sanitize)",
    )


def add_query_arguments(parser: argparse.ArgumentParser) -> None:
    """Agrega lo propio de consultar, descargar y listar."""
    group = parser.add_argument_group("consultas (--action status, download, history)")
    group.add_argument("--job-id", help="identificador del trabajo a consultar o descargar")
    group.add_argument(
        "-o", "--output", type=Path,
        help="dónde guardar el archivo descargado (por defecto, el nombre que sugiera el servidor)",
    )
    group.add_argument(
        "--limit", type=positive_integer, default=10,
        help="cantidad de trabajos a listar en el historial (por defecto: 10)",
    )


# ────────────────────── validadores de los argumentos ──────────────────────


def port_number(raw_value: str) -> int:
    """Valida que sea un puerto TCP posible, de 1 a 65535.

    Raises:
        argparse.ArgumentTypeError: Si no es un entero, o si está fuera del rango.
    """
    try:
        port = int(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw_value}' no es un número") from None

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("un puerto va de 1 a 65535")
    return port


def positive_integer(raw_value: str) -> int:
    """Valida que sea un entero mayor que cero.

    Raises:
        argparse.ArgumentTypeError: Si no es un entero, o si no es positivo.
    """
    try:
        value = int(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw_value}' no es un número entero") from None

    if value <= 0:
        raise argparse.ArgumentTypeError("tiene que ser mayor que cero")
    return value


def positive_number(raw_value: str) -> float:
    """Valida que sea un número mayor que cero, con o sin decimales.

    Raises:
        argparse.ArgumentTypeError: Si no es un número, o si no es positivo.
    """
    try:
        value = float(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw_value}' no es un número") from None

    if value <= 0:
        raise argparse.ArgumentTypeError("tiene que ser mayor que cero")
    return value


def quality_level(raw_value: str) -> int:
    """Valida que sea un nivel de calidad de 1 a 95.

    El techo es 95 y no 100 porque por encima de ese valor JPEG deja de comprimir de forma
    útil: el archivo crece mucho sin mejora visible.

    Raises:
        argparse.ArgumentTypeError: Si no es un entero, o si está fuera del rango.
    """
    try:
        quality = int(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{raw_value}' no es un número entero") from None

    if not 1 <= quality <= 95:
        raise argparse.ArgumentTypeError("la calidad va de 1 a 95")
    return quality


# ───────────────────── reglas que argparse no puede expresar ─────────────────────


def check_action_requirements(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> None:
    """Verifica que estén los argumentos que exige la acción elegida.

    Si falta alguno, `parser.error` imprime el uso y termina el proceso con código 2.
    """
    if arguments.action == "submit":
        if arguments.file is None:
            parser.error("--action submit necesita --file con la imagen a enviar")
        if arguments.op is None:
            parser.error("--action submit necesita --op con la operación a aplicar")

    if arguments.action in ("status", "download") and arguments.job_id is None:
        parser.error(f"--action {arguments.action} necesita --job-id")

    if arguments.action != "submit" and arguments.wait:
        parser.error("--wait solo tiene sentido con --action submit")


def collect_operation_parameters(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace, operation: str
) -> dict[str, Any]:
    """Junta los parámetros que corresponden a la operación pedida.

    Solo se incluyen los que el usuario escribió: los que no pasó quedan afuera y el
    servidor aplica su valor por defecto. Pasar un parámetro que la operación no acepta es
    un error, porque casi siempre es una confusión —`--mode` con `--op clean`— y
    silenciarlo haría creer que se aplicó.
    """
    accepted = config.OPERATION_PARAMETERS[operation]
    parameters: dict[str, Any] = {}

    # vars() convierte el Namespace de argparse en un diccionario común.
    argument_values = vars(arguments)

    for parameter_name in config.ALL_OPERATION_PARAMETERS:
        value = argument_values.get(parameter_name)
        if value is None:
            continue

        if parameter_name not in accepted:
            accepted_list = ", ".join(f"--{name.replace('_', '-')}" for name in accepted)
            parser.error(
                f"--{parameter_name.replace('_', '-')} no es un parámetro de '{operation}'; "
                + (f"acepta: {accepted_list}" if accepted else "no acepta parámetros")
            )

        parameters[parameter_name] = value

    return parameters


# ──────────────────────────────── ejecución ────────────────────────────────


async def run_direct_action(
    arguments: argparse.Namespace, parameters: dict[str, Any]
) -> int:
    """Abre la conexión, ejecuta la acción pedida y cierra.

    Returns:
        El código de salida del proceso.
    """
    output = console_mode.build_console()

    async with session.ClientSession(arguments.host, arguments.port, arguments.user) as client:
        if arguments.action == "submit":
            return await console_mode.run_submit(
                client, output, arguments.file, arguments.op, parameters,
                arguments.wait, arguments.timeout, arguments.output,
            )
        if arguments.action == "status":
            return await console_mode.run_status(client, output, arguments.job_id)
        if arguments.action == "download":
            return await console_mode.run_download(
                client, output, arguments.job_id, arguments.output
            )
        return await console_mode.run_history(client, output, arguments.limit)


def report_failure(
    errors: Console, error: BaseException, arguments: argparse.Namespace
) -> int:
    """Traduce una excepción esperable en un mensaje legible y un código de salida.

    Que el usuario vea una traza de Python sería un error de este programa, no información
    útil.

    Returns:
        El código de salida que corresponde a ese fallo.
    """
    target = f"{arguments.host}:{arguments.port}"

    if isinstance(error, messages.ServerError):
        console_mode.print_error(
            errors, f"El servidor rechazó el pedido ({error.code})", error.message
        )
    elif isinstance(error, asyncio.IncompleteReadError):
        console_mode.print_error(
            errors, "El servidor cortó la conexión",
            "La conexión se cerró antes de que llegara la respuesta completa.",
        )
    elif isinstance(error, protocol.ProtocolError):
        console_mode.print_error(errors, "Respuesta mal formada", str(error))
    elif isinstance(error, OSError):
        console_mode.print_error(
            errors, f"No se pudo hablar con {target}", str(error),
            "Verificá que el servidor esté levantado y que el host y el puerto sean los correctos.",
        )
    else:  # KeyboardInterrupt
        errors.print("\n[yellow]Interrumpido.[/yellow] Los trabajos ya enviados siguen su curso.")
        return EXIT_INTERRUPTED

    return EXIT_FAILURE


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del cliente: parsea, despacha y traduce los fallos a un código.

    Args:
        argv: Argumentos de la línea de comandos. Si es None se usan los del proceso; se
            pasan explícitos solo desde las pruebas.

    Returns:
        El código de salida del proceso.
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    check_action_requirements(parser, arguments)

    parameters: dict[str, Any] = {}
    errors = Console(stderr=True)

    if arguments.action == "submit":
        parameters = collect_operation_parameters(parser, arguments, arguments.op)

        # Se valida antes de conectar: si el archivo no sirve, no hay por qué molestar al
        # servidor ni ocupar el enlace.
        try:
            session.validate_image_file(arguments.file)
        except session.LocalValidationError as error:
            console_mode.print_error(errors, "La imagen no se puede enviar", str(error))
            return EXIT_BAD_USAGE

    try:
        return asyncio.run(run_direct_action(arguments, parameters))
    except (
        messages.ServerError,
        asyncio.IncompleteReadError,
        protocol.ProtocolError,
        OSError,
        KeyboardInterrupt,
    ) as error:
        return report_failure(errors, error, arguments)
