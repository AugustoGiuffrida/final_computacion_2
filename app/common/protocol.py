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

Este módulo no sabe nada de la aplicación: no menciona imágenes, operaciones ni
identificadores de trabajo. Solo empaqueta y desempaqueta mensajes.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

LENGTH_PREFIX_SIZE: Final[int] = 4
"""Cantidad de bytes que ocupa el prefijo de longitud del header."""

MAX_HEADER_SIZE: Final[int] = 64 * 1024
"""Techo defensivo del header, para no reservar memoria ante un prefijo absurdo."""

MAX_PAYLOAD_SIZE: Final[int] = 32 * 1024 * 1024
"""Techo defensivo del payload.

Es el límite del framing, no el de la aplicación: existe para rechazar un tamaño
disparatado antes de empezar a leerlo. El límite de negocio —cuánto puede pesar una
imagen— es más bajo y vive en `config.DEFAULT_MAX_IMAGE_SIZE`.
"""

CHUNK_SIZE: Final[int] = 64 * 1024
"""Tamaño de bloque al transferir archivos, para acotar el uso de memoria."""

PAYLOAD_SIZE_FIELD: Final[str] = "payload_size"
"""Campo del header donde se declara el tamaño del payload."""


class ProtocolError(Exception):
    """El otro extremo envió algo que no respeta el formato de mensaje."""


def pack_header(header: dict[str, Any]) -> bytes:
    """Serializa un header a JSON y le antepone su longitud.

    Args:
        header: Campos del mensaje. Se serializan como JSON compacto en UTF-8.

    Returns:
        Los bytes listos para escribir en el socket: el prefijo de longitud seguido
        del header serializado.

    Raises:
        ProtocolError: Si el header serializado supera MAX_HEADER_SIZE.
        TypeError: Si el header contiene valores que JSON no puede serializar.
    """
    serialized_header = json.dumps(header, separators=(",", ":")).encode("utf-8")

    if len(serialized_header) > MAX_HEADER_SIZE:
        raise ProtocolError(
            f"el header ocupa {len(serialized_header)} bytes y el máximo es {MAX_HEADER_SIZE}"
        )

    length_prefix = len(serialized_header).to_bytes(LENGTH_PREFIX_SIZE, "big")
    return length_prefix + serialized_header


# ──────────────────────────────── envío ────────────────────────────────


async def send_message(
    writer: asyncio.StreamWriter,
    header: dict[str, Any],
    payload: bytes = b"",
) -> None:
    """Envía un mensaje completo, con un payload chico o sin payload.

    Para archivos grandes usar `send_file`, que no los carga en memoria.

    Args:
        writer: Stream de escritura de una conexión ya establecida.
        header: Campos del mensaje. No se modifica: se envía una copia con el
            campo 'payload_size' agregado.
        payload: Contenido binario que acompaña al header. Vacío por defecto.

    Returns:
        None. Al terminar, el mensaje quedó entregado al sistema operativo.

    Raises:
        ProtocolError: Si el header o el payload superan sus límites.
    """
    validate_payload_size(len(payload))

    writer.write(pack_header({**header, PAYLOAD_SIZE_FIELD: len(payload)}))
    if payload:
        writer.write(payload)
    await writer.drain()


async def send_file(
    writer: asyncio.StreamWriter,
    header: dict[str, Any],
    file_path: Path,
) -> None:
    """Envía un mensaje cuyo payload es un archivo, transmitido en bloques.

    No carga el archivo en memoria: lo lee y lo escribe de a CHUNK_SIZE bytes. El
    `drain()` posterior a cada bloque frena al emisor cuando el receptor no da abasto
    —evitando que el buffer de salida crezca sin control— y de paso le devuelve el
    control al event loop para que atienda a los demás clientes.

    El tamaño se mide antes de empezar, de modo que el header puede anunciar cuánto
    va a enviarse.

    Args:
        writer: Stream de escritura de una conexión ya establecida.
        header: Campos del mensaje. No se modifica: se envía una copia con el
            campo 'payload_size' agregado.
        file_path: Ruta del archivo cuyo contenido se envía como payload.

    Returns:
        None. Al terminar, el archivo quedó enviado por completo.

    Raises:
        ProtocolError: Si el archivo supera MAX_PAYLOAD_SIZE.
        OSError: Si el archivo no existe o no se puede leer.
    """
    file_size = os.path.getsize(file_path)
    validate_payload_size(file_size)

    writer.write(pack_header({**header, PAYLOAD_SIZE_FIELD: file_size}))
    await writer.drain()

    with open(file_path, "rb") as source_file:
        while chunk := source_file.read(CHUNK_SIZE):
            writer.write(chunk)
            await writer.drain()


# ────────────────────────────── recepción ──────────────────────────────


async def receive_header(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Lee el prefijo de longitud y el header que le sigue.

    Args:
        reader: Stream de lectura de una conexión ya establecida.

    Returns:
        El header deserializado. Siempre incluye 'payload_size' si el emisor
        respeta el formato; usar `payload_size_of` para leerlo con seguridad.

    Raises:
        ProtocolError: Si el header excede el límite, no es JSON válido o no es
            un objeto.
        asyncio.IncompleteReadError: Si la conexión se cortó antes de completar la
            lectura. Es la forma normal de detectar que el otro extremo se fue.
    """
    length_prefix = await reader.readexactly(LENGTH_PREFIX_SIZE)
    header_size = int.from_bytes(length_prefix, "big")

    if header_size > MAX_HEADER_SIZE:
        raise ProtocolError(
            f"el emisor declaró un header de {header_size} bytes y el máximo es {MAX_HEADER_SIZE}"
        )

    serialized_header = await reader.readexactly(header_size)

    try:
        header = json.loads(serialized_header)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"el header no es JSON válido: {error}") from error

    if not isinstance(header, dict):
        raise ProtocolError("el header debe ser un objeto JSON")

    return header


async def receive_payload(reader: asyncio.StreamReader, payload_size: int) -> bytes:
    """Lee un payload chico y lo devuelve completo en memoria.

    Para archivos grandes usar `stream_payload`, que los entrega por partes.

    Args:
        reader: Stream de lectura de una conexión ya establecida.
        payload_size: Cantidad exacta de bytes a leer, tomada del header.

    Returns:
        Los bytes del payload, o `b""` si el tamaño declarado era cero.

    Raises:
        ProtocolError: Si el tamaño declarado supera MAX_PAYLOAD_SIZE.
        asyncio.IncompleteReadError: Si la conexión se cortó antes de completar
            los bytes anunciados.
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

    Devuelve un generador asíncrono para que sea quien lo consume el que decida qué
    hacer con cada bloque: escribirlo en disco, calcularle un hash, o ambas cosas.
    El módulo se limita a leer del socket.

    Uso típico:

        header = await receive_header(reader)
        with open(destination, "wb") as image_file:
            async for chunk in stream_payload(reader, payload_size_of(header)):
                image_file.write(chunk)

    Args:
        reader: Stream de lectura de una conexión ya establecida.
        payload_size: Cantidad exacta de bytes a leer, tomada del header.

    Yields:
        Bloques de hasta CHUNK_SIZE bytes, en orden. La suma de todos los bloques
        es exactamente `payload_size`.

    Raises:
        ProtocolError: Si el tamaño declarado supera MAX_PAYLOAD_SIZE, o si la
            conexión se cortó antes de completar los bytes anunciados.
    """
    validate_payload_size(payload_size)

    remaining_bytes = payload_size
    while remaining_bytes > 0:
        chunk = await reader.read(min(CHUNK_SIZE, remaining_bytes))

        if not chunk:
            raise ProtocolError(
                f"la transferencia se cortó: faltaban {remaining_bytes} de {payload_size} bytes"
            )

        remaining_bytes -= len(chunk)
        yield chunk


# ─────────────────────────────── auxiliares ───────────────────────────────


def payload_size_of(header: dict[str, Any]) -> int:
    """Lee el tamaño de payload declarado en un header, validándolo.

    Args:
        header: Header ya deserializado por `receive_header`.

    Returns:
        La cantidad de bytes de payload que siguen al header. Cero si el campo no
        está presente.

    Raises:
        ProtocolError: Si el campo no es un entero no negativo.
    """
    declared_size = header.get(PAYLOAD_SIZE_FIELD, 0)

    if not isinstance(declared_size, int) or isinstance(declared_size, bool):
        raise ProtocolError(f"'{PAYLOAD_SIZE_FIELD}' debe ser un entero")
    if declared_size < 0:
        raise ProtocolError(f"'{PAYLOAD_SIZE_FIELD}' no puede ser negativo")

    return declared_size


def validate_payload_size(payload_size: int) -> None:
    """Verifica que un tamaño de payload esté dentro del límite del framing.

    Se llama antes de leer o escribir, para que un valor disparatado no llegue a
    reservar memoria ni a ocupar el enlace.

    Args:
        payload_size: Cantidad de bytes que se pretende transferir.

    Returns:
        None. Si el tamaño es válido, la función simplemente retorna.

    Raises:
        ProtocolError: Si el tamaño supera MAX_PAYLOAD_SIZE.
    """
    if payload_size > MAX_PAYLOAD_SIZE:
        raise ProtocolError(
            f"el payload declarado es de {payload_size} bytes y el máximo es {MAX_PAYLOAD_SIZE}"
        )
