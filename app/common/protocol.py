"""Framing de mensajes sobre TCP.

TCP entrega un flujo continuo de bytes, sin ninguna marca que separe un mensaje del
siguiente: lo que el emisor manda en tres operaciones puede llegar en una sola lectura, o
al revés. Este módulo resuelve ese único problema, anunciando la longitud del contenido
antes de enviarlo.

Formato de un mensaje, igual en ambas direcciones:

    +----------------+----------------------+----------------------+
    | 4 bytes u32 BE | header JSON (UTF-8)  | payload binario      |
    | long. header   |                      | (opcional)           |
    +----------------+----------------------+----------------------+

Los primeros cuatro bytes son un entero sin signo en big-endian (el orden de red) que
indica cuánto mide el header. Como su tamaño es fijo, siempre se puede leer primero sin
ambigüedad. El header, a su vez, declara en 'payload_size' cuántos bytes binarios vienen
a continuación (0 si no viene ninguno).

Hay dos pares de funciones para enviar y recibir. `send_message` y `receive_payload`
trabajan en memoria y sirven para mensajes chicos; `send_file` y `stream_payload` van de a
bloques de CHUNK_SIZE y son las que sostienen los archivos grandes sin que la memoria
crezca con ellos.

Este módulo no sabe nada de la aplicación: no menciona imágenes, operaciones ni
identificadores de trabajo. Solo empaqueta y desempaqueta mensajes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, Final

LENGTH_PREFIX_SIZE: Final[int] = 4 #Bytes que ocupa el prefijo de longitud del header.

MAX_HEADER_SIZE: Final[int] = 64 * 1024 #Techo defensivo, para no reservar memoria ante un prefijo absurdo.

MAX_PAYLOAD_SIZE: Final[int] = 32 * 1024 * 1024 #Límite del framing, no el de la aplicación.

CHUNK_SIZE: Final[int] = 64 * 1024 #Bloque de transferencia, para acotar el uso de memoria.

PAYLOAD_SIZE_FIELD: Final[str] = "payload_size" #Campo donde se declara el tamaño del payload.

ProgressCallback = Callable[[int, int], None] #Informa avance: (bytes transferidos, total).


class ProtocolError(Exception):
    """El otro extremo envió algo que no respeta el formato de mensaje."""


# ─────────────────────── el prefijo de longitud ───────────────────────


def encode_length(length: int) -> bytes:
    """Codifica una longitud en el prefijo de 4 bytes que precede a cada header.

    Se usa big-endian —el byte más significativo primero— porque es el orden de red
    convencional: el mismo que usan las cabeceras de IP y de TCP. Fijarlo explícitamente
    hace que dos máquinas con distinta arquitectura se entiendan igual.

    Returns:
        Los cuatro bytes del prefijo, listos para anteponer al header.
    """
    return length.to_bytes(LENGTH_PREFIX_SIZE, "big")


def decode_length(raw_prefix: bytes) -> int:
    """Interpreta el prefijo de 4 bytes que precede a cada header.

    Args:
        raw_prefix: Los cuatro bytes leídos al principio de un mensaje.

    Returns:
        Cuántos bytes ocupa el header que viene a continuación.
    """
    return int.from_bytes(raw_prefix, "big")


# ─────────────────────────── armado del mensaje ───────────────────────────


def pack_header(header: dict[str, Any]) -> bytes:
    """Serializa un header a JSON compacto y le antepone su longitud en 4 bytes.

    Returns:
        Los bytes listos para escribir en el socket.

    Raises:
        ProtocolError: Si el header serializado supera MAX_HEADER_SIZE.
        TypeError: Si el header contiene valores que JSON no puede serializar.
    """
    serialized_header = json.dumps(header, separators=(",", ":")).encode("utf-8")

    if len(serialized_header) > MAX_HEADER_SIZE:
        raise ProtocolError(
            f"el header ocupa {len(serialized_header)} bytes y el máximo es {MAX_HEADER_SIZE}"
        )

    return encode_length(len(serialized_header)) + serialized_header


# ──────────────────────────────── envío ────────────────────────────────


def read_in_chunks(file_path: Path) -> Iterator[bytes]:
    """Recorre un archivo devolviéndolo de a bloques, sin cargarlo entero en memoria.

    Está separado del envío para que cada función tenga una sola idea: esta sabe leer un
    archivo por partes y no sabe nada de sockets; `send_file` sabe mandar por un socket y
    no sabe de dónde salen los bloques.

    Yields:
        Bloques de hasta CHUNK_SIZE bytes, en orden. El último puede ser más chico.

    Raises:
        OSError: Si el archivo no existe o no se puede leer.
    """
    with open(file_path, "rb") as source_file:
        while True:
            chunk = source_file.read(CHUNK_SIZE)
            if not chunk:  # read() devuelve b"" al llegar al final del archivo
                return
            yield chunk


async def send_message(
    writer: asyncio.StreamWriter,
    header: dict[str, Any],
    payload: bytes = b"",
) -> None:
    """Envía un mensaje completo, con un payload chico o sin payload.

    El header no se modifica: se envía una copia con 'payload_size' agregado. Para
    archivos grandes usar `send_file`, que no los carga en memoria.

    Raises:
        ProtocolError: Si el header o el payload superan sus límites.
    """
    validate_payload_size(len(payload))

    # Se copia el header para no modificar el que pasó quien llama.
    header_with_size = dict(header)
    header_with_size[PAYLOAD_SIZE_FIELD] = len(payload)

    framed_header = pack_header(header_with_size)
    writer.write(framed_header)
    if payload:
        writer.write(payload)
    await writer.drain()


async def send_file(
    writer: asyncio.StreamWriter,
    header: dict[str, Any],
    file_path: Path,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Envía un mensaje cuyo payload es un archivo, transmitido en bloques.

    No carga el archivo en memoria: lo lee y lo escribe de a CHUNK_SIZE bytes. El
    `drain()` posterior a cada bloque frena al emisor cuando el receptor no da abasto
    —evitando que el buffer de salida crezca sin control— y de paso le devuelve el
    control al event loop para que atienda a los demás clientes.

    El tamaño se mide antes de empezar, de modo que el header puede anunciar cuánto va a
    enviarse.

    Args:
        on_progress: Se llama después de cada bloque. Informa el avance sin que este
            módulo sepa quién lo muestra ni cómo. Debe retornar rápido, porque corre
            dentro del bucle de envío.

    Raises:
        ProtocolError: Si el archivo supera MAX_PAYLOAD_SIZE.
        OSError: Si el archivo no existe o no se puede leer.
    """
    file_size = file_path.stat().st_size
    validate_payload_size(file_size)

    # Se copia el header para no modificar el que pasó quien llama.
    header_with_size = dict(header)
    header_with_size[PAYLOAD_SIZE_FIELD] = file_size

    framed_header = pack_header(header_with_size)
    writer.write(framed_header)
    await writer.drain()

    sent_bytes = 0
    for chunk in read_in_chunks(file_path):
        writer.write(chunk)
        await writer.drain()

        sent_bytes += len(chunk)
        if on_progress is not None:
            on_progress(sent_bytes, file_size)


# ────────────────────────────── recepción ──────────────────────────────


async def receive_header(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Lee el prefijo de longitud y el header que le sigue.

    El límite se verifica antes de leer, para que un prefijo disparatado no llegue a
    reservar memoria.

    Returns:
        El header deserializado. Usar `payload_size_of` para leer su 'payload_size'.

    Raises:
        ProtocolError: Si el header excede el límite, no es JSON válido o no es un objeto.
        asyncio.IncompleteReadError: Si la conexión se cortó antes de completar la
            lectura. Es la forma normal de detectar que el otro extremo se fue.
    """
    length_prefix = await reader.readexactly(LENGTH_PREFIX_SIZE)
    header_size = decode_length(length_prefix)

    if header_size > MAX_HEADER_SIZE:
        raise ProtocolError(
            f"el emisor declaró un header de {header_size} bytes y el máximo es {MAX_HEADER_SIZE}"
        )

    serialized_header = await reader.readexactly(header_size)

    try:
        header = json.loads(serialized_header)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"el header no es JSON válido: {error}") from error

    # JSON válido incluye 42, "hola" y [1,2]. El protocolo exige un objeto, y sin este
    # control el primer header.get() fallaría con un error incomprensible.
    if not isinstance(header, dict):
        raise ProtocolError("el header debe ser un objeto JSON")

    return header


async def receive_payload(reader: asyncio.StreamReader, payload_size: int) -> bytes:
    """Lee un payload chico y lo devuelve completo en memoria.

    Para archivos grandes usar `stream_payload`, que los entrega por partes.

    Raises:
        ProtocolError: Si el tamaño declarado supera MAX_PAYLOAD_SIZE.
        asyncio.IncompleteReadError: Si la conexión se cortó antes de completar los
            bytes anunciados.
    """
    validate_payload_size(payload_size)

    if payload_size == 0:
        return b""
    return await reader.readexactly(payload_size)


async def stream_payload(
    reader: asyncio.StreamReader,
    payload_size: int,
) -> AsyncIterator[bytes]:
    """Entrega el payload por bloques, sin cargarlo entero en memoria.

    Es un generador asíncrono para que sea quien lo consume el que decida qué hacer con
    cada bloque —escribirlo en disco, hashearlo, o ambas—. El módulo se limita a leer del
    socket:

        header = await receive_header(reader)
        with open(destination, "wb") as image_file:
            async for chunk in stream_payload(reader, payload_size_of(header)):
                image_file.write(chunk)

    Yields:
        Bloques de hasta CHUNK_SIZE bytes, en orden. La suma de todos es exactamente
        `payload_size`.

    Raises:
        ProtocolError: Si el tamaño declarado supera MAX_PAYLOAD_SIZE.
        asyncio.IncompleteReadError: Si la conexión se cortó antes de completar los bytes
            anunciados. Es el mismo error que informa `receive_header` en ese caso.
    """
    validate_payload_size(payload_size)

    remaining_bytes = payload_size
    while remaining_bytes > 0:
        # El min() es lo que impide invadir el mensaje siguiente: pedir de más se
        # llevaría bytes que ya no son de este payload.
        block_size = min(CHUNK_SIZE, remaining_bytes)

        # readexactly devuelve esa cantidad exacta o lanza excepción, así que no hace
        # falta contar lo que efectivamente llegó ni detectar el corte a mano.
        yield await reader.readexactly(block_size)

        remaining_bytes -= block_size


# ─────────────────────────────── auxiliares ───────────────────────────────


def payload_size_of(header: dict[str, Any]) -> int:
    """Lee el 'payload_size' declarado en un header, validándolo.

    Returns:
        Los bytes de payload que siguen al header. Cero si el campo no está presente.

    Raises:
        ProtocolError: Si el campo no es un entero no negativo.
    """
    declared_size = header.get(PAYLOAD_SIZE_FIELD, 0)

    # El bool se descarta aparte porque en Python es subclase de int: sin esto, un
    # payload_size de `true` pasaría como si fuera 1.
    if not isinstance(declared_size, int) or isinstance(declared_size, bool):
        raise ProtocolError(f"'{PAYLOAD_SIZE_FIELD}' debe ser un entero")
    if declared_size < 0:
        raise ProtocolError(f"'{PAYLOAD_SIZE_FIELD}' no puede ser negativo")

    return declared_size


def validate_payload_size(payload_size: int) -> None:
    """Rechaza un payload que supere el techo del framing, antes de leerlo o escribirlo.

    Raises:
        ProtocolError: Si el tamaño supera MAX_PAYLOAD_SIZE.
    """
    if payload_size > MAX_PAYLOAD_SIZE:
        raise ProtocolError(
            f"el payload declarado es de {payload_size} bytes y el máximo es {MAX_PAYLOAD_SIZE}"
        )
