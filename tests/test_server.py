"""Pruebas del proceso principal del servidor.

Levantan el servidor de verdad en un puerto libre y lo interrogan con la misma
`ClientSession` que usa el cliente real. No se simula nada: los mensajes viajan por un
socket TCP y atraviesan el framing completo en las dos direcciones.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.client import session
from app.common import messages, protocol
from app.server.server import ImageServer


async def running_server() -> ImageServer:
    """Levanta un servidor en un puerto libre de localhost.

    Returns:
        El servidor ya escuchando. Quien lo use tiene que detenerlo al terminar.
    """
    server = ImageServer("127.0.0.1", 0)
    await server.start()
    return server


async def connected_client(server: ImageServer) -> session.ClientSession:
    """Conecta una sesión de cliente al servidor indicado.

    Args:
        server: Servidor ya escuchando.

    Returns:
        La sesión conectada.
    """
    client = session.ClientSession("127.0.0.1", server.listening_port, "augusto")
    await client.connect()
    return client


# ─────────────────────────── ciclo de vida ───────────────────────────


@pytest.mark.asyncio
async def test_the_server_accepts_a_connection() -> None:
    """Un cliente puede conectarse a un servidor recién levantado."""
    server = await running_server()
    client = await connected_client(server)

    assert client.is_connected

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_the_server_counts_its_connected_clients() -> None:
    """El contador de conexiones sube al conectarse y baja al desconectarse."""
    server = await running_server()

    first_client = await connected_client(server)
    second_client = await connected_client(server)
    await asyncio.sleep(0.05)  # las tareas de los handlers ya arrancaron

    assert server.connected_clients == 2

    await first_client.close()
    await second_client.close()
    await asyncio.sleep(0.05)

    assert server.connected_clients == 0

    await server.stop()


@pytest.mark.asyncio
async def test_stopping_the_server_closes_its_listening_socket() -> None:
    """Después de detenerlo, nadie más puede conectarse."""
    server = await running_server()
    port = server.listening_port
    await server.stop()

    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


# ─────────────────────────── atención de pedidos ───────────────────────────


@pytest.mark.asyncio
async def test_a_known_request_is_answered_as_not_implemented_yet() -> None:
    """Los cuatro pedidos del protocolo se reconocen, pero todavía no se atienden.

    Es el estado esperado de esta etapa: el framing y el despacho funcionan; la lógica de
    cada pedido llega cuando el profesor apruebe los componentes que necesita.
    """
    server = await running_server()
    client = await connected_client(server)

    with pytest.raises(messages.ServerError) as raised:
        await client.history(limit=10)

    assert raised.value.code == messages.INTERNAL
    assert "history" in raised.value.message

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_an_unknown_request_type_is_rejected() -> None:
    """Un tipo de mensaje que no existe se rechaza con BAD_REQUEST."""
    server = await running_server()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.listening_port)

    await protocol.send_message(writer, {
        messages.TYPE_FIELD: "borrar_todo", "user": "augusto"
    })
    response = await protocol.receive_header(reader)

    assert response[messages.TYPE_FIELD] == messages.ERROR
    assert response["code"] == messages.BAD_REQUEST

    writer.close()
    await server.stop()


@pytest.mark.asyncio
async def test_the_payload_is_consumed_even_when_the_request_is_rejected(
    tmp_path: Path,
) -> None:
    """Tras rechazar un pedido con imagen, el mensaje siguiente se lee correctamente.

    Es la prueba más importante de esta etapa. Si el servidor no consumiera el payload de
    un pedido que rechaza, esos bytes quedarían en el socket y el `receive_header` del
    mensaje siguiente los tomaría como su prefijo de longitud: el diálogo quedaría
    desfasado y ninguno de los dos extremos podría detectarlo.
    """
    server = await running_server()
    client = await connected_client(server)

    image = tmp_path / "foto.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 200_000)  # más de tres bloques

    # Primer pedido: lleva 200 KB de payload y el servidor lo rechaza.
    with pytest.raises(messages.ServerError):
        await client.submit(image, "inspect", {})

    # Segundo pedido sobre la misma conexión: tiene que entenderse bien.
    with pytest.raises(messages.ServerError) as raised:
        await client.status("a3f7b2c1")

    assert raised.value.code == messages.INTERNAL
    assert "status" in raised.value.message

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_several_requests_travel_over_one_connection() -> None:
    """Una conexión transporta varios pedidos seguidos, como manda el protocolo."""
    server = await running_server()
    client = await connected_client(server)

    for _ in range(5):
        with pytest.raises(messages.ServerError):
            await client.status("a3f7b2c1")

    assert client.is_connected

    await client.close()
    await server.stop()


# ─────────────────────────── robustez ───────────────────────────


@pytest.mark.asyncio
async def test_garbage_does_not_bring_down_the_server() -> None:
    """Un cliente que manda basura recibe un error y no afecta a los demás."""
    server = await running_server()

    # Un cliente envía un prefijo que anuncia un header disparatado.
    _, bad_writer = await asyncio.open_connection("127.0.0.1", server.listening_port)
    absurd_size = protocol.MAX_HEADER_SIZE + 1
    bad_writer.write(protocol.encode_length(absurd_size))
    await bad_writer.drain()
    await asyncio.sleep(0.05)
    bad_writer.close()

    # El servidor sigue atendiendo con normalidad.
    good_client = await connected_client(server)
    with pytest.raises(messages.ServerError):
        await good_client.history(limit=1)

    await good_client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_a_client_that_disappears_does_not_affect_the_others() -> None:
    """Si un cliente se corta de golpe, los demás siguen atendidos."""
    server = await running_server()

    abandoned_client = await connected_client(server)
    surviving_client = await connected_client(server)

    # Se cierra sin avisar, en medio de la sesión.
    abandoned_client._writer.transport.abort()  # type: ignore[union-attr]
    await asyncio.sleep(0.05)

    with pytest.raises(messages.ServerError):
        await surviving_client.history(limit=1)

    assert server.connected_clients == 1

    await surviving_client.close()
    await server.stop()
