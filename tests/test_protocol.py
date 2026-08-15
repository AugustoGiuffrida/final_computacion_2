"""Pruebas del framing de mensajes.

Levantan un servidor de prueba sobre localhost y hacen viajar mensajes de verdad por un
socket. Es la única forma de verificar que el framing resuelve el problema real: que TCP
entrega un flujo continuo y los mensajes pueden llegar partidos de cualquier manera.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.common import protocol


async def open_connected_pair() -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter, asyncio.Server
]:
    """Levanta un servidor local y devuelve los dos extremos de una conexión.

    Returns:
        Una tupla con el reader y el writer del cliente, el reader y el writer del
        servidor, y el objeto servidor para poder cerrarlo al terminar.
    """
    server_side: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server_side.set_result((reader, writer))

    server = await asyncio.start_server(on_client_connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
    server_reader, server_writer = await server_side

    return client_reader, client_writer, server_reader, server_writer, server


@pytest.mark.asyncio
async def test_message_without_payload_survives_the_round_trip() -> None:
    """Un header sin payload llega igual del otro lado."""
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    sent_header = {"type": "status", "user": "augusto", "job_id": "a3f7b2c1"}
    await protocol.send_message(client_writer, sent_header)

    received_header = await protocol.receive_header(server_reader)

    assert received_header["type"] == "status"
    assert received_header["user"] == "augusto"
    assert protocol.payload_size_of(received_header) == 0

    server.close()


@pytest.mark.asyncio
async def test_two_messages_in_a_row_do_not_get_mixed() -> None:
    """Dos mensajes seguidos se leen como dos mensajes, no como uno solo.

    Es exactamente lo que el framing tiene que garantizar: TCP no conserva la
    separación entre envíos.
    """
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    await protocol.send_message(client_writer, {"type": "first"}, b"aaa")
    await protocol.send_message(client_writer, {"type": "second"}, b"bbbb")

    first_header = await protocol.receive_header(server_reader)
    first_payload = await protocol.receive_payload(server_reader, protocol.payload_size_of(first_header))
    second_header = await protocol.receive_header(server_reader)
    second_payload = await protocol.receive_payload(server_reader, protocol.payload_size_of(second_header))

    assert first_header["type"] == "first"
    assert first_payload == b"aaa"
    assert second_header["type"] == "second"
    assert second_payload == b"bbbb"

    server.close()


@pytest.mark.asyncio
async def test_a_file_larger_than_the_chunk_arrives_complete(tmp_path: Path) -> None:
    """Un archivo que no entra en un solo bloque llega entero y sin alterarse.

    El tamaño elegido obliga a varias iteraciones del bucle de envío y de lectura,
    que es donde aparecerían los errores de contabilidad de bytes.
    """
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    original_content = bytes(range(256)) * 1500  # 384.000 bytes: casi seis bloques
    source_file = tmp_path / "imagen.jpg"
    source_file.write_bytes(original_content)

    destination_file = tmp_path / "recibida.jpg"

    async def receive_into_file() -> None:
        header = await protocol.receive_header(server_reader)
        with open(destination_file, "wb") as output_file:
            async for chunk in protocol.stream_payload(server_reader, protocol.payload_size_of(header)):
                output_file.write(chunk)

    await asyncio.gather(
        protocol.send_file(client_writer, {"type": "submit", "filename": "imagen.jpg"}, source_file),
        receive_into_file(),
    )

    assert destination_file.read_bytes() == original_content

    server.close()


@pytest.mark.asyncio
async def test_a_cut_in_the_middle_of_the_payload_is_reported() -> None:
    """Si la conexión se corta a mitad del payload, la lectura falla en vez de mentir."""
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    # Anuncia diez bytes pero envía tres y cierra.
    client_writer.write(protocol.pack_header({"type": "submit", "payload_size": 10}))
    client_writer.write(b"abc")
    await client_writer.drain()
    client_writer.close()

    header = await protocol.receive_header(server_reader)

    with pytest.raises(protocol.ProtocolError, match="se cortó"):
        async for _ in protocol.stream_payload(server_reader, protocol.payload_size_of(header)):
            pass

    server.close()


@pytest.mark.asyncio
async def test_an_absurd_header_size_is_rejected_before_reading_it() -> None:
    """Un prefijo disparatado se rechaza sin intentar leer esa cantidad de bytes."""
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    absurd_size = protocol.MAX_HEADER_SIZE + 1
    client_writer.write(absurd_size.to_bytes(protocol.LENGTH_PREFIX_SIZE, "big"))
    await client_writer.drain()

    with pytest.raises(protocol.ProtocolError, match="máximo"):
        await protocol.receive_header(server_reader)

    server.close()


@pytest.mark.asyncio
async def test_a_header_that_is_not_json_is_rejected() -> None:
    """Un header que no es JSON válido produce un error claro, no un crash."""
    client_reader, client_writer, server_reader, server_writer, server = await open_connected_pair()

    garbage = b"esto no es json"
    client_writer.write(len(garbage).to_bytes(protocol.LENGTH_PREFIX_SIZE, "big"))
    client_writer.write(garbage)
    await client_writer.drain()

    with pytest.raises(protocol.ProtocolError, match="JSON"):
        await protocol.receive_header(server_reader)

    server.close()


def test_the_header_carries_the_declared_payload_size() -> None:
    """`send_message` agrega el tamaño del payload sin tocar el header original."""
    original_header = {"type": "submit"}
    packed = protocol.pack_header({**original_header, protocol.PAYLOAD_SIZE_FIELD: 42})

    header_size = int.from_bytes(packed[: protocol.LENGTH_PREFIX_SIZE], "big")
    decoded_header = json.loads(packed[protocol.LENGTH_PREFIX_SIZE :])

    assert header_size == len(packed) - protocol.LENGTH_PREFIX_SIZE
    assert decoded_header[protocol.PAYLOAD_SIZE_FIELD] == 42
    assert "payload_size" not in original_header  # no se modificó el original
