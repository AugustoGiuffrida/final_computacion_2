"""Lo que sale: armar y mandar las respuestas del servidor.

Dos funciones envían —un error del protocolo, y el mismo error cuando ya puede no haber
nadie del otro lado— y dos calculan datos que viajan en el header de una descarga.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.common import messages, protocol
from app.server.main import registry


# Los tipos MIME de lo que este servidor puede llegar a servir. Son cuatro: no hace falta
# una biblioteca para esto.
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


# ──────────────────────────── mensajes de error ────────────────────────────


async def respond_error(
    writer: asyncio.StreamWriter, code: str, detail: str = ""
) -> None:
    """Responde un mensaje de error del protocolo.

    Un error es una respuesta como cualquier otra y no cierra la conexión: el cliente
    tiene que poder distinguir un pedido rechazado de una caída del servidor.

    Args:
        writer: Stream de escritura de la conexión.
        code: Uno de los códigos de `messages`.
        detail: Explicación para el usuario. Si se omite, el cliente usa la explicación
            estándar del código.
    """
    await protocol.send_message(writer, {
        messages.TYPE_FIELD: messages.ERROR,
        "code": code,
        "message": detail,
    })


async def try_to_report(
    writer: asyncio.StreamWriter, code: str, detail: str
) -> None:
    """Intenta informar un error, aceptando que quizá ya no haya nadie escuchando.

    Se usa cuando el fallo pudo haber roto la conexión. Si el envío falla, no hay nada
    que hacer ni nada que informar: el cliente ya no está.

    Args:
        writer: Stream de escritura de la conexión.
        code: Uno de los códigos de `messages`.
        detail: Explicación para el usuario.
    """
    try:
        await respond_error(writer, code, detail)
    except (OSError, protocol.ProtocolError):
        pass


# ─────────────────────────── datos de una descarga ───────────────────────────


def suggested_download_name(job: registry.Job) -> str:
    """Arma un nombre de archivo legible para el resultado de un trabajo.

    En disco el resultado se llama siempre igual —un nombre interno y uniforme— pero al
    cliente le sirve más algo que diga de dónde salió: `foto_anonymize.jpg` en lugar de
    `out.jpg`. Es solo una sugerencia: el cliente puede guardarlo donde indique `-o`.

    Args:
        job: El trabajo terminado, con su archivo de salida.

    Returns:
        El nombre sugerido, combinando el nombre original y la operación aplicada.
    """
    original = Path(job.filename)
    output_suffix = job.output_path.suffix if job.output_path else original.suffix

    return f"{original.stem}_{job.operation}{output_suffix}"


def content_type_of(file_path: Path) -> str:
    """Deduce el tipo de contenido de un archivo a partir de su extensión.

    Args:
        file_path: Ruta del archivo.

    Returns:
        El tipo MIME, o 'application/octet-stream' si la extensión no es conocida.
    """
    return CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
