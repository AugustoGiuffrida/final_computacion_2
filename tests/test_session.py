"""Pruebas de la capa de red del cliente.

El servidor real todavía no está escrito, así que cada prueba levanta un servidor de
mentira que habla el protocolo y responde lo que la prueba necesita. No es una simulación
de los objetos: los mensajes viajan de verdad por un socket TCP sobre localhost, igual que
en `test_protocol.py`. Lo único fingido es qué contesta del otro lado.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from app.client import session
from app.common import config, messages, protocol

ServerBehaviour = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


class FakeServer:
    """Servidor de prueba que ejecuta el comportamiento que le indique cada prueba.

    Guarda los pedidos que recibe, para poder verificar que el cliente arma bien los
    headers y no solo que interpreta bien las respuestas.

    Attributes:
        requests: Headers de todos los pedidos recibidos, en orden de llegada.
        payloads: Bytes recibidos como payload de cada pedido que traía uno.
    """

    def __init__(self, behaviour: ServerBehaviour) -> None:
        """Prepara el servidor sin levantarlo todavía.

        Args:
            behaviour: Corrutina que atiende una conexión. Recibe el reader y el writer
                del lado del servidor.
        """
        self.behaviour = behaviour
        self.requests: list[dict[str, Any]] = []
        self.payloads: list[bytes] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Levanta el servidor en un puerto libre de localhost.

        Returns:
            El puerto que el sistema operativo le asignó.
        """
        self._server = await asyncio.start_server(self.behaviour, "127.0.0.1", 0)
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Cierra el servidor y espera a que libere el puerto.

        Returns:
            None.
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[dict[str, Any], bytes]:
        """Lee un pedido completo y lo registra.

        Args:
            reader: Stream de lectura del lado del servidor.

        Returns:
            Una tupla con el header del pedido y su payload (vacío si no traía).
        """
        header = await protocol.receive_header(reader)
        payload = await protocol.receive_payload(reader, protocol.payload_size_of(header))

        self.requests.append(header)
        self.payloads.append(payload)

        return header, payload


async def connected_session(fake_server: FakeServer) -> session.ClientSession:
    """Levanta el servidor de mentira y devuelve una sesión ya conectada a él.

    Args:
        fake_server: El servidor que va a atender.

    Returns:
        La sesión conectada, lista para enviar pedidos.
    """
    port = await fake_server.start()
    client_session = session.ClientSession("127.0.0.1", port, "augusto")
    await client_session.connect()
    return client_session


# ────────────────────────── validación local ──────────────────────────


def test_a_missing_file_is_rejected_without_touching_the_network(tmp_path: Path) -> None:
    """Un archivo inexistente falla localmente, sin llegar a abrir la conexión."""
    with pytest.raises(session.LocalValidationError, match="no existe"):
        session.validate_image_file(tmp_path / "no_esta.jpg")


def test_an_unsupported_extension_is_rejected(tmp_path: Path) -> None:
    """Una extensión fuera de las soportadas se rechaza antes de enviar nada."""
    document = tmp_path / "informe.pdf"
    document.write_bytes(b"contenido")

    with pytest.raises(session.LocalValidationError, match="no está soportada"):
        session.validate_image_file(document)


def test_an_image_over_the_limit_is_rejected(tmp_path: Path) -> None:
    """Una imagen que excede el máximo se rechaza sin ocupar el enlace."""
    oversized_image = tmp_path / "enorme.jpg"
    oversized_image.write_bytes(b"\x00" * (config.DEFAULT_MAX_IMAGE_SIZE + 1))

    with pytest.raises(session.LocalValidationError, match="máximo"):
        session.validate_image_file(oversized_image)


def test_a_valid_image_returns_its_size(tmp_path: Path) -> None:
    """Un archivo que pasa las tres verificaciones devuelve su tamaño en bytes."""
    image = tmp_path / "foto.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" * 100)

    assert session.validate_image_file(image) == 400


# ──────────────────────────────── submit ────────────────────────────────


@pytest.mark.asyncio
async def test_submit_sends_the_image_and_returns_the_job_id(tmp_path: Path) -> None:
    """Un submit manda el header con la operación y la imagen entera, y recibe el job_id."""
    original_content = bytes(range(256)) * 800  # 204.800 bytes: más de tres bloques

    async def respond_with_a_new_job(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": "a3f7b2c1-9e4d-4b8a-b3c7-1f2e5d8a9c40",
            "status": messages.QUEUED,
            "deduplicated": False,
        })

    fake_server = FakeServer(respond_with_a_new_job)
    client_session = await connected_session(fake_server)

    image = tmp_path / "foto.jpg"
    image.write_bytes(original_content)

    response = await client_session.submit(
        image, "anonymize", {"mode": "blur", "strength": 15}
    )

    assert response["job_id"] == "a3f7b2c1-9e4d-4b8a-b3c7-1f2e5d8a9c40"
    assert response["status"] == messages.QUEUED
    assert response["deduplicated"] is False

    sent_request = fake_server.requests[0]
    assert sent_request[messages.TYPE_FIELD] == messages.SUBMIT
    assert sent_request["user"] == "augusto"
    assert sent_request["op"] == "anonymize"
    assert sent_request["params"] == {"mode": "blur", "strength": 15}
    assert sent_request["filename"] == "foto.jpg"
    assert protocol.payload_size_of(sent_request) == len(original_content)

    # La imagen llegó completa y sin alterarse, aunque viajó en varios bloques.
    assert fake_server.payloads[0] == original_content

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_submit_reports_its_progress(tmp_path: Path) -> None:
    """El avance se informa varias veces y termina en el total exacto del archivo."""

    async def accept_and_confirm(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK, "job_id": "x", "status": messages.QUEUED
        })

    fake_server = FakeServer(accept_and_confirm)
    client_session = await connected_session(fake_server)

    image = tmp_path / "foto.jpg"
    image.write_bytes(b"\x00" * (protocol.CHUNK_SIZE * 3 + 17))

    reported: list[tuple[int, int]] = []
    await client_session.submit(image, "clean", {}, on_progress=lambda sent, total: reported.append((sent, total)))

    assert len(reported) == 4  # tres bloques enteros y el resto
    assert reported[-1] == (protocol.CHUNK_SIZE * 3 + 17, protocol.CHUNK_SIZE * 3 + 17)
    assert [sent for sent, _ in reported] == sorted(sent for sent, _ in reported)

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_a_deduplicated_submit_returns_the_previous_job(tmp_path: Path) -> None:
    """Cuando el contenido ya fue procesado, llega el job_id anterior y ya terminado."""

    async def respond_with_a_duplicate(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": "b8e1d4f2-3c7a-4e91-8d25-6b0f3a1c9e47",
            "status": messages.DONE,
            "deduplicated": True,
        })

    fake_server = FakeServer(respond_with_a_duplicate)
    client_session = await connected_session(fake_server)

    image = tmp_path / "repetida.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0")

    response = await client_session.submit(image, "compress", {"quality": 80})

    assert response["deduplicated"] is True
    assert response["status"] == messages.DONE

    await client_session.close()
    await fake_server.stop()


# ──────────────────────────────── errores ────────────────────────────────


@pytest.mark.asyncio
async def test_an_error_response_becomes_an_exception_with_its_code(tmp_path: Path) -> None:
    """Una respuesta de tipo error se convierte en ServerError conservando el código."""

    async def reject_the_image(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.ERROR,
            "code": messages.INVALID_IMAGE,
            "message": "el archivo no se pudo decodificar",
        })

    fake_server = FakeServer(reject_the_image)
    client_session = await connected_session(fake_server)

    image = tmp_path / "rota.jpg"
    image.write_bytes(b"esto no es una imagen")

    with pytest.raises(messages.ServerError) as raised:
        await client_session.submit(image, "anonymize", {})

    assert raised.value.code == messages.INVALID_IMAGE
    assert "decodificar" in raised.value.message

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_an_error_without_message_falls_back_to_the_standard_explanation() -> None:
    """Si el servidor no manda texto, se usa la explicación estándar del código."""

    async def reject_without_explaining(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.ERROR, "code": messages.FORBIDDEN
        })

    fake_server = FakeServer(reject_without_explaining)
    client_session = await connected_session(fake_server)

    with pytest.raises(messages.ServerError) as raised:
        await client_session.status("a3f7b2c1")

    assert raised.value.code == messages.FORBIDDEN
    assert raised.value.message == messages.ERROR_EXPLANATIONS[messages.FORBIDDEN]

    await client_session.close()
    await fake_server.stop()


# ─────────────────────────────── descarga ───────────────────────────────


@pytest.mark.asyncio
async def test_download_writes_the_file_to_disk(tmp_path: Path) -> None:
    """El resultado llega completo y queda escrito en la ruta pedida."""
    result_content = bytes(range(256)) * 500  # 128.000 bytes

    async def send_the_result(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": "a3f7b2c1",
            "filename": "foto_anonymized.jpg",
            "content_type": "image/jpeg",
        }, result_content)

    fake_server = FakeServer(send_the_result)
    client_session = await connected_session(fake_server)

    destination = tmp_path / "salida" / "resultado.jpg"
    output_path, response = await client_session.download("a3f7b2c1", destination)

    assert output_path == destination
    assert destination.read_bytes() == result_content
    assert response["filename"] == "foto_anonymized.jpg"

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_a_cut_download_leaves_no_partial_file(tmp_path: Path) -> None:
    """Si la transferencia se corta, no queda un archivo truncado que parezca válido."""

    async def announce_more_than_it_sends(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        # Anuncia 5000 bytes, manda 10 y cierra.
        writer.write(protocol.pack_header({
            messages.TYPE_FIELD: messages.OK, "job_id": "a3f7b2c1",
            protocol.PAYLOAD_SIZE_FIELD: 5000,
        }))
        writer.write(b"0123456789")
        await writer.drain()
        writer.close()

    fake_server = FakeServer(announce_more_than_it_sends)
    client_session = await connected_session(fake_server)

    destination = tmp_path / "truncado.jpg"

    with pytest.raises(asyncio.IncompleteReadError):
        await client_session.download("a3f7b2c1", destination)

    assert not destination.exists()

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_download_without_destination_uses_the_suggested_name(tmp_path: Path) -> None:
    """Sin ruta de salida se usa el nombre que sugiere el servidor."""

    async def send_a_named_result(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": "a3f7b2c1",
            "filename": "sugerido.webp",
        }, b"contenido")

    fake_server = FakeServer(send_a_named_result)
    client_session = await connected_session(fake_server)

    output_path, _ = await client_session.download(
        "a3f7b2c1", tmp_path / "sugerido.webp"
    )

    assert output_path.name == "sugerido.webp"
    assert output_path.read_bytes() == b"contenido"

    await client_session.close()
    await fake_server.stop()


# ──────────────────────────────── historial ────────────────────────────────


@pytest.mark.asyncio
async def test_history_returns_the_list_of_jobs() -> None:
    """El historial llega como lista de trabajos y se devuelve tal cual."""

    async def send_two_jobs(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await fake_server.read_request(reader)
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "jobs": [
                {"job_id": "a3f7b2c1", "op": "anonymize", "status": messages.DONE},
                {"job_id": "b8e1d4f2", "op": "compress", "status": messages.FAILED},
            ],
        })

    fake_server = FakeServer(send_two_jobs)
    client_session = await connected_session(fake_server)

    jobs = await client_session.history(limit=10)

    assert len(jobs) == 2
    assert jobs[0]["op"] == "anonymize"
    assert fake_server.requests[0]["limit"] == 10

    await client_session.close()
    await fake_server.stop()


# ────────────────────────── espera del resultado ──────────────────────────


@pytest.mark.asyncio
async def test_wait_stops_as_soon_as_the_job_finishes() -> None:
    """La espera consulta hasta que el estado es terminal, y ahí corta."""
    states = [messages.QUEUED, messages.PROCESSING, messages.DONE]

    async def walk_through_the_states(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        for state in states:
            await fake_server.read_request(reader)
            await protocol.send_message(writer, {
                messages.TYPE_FIELD: messages.OK,
                "job_id": "a3f7b2c1",
                "status": state,
                "has_output": state == messages.DONE,
            })

    fake_server = FakeServer(walk_through_the_states)
    client_session = await connected_session(fake_server)

    observed: list[str] = []
    final_response = await client_session.wait_until_finished(
        "a3f7b2c1", on_poll=lambda response, _: observed.append(response["status"])
    )

    assert observed == states
    assert final_response["status"] == messages.DONE
    assert len(fake_server.requests) == 3

    await client_session.close()
    await fake_server.stop()


@pytest.mark.asyncio
async def test_wait_gives_up_on_timeout_without_cancelling_anything() -> None:
    """Al agotarse el tiempo devuelve el último estado, que no es terminal."""

    async def never_finish(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            try:
                await fake_server.read_request(reader)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                return
            await protocol.send_message(writer, {
                messages.TYPE_FIELD: messages.OK,
                "job_id": "a3f7b2c1",
                "status": messages.PROCESSING,
            })

    fake_server = FakeServer(never_finish)
    client_session = await connected_session(fake_server)

    final_response = await client_session.wait_until_finished("a3f7b2c1", timeout=0)

    assert final_response["status"] == messages.PROCESSING
    assert final_response["status"] not in messages.TERMINAL_STATUSES

    await client_session.close()
    await fake_server.stop()


# ─────────────────────── reglas del diálogo ───────────────────────


@pytest.mark.asyncio
async def test_using_a_session_before_connecting_is_a_programming_error() -> None:
    """Enviar sin haber conectado avisa claramente, en vez de fallar de forma oscura."""
    client_session = session.ClientSession("127.0.0.1", 9000, "augusto")

    with pytest.raises(RuntimeError, match="no está conectada"):
        await client_session.status("a3f7b2c1")
