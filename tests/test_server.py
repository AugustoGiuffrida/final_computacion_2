"""Pruebas del proceso principal del servidor.

Levantan el servidor de verdad en un puerto libre y lo interrogan con la misma
`ClientSession` que usa el cliente real. No se simula nada: los mensajes viajan por un
socket TCP y atraviesan el framing completo en las dos direcciones.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.client import session
from app.common import config, messages, protocol
from app.server import registry
from app.server.server import ImageServer


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    """Base con un directorio temporal y el montaje de servidores y clientes.

    Todo lo que se levanta queda registrado para cerrarse al terminar la prueba.

    Attributes:
        working_directory: Directorio temporal propio de cada prueba.
    """

    def setUp(self) -> None:
        """Crea el directorio temporal y las listas de recursos a liberar."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)
        self._servers: list[ImageServer] = []
        self._clients: list[session.ClientSession] = []
        self._writers: list[asyncio.StreamWriter] = []

    def tearDown(self) -> None:
        """Borra el directorio temporal."""
        self._temporary_directory.cleanup()

    async def running_server(self, host: str | None = "127.0.0.1") -> ImageServer:
        """Levanta un servidor en un puerto libre.

        Args:
            host: Dirección de escucha. `None` para todas las interfaces.

        Returns:
            El servidor ya escuchando.
        """
        server = ImageServer(host, 0, storage_dir=self.working_directory)
        await server.start()

        self._servers.append(server)
        self.addAsyncCleanup(self._close_everything)

        return server

    async def connected_client(self, server: ImageServer) -> session.ClientSession:
        """Conecta una sesión de cliente al servidor indicado.

        Args:
            server: Servidor ya escuchando.

        Returns:
            La sesión conectada.
        """
        client = session.ClientSession("127.0.0.1", server.listening_port, "augusto")
        await client.connect()

        self._clients.append(client)
        return client

    async def open_raw_connection(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Abre una conexión sin pasar por `ClientSession`, para hablar el protocolo a mano.

        Registra el writer para cerrarlo al terminar. Si una prueba dejara una conexión
        abierta, el `wait_closed` del servidor esperaría para siempre a que su handler
        termine, y una aserción fallida colgaría toda la suite en vez de reportarse.

        Args:
            host: Dirección del servidor.
            port: Puerto del servidor.

        Returns:
            Los dos extremos de la conexión.
        """
        reader, writer = await asyncio.open_connection(host, port)
        self._writers.append(writer)
        return reader, writer

    async def _close_everything(self) -> None:
        """Cierra las conexiones, los clientes y los servidores que la prueba levantó."""
        while self._writers:
            writer = self._writers.pop()
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError):
                pass  # el otro extremo ya había cerrado

        while self._clients:
            await self._clients.pop().close()
        while self._servers:
            await self._servers.pop().stop()

        # Una vuelta más del event loop para que los transportes terminen de cerrarse.
        await asyncio.sleep(0)




# ──────────────────────── ciclo de vida ────────────────────────


class Lifecycle(ServerTestCase):
    """Arranque, conteo de conexiones y detención del servidor."""



    async def test_the_server_accepts_a_connection(self) -> None:
        """Un cliente puede conectarse a un servidor recién levantado."""
        server = await self.running_server()
        client = await self.connected_client(server)

        self.assertTrue(client.is_connected)



    async def test_the_server_counts_its_connected_clients(self) -> None:
        """El contador de conexiones sube al conectarse y baja al desconectarse."""
        server = await self.running_server()

        first_client = await self.connected_client(server)
        second_client = await self.connected_client(server)
        await asyncio.sleep(0.05)  # las tareas de los handlers ya arrancaron

        self.assertEqual(server.connected_clients, 2)

        await first_client.close()
        await second_client.close()
        await asyncio.sleep(0.05)

        self.assertEqual(server.connected_clients, 0)



    async def test_stopping_the_server_closes_its_listening_socket(self) -> None:
        """Después de detenerlo, nadie más puede conectarse."""
        server = await self.running_server()
        port = server.listening_port

        await server.stop()

        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)




# ──────────────────────── atención de pedidos ────────────────────────


class RequestHandling(ServerTestCase):
    """Lectura y despacho de los pedidos del protocolo."""



    async def test_an_unknown_request_type_is_rejected(self) -> None:
        """Un tipo de mensaje que no existe se rechaza con BAD_REQUEST."""
        server = await self.running_server()
        reader, writer = await self.open_raw_connection("127.0.0.1", server.listening_port)

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: "borrar_todo", "user": "augusto"
        })
        response = await protocol.receive_header(reader)

        self.assertEqual(response[messages.TYPE_FIELD], messages.ERROR)
        self.assertEqual(response["code"], messages.BAD_REQUEST)

        writer.close()
        await writer.wait_closed()


    async def test_the_payload_of_a_rejected_request_is_consumed(self) -> None:
        """Un pedido rechazado que traiga payload no desfasa el diálogo.

        Los bytes ya vienen en camino, así que hay que sacarlos del socket aunque el
        pedido se rechace. Si quedaran ahí, el `receive_header` siguiente los tomaría
        como su prefijo de longitud y ninguno de los dos extremos podría detectarlo.
        """
        server = await self.running_server()
        reader, writer = await self.open_raw_connection(
            "127.0.0.1", server.listening_port
        )

        # Un `status` de un trabajo inexistente, y encima con un payload que no le
        # corresponde llevar.
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.STATUS,
            "user": "augusto",
            "job_id": "no-existe",
        }, b"x" * 200_000)
        rejected = await protocol.receive_header(reader)
        self.assertEqual(rejected["code"], messages.JOB_NOT_FOUND)

        # El pedido siguiente sobre la misma conexión tiene que entenderse bien.
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.HISTORY, "user": "augusto"
        })
        accepted = await protocol.receive_header(reader)

        self.assertEqual(accepted[messages.TYPE_FIELD], messages.OK)

    async def test_several_requests_travel_over_one_connection(self) -> None:
        """Una conexión transporta varios pedidos seguidos, como manda el protocolo."""
        server = await self.running_server()
        client = await self.connected_client(server)

        for _ in range(5):
            with self.assertRaises(messages.ServerError):
                await client.status("a3f7b2c1")

        self.assertTrue(client.is_connected)





# ──────────────────────── robustez ────────────────────────


class Robustness(ServerTestCase):
    """Un cliente que falla no afecta a los demás."""



    async def test_garbage_does_not_bring_down_the_server(self) -> None:
        """Un cliente que manda basura recibe un error y no afecta a los demás."""
        server = await self.running_server()

        # Un cliente envía un prefijo que anuncia un header disparatado.
        _, bad_writer = await self.open_raw_connection("127.0.0.1", server.listening_port)
        absurd_size = protocol.MAX_HEADER_SIZE + 1
        bad_writer.write(protocol.encode_length(absurd_size))
        await bad_writer.drain()
        await asyncio.sleep(0.05)
        bad_writer.close()
        await bad_writer.wait_closed()

        # El servidor sigue atendiendo con normalidad.
        good_client = await self.connected_client(server)
        self.assertEqual(await good_client.history(limit=1), [])



    async def test_a_client_that_disappears_does_not_affect_the_others(self) -> None:
        """Si un cliente se corta de golpe, los demás siguen atendidos."""
        server = await self.running_server()

        abandoned_client = await self.connected_client(server)
        surviving_client = await self.connected_client(server)

        # Se cierra sin avisar, en medio de la sesión.
        abandoned_client._writer.transport.abort()  # type: ignore[union-attr]
        await asyncio.sleep(0.05)

        self.assertEqual(await surviving_client.history(limit=1), [])

        self.assertEqual(server.connected_clients, 1)





# ──────────────────────── historial ────────────────────────


class HistoryRequest(ServerTestCase):
    """El pedido `history`: listado de los trabajos del usuario."""

    async def test_a_user_without_jobs_gets_an_empty_list(self) -> None:
        """No tener trabajos no es un error: la respuesta es una lista vacía."""
        server = await self.running_server()
        client = await self.connected_client(server)

        self.assertEqual(await client.history(limit=10), [])

    async def test_only_the_jobs_of_that_user_are_listed(self) -> None:
        """Cada usuario ve los suyos y ninguno más."""
        server = await self.running_server()
        server.jobs.add(registry.new_job("augusto", "anonymize", {}, "foto.jpg"))
        server.jobs.add(registry.new_job("ana", "compress", {}, "otra.png"))

        client = await self.connected_client(server)
        listed = await client.history(limit=10)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["op"], "anonymize")

    async def test_the_most_recent_job_comes_first(self) -> None:
        """El historial llega ordenado de lo más nuevo a lo más viejo."""
        server = await self.running_server()
        for operation in ("anonymize", "clean", "compress"):
            server.jobs.add(registry.new_job("augusto", operation, {}, "foto.jpg"))

        client = await self.connected_client(server)
        listed = await client.history(limit=10)

        self.assertEqual(
            [job["op"] for job in listed], ["compress", "clean", "anonymize"]
        )

    async def test_the_limit_is_respected(self) -> None:
        """Se devuelven a lo sumo tantos trabajos como se hayan pedido."""
        server = await self.running_server()
        for _ in range(5):
            server.jobs.add(registry.new_job("augusto", "clean", {}, "foto.jpg"))

        client = await self.connected_client(server)

        self.assertEqual(len(await client.history(limit=2)), 2)

    async def test_an_oversized_limit_is_trimmed_instead_of_rejected(self) -> None:
        """Pedir más de la cuenta no es un error del cliente: se recorta al tope."""
        server = await self.running_server()
        for _ in range(config.MAX_HISTORY_LIMIT + 10):
            server.jobs.add(registry.new_job("augusto", "clean", {}, "foto.jpg"))

        client = await self.connected_client(server)
        listed = await client.history(limit=100_000)

        self.assertEqual(len(listed), config.MAX_HISTORY_LIMIT)

    async def test_an_invalid_limit_is_rejected(self) -> None:
        """Un límite negativo o que no sea un entero no tiene interpretación razonable."""
        server = await self.running_server()

        for invalid_limit in (0, -3, "muchos", True):
            with self.subTest(limit=invalid_limit):
                reader, writer = await self.open_raw_connection(
                    "127.0.0.1", server.listening_port
                )
                await protocol.send_message(writer, {
                    messages.TYPE_FIELD: messages.HISTORY,
                    "user": "augusto",
                    "limit": invalid_limit,
                })
                response = await protocol.receive_header(reader)

                self.assertEqual(response[messages.TYPE_FIELD], messages.ERROR)
                self.assertEqual(response["code"], messages.BAD_REQUEST)

    async def test_a_request_without_a_user_is_rejected(self) -> None:
        """Todo pedido declara de quién es: sin eso no se puede saber qué listar."""
        server = await self.running_server()
        reader, writer = await self.open_raw_connection(
            "127.0.0.1", server.listening_port
        )

        await protocol.send_message(writer, {messages.TYPE_FIELD: messages.HISTORY})
        response = await protocol.receive_header(reader)

        self.assertEqual(response["code"], messages.BAD_REQUEST)
        self.assertIn("user", response["message"])

    async def test_a_rejected_request_does_not_close_the_connection(self) -> None:
        """Tras un rechazo se puede seguir pidiendo por la misma conexión.

        Es la regla del protocolo: un error es una respuesta más. Si cortara, el cliente
        no podría distinguir un pedido inválido de una caída del servidor.
        """
        server = await self.running_server()
        reader, writer = await self.open_raw_connection(
            "127.0.0.1", server.listening_port
        )

        await protocol.send_message(writer, {messages.TYPE_FIELD: messages.HISTORY})
        rejected = await protocol.receive_header(reader)
        self.assertEqual(rejected[messages.TYPE_FIELD], messages.ERROR)

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.HISTORY, "user": "augusto"
        })
        accepted = await protocol.receive_header(reader)

        self.assertEqual(accepted[messages.TYPE_FIELD], messages.OK)


# ──────────────────────── envío de imágenes ────────────────────────


class SubmitRequest(ServerTestCase):
    """El pedido `submit`: recibir una imagen y registrar el trabajo."""

    def an_image(self, name: str = "foto.jpg", size: int = 200_000) -> Path:
        """Crea un archivo de prueba del tamaño pedido.

        Args:
            name: Nombre del archivo.
            size: Cuántos bytes escribirle.

        Returns:
            La ruta del archivo creado.
        """
        image = self.working_directory / name
        image.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * (size - 4))
        return image

    async def test_an_accepted_image_returns_a_new_job(self) -> None:
        """Un envío válido devuelve un identificador y el trabajo queda encolado."""
        server = await self.running_server()
        client = await self.connected_client(server)

        response = await client.submit(self.an_image(), "anonymize", {"mode": "blur"})

        self.assertEqual(response["status"], messages.QUEUED)
        self.assertFalse(response["deduplicated"])
        self.assertEqual(len(server.jobs), 1)

    async def test_the_image_is_written_to_disk_intact(self) -> None:
        """La imagen llega completa y sin alterarse, aunque viaje en varios bloques."""
        server = await self.running_server()
        client = await self.connected_client(server)
        image = self.an_image(size=200_000)  # más de tres bloques

        response = await client.submit(image, "clean", {})

        stored = server.uploads_dir / response["job_id"] / "foto.jpg"
        self.assertEqual(stored.read_bytes(), image.read_bytes())

    async def test_the_job_appears_in_the_history(self) -> None:
        """Lo que se envía se puede listar después: los dos pedidos ven lo mismo."""
        server = await self.running_server()
        client = await self.connected_client(server)

        await client.submit(self.an_image(), "compress", {"quality": 80})
        listed = await client.history(limit=10)

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["op"], "compress")
        self.assertEqual(listed[0]["filename"], "foto.jpg")

    async def test_each_submit_gets_its_own_directory(self) -> None:
        """Dos envíos no se pisan, aunque el archivo se llame igual."""
        server = await self.running_server()
        client = await self.connected_client(server)

        first = await client.submit(self.an_image(), "clean", {})
        second = await client.submit(self.an_image(), "clean", {})

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(list(server.uploads_dir.iterdir())), 2)

    # ─────────────── pedidos que se rechazan ───────────────

    async def test_an_unknown_operation_is_rejected(self) -> None:
        """Una operación que no existe se rechaza con su propio código."""
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.submit(self.an_image(), "destruir", {})

        self.assertEqual(raised.exception.code, messages.UNKNOWN_OP)
        self.assertEqual(len(server.jobs), 0)

    async def test_a_parameter_of_another_operation_is_rejected(self) -> None:
        """Pasar `mode` a `clean` es una confusión, y silenciarla haría creer que se aplicó."""
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.submit(self.an_image(), "clean", {"mode": "blur"})

        self.assertEqual(raised.exception.code, messages.BAD_REQUEST)

    async def test_an_image_over_the_limit_is_rejected_before_being_read(self) -> None:
        """Una imagen demasiado grande se rechaza sin escribir nada en disco."""
        server = await self.running_server()
        reader, writer = await self.open_raw_connection(
            "127.0.0.1", server.listening_port
        )

        # Se anuncia un payload enorme sin llegar a enviarlo.
        writer.write(protocol.pack_header({
            messages.TYPE_FIELD: messages.SUBMIT,
            "user": "augusto",
            "op": "clean",
            "params": {},
            "filename": "enorme.jpg",
            protocol.PAYLOAD_SIZE_FIELD: config.DEFAULT_MAX_IMAGE_SIZE + 1,
        }))
        await writer.drain()
        response = await protocol.receive_header(reader)

        self.assertEqual(response["code"], messages.TOO_LARGE)
        self.assertEqual(len(list(server.uploads_dir.iterdir())), 0)

    async def test_a_rejected_submit_does_not_desynchronise_the_dialogue(self) -> None:
        """Tras rechazar un envío con imagen, el pedido siguiente se entiende bien.

        El payload de un pedido rechazado igual hay que consumirlo: si quedara en el
        socket, el `receive_header` siguiente lo tomaría como su prefijo de longitud.
        """
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError):
            await client.submit(self.an_image(), "destruir", {})

        self.assertEqual(await client.history(limit=10), [])

    # ─────────────── seguridad ───────────────

    async def submit_with_filename(
        self, server: ImageServer, filename: str
    ) -> dict[str, Any]:
        """Envía una imagen declarando un nombre arbitrario, sin usar `ClientSession`.

        Hace falta hablar el protocolo a mano porque el cliente propio siempre manda el
        nombre real del archivo. Un cliente hostil no tiene esa limitación, y es
        justamente el caso que hay que probar.

        Args:
            server: El servidor al que enviar.
            filename: El nombre a declarar en el header.

        Returns:
            El header de la respuesta.
        """
        content = b"\xff\xd8\xff\xe0" + b"x" * 1000
        reader, writer = await self.open_raw_connection(
            "127.0.0.1", server.listening_port
        )

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.SUBMIT,
            "user": "augusto",
            "op": "clean",
            "params": {},
            "filename": filename,
        }, content)

        return await protocol.receive_header(reader)

    async def test_a_filename_cannot_escape_its_directory(self) -> None:
        """Un nombre con partes de ruta no puede escribir fuera del trabajo.

        El nombre lo elige el cliente: sin sanearlo, un valor como '../../evil.jpg'
        construiría una ruta fuera del directorio del trabajo. Se conserva solo el último
        componente, así que el archivo cae adentro y con el nombre a secas.
        """
        server = await self.running_server()

        response = await self.submit_with_filename(server, "../../../evil.jpg")

        job_directory = server.uploads_dir / response["job_id"]
        self.assertEqual([f.name for f in job_directory.iterdir()], ["evil.jpg"])
        self.assertFalse((self.working_directory.parent / "evil.jpg").exists())

    async def test_an_unsupported_extension_is_rejected(self) -> None:
        """Un archivo que no dice ser una imagen se rechaza en la puerta."""
        server = await self.running_server()

        response = await self.submit_with_filename(server, "script.sh")

        self.assertEqual(response["code"], messages.INVALID_IMAGE)

# ──────────────────────── consulta y descarga ────────────────────────


class StatusRequest(ServerTestCase):
    """El pedido `status`: en qué anda un trabajo."""

    async def test_a_queued_job_reports_its_state(self) -> None:
        """Un trabajo recién aceptado figura encolado, sin datos de resultado."""
        server = await self.running_server()
        job = registry.new_job("augusto", "clean", {}, "foto.jpg")
        server.jobs.add(job)

        client = await self.connected_client(server)
        response = await client.status(job.job_id)

        self.assertEqual(response["status"], messages.QUEUED)
        self.assertNotIn("has_output", response)
        self.assertNotIn("result", response)

    async def test_a_finished_job_reports_what_it_produced(self) -> None:
        """Al terminar, la respuesta dice si hay archivo y trae los datos de la operación."""
        server = await self.running_server()
        job = registry.new_job("augusto", "anonymize", {}, "foto.jpg")
        job.status = messages.DONE
        job.output_path = self.working_directory / "out.jpg"
        job.result = {"faces_detected": 3}
        server.jobs.add(job)

        client = await self.connected_client(server)
        response = await client.status(job.job_id)

        self.assertEqual(response["status"], messages.DONE)
        self.assertTrue(response["has_output"])
        self.assertEqual(response["result"], {"faces_detected": 3})

    async def test_an_operation_without_output_says_so(self) -> None:
        """`inspect` termina bien pero no deja archivo: el cliente no debe pedir descarga."""
        server = await self.running_server()
        job = registry.new_job("augusto", "inspect", {}, "foto.jpg")
        job.status = messages.DONE
        job.result = {"gps": {"lat": -32.889, "lon": -68.845}}
        server.jobs.add(job)

        client = await self.connected_client(server)
        response = await client.status(job.job_id)

        self.assertFalse(response["has_output"])
        self.assertIn("gps", response["result"])

    async def test_a_failed_job_reports_the_reason(self) -> None:
        """Si falló, viaja el motivo en lugar de los datos del resultado."""
        server = await self.running_server()
        job = registry.new_job("augusto", "clean", {}, "rota.jpg")
        job.status = messages.FAILED
        job.error = "imagen corrupta"
        server.jobs.add(job)

        client = await self.connected_client(server)
        response = await client.status(job.job_id)

        self.assertEqual(response["error"], "imagen corrupta")

    async def test_an_unknown_job_is_not_found(self) -> None:
        """Un identificador inventado no existe."""
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.status("no-existe")

        self.assertEqual(raised.exception.code, messages.JOB_NOT_FOUND)

    async def test_a_job_of_another_user_is_forbidden(self) -> None:
        """La regla de propiedad se aplica también al consultar."""
        server = await self.running_server()
        job = registry.new_job("ana", "clean", {}, "foto.jpg")
        server.jobs.add(job)

        client = await self.connected_client(server)  # se conecta como augusto

        with self.assertRaises(messages.ServerError) as raised:
            await client.status(job.job_id)

        self.assertEqual(raised.exception.code, messages.FORBIDDEN)


class DownloadRequest(ServerTestCase):
    """El pedido `download`: obtener el archivo que produjo un trabajo."""

    def a_finished_job(self, content: bytes = b"resultado") -> registry.Job:
        """Deja un trabajo terminado, con su archivo de salida ya escrito.

        Hace falta inyectarlo porque nada procesa los trabajos todavía: sin workers,
        ninguno llega por sí solo a `DONE`.

        Args:
            content: Bytes a escribir en el archivo de salida.

        Returns:
            El trabajo terminado, listo para agregar al registro.
        """
        output = self.working_directory / "out.jpg"
        output.write_bytes(content)

        job = registry.new_job("augusto", "anonymize", {}, "paisaje.jpg")
        job.status = messages.DONE
        job.output_path = output
        return job

    async def test_the_file_arrives_intact(self) -> None:
        """El archivo llega completo, aunque no entre en un solo bloque."""
        original = bytes(range(256)) * 1500  # 384.000 bytes: casi seis bloques
        server = await self.running_server()
        job = self.a_finished_job(original)
        server.jobs.add(job)

        client = await self.connected_client(server)
        destination = self.working_directory / "descargado.jpg"
        saved_path, _response = await client.download(job.job_id, destination)

        self.assertEqual(saved_path.read_bytes(), original)

    async def test_the_suggested_name_says_where_it_came_from(self) -> None:
        """El nombre sugerido combina el original y la operación aplicada."""
        server = await self.running_server()
        job = self.a_finished_job()
        server.jobs.add(job)

        client = await self.connected_client(server)
        _path, response = await client.download(
            job.job_id, self.working_directory / "x.jpg"
        )

        self.assertEqual(response["filename"], "paisaje_anonymize.jpg")
        self.assertEqual(response["content_type"], "image/jpeg")

    async def test_a_job_still_running_is_not_ready(self) -> None:
        """Un trabajo sin terminar no tiene nada para descargar."""
        server = await self.running_server()
        job = registry.new_job("augusto", "clean", {}, "foto.jpg")
        server.jobs.add(job)

        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.download(job.job_id, self.working_directory / "x.jpg")

        self.assertEqual(raised.exception.code, messages.NOT_READY)

    async def test_an_operation_without_output_cannot_be_downloaded(self) -> None:
        """`inspect` termina bien pero no genera archivo."""
        server = await self.running_server()
        job = registry.new_job("augusto", "inspect", {}, "foto.jpg")
        job.status = messages.DONE
        server.jobs.add(job)

        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.download(job.job_id, self.working_directory / "x.jpg")

        self.assertEqual(raised.exception.code, messages.NO_OUTPUT)

    async def test_a_failed_job_has_nothing_to_download(self) -> None:
        """Un trabajo que falló terminó, pero no produjo ningún archivo."""
        server = await self.running_server()
        job = registry.new_job("augusto", "clean", {}, "rota.jpg")
        job.status = messages.FAILED
        job.error = "imagen corrupta"
        server.jobs.add(job)

        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.download(job.job_id, self.working_directory / "x.jpg")

        self.assertEqual(raised.exception.code, messages.NO_OUTPUT)
        self.assertIn("imagen corrupta", raised.exception.message)

    async def test_a_missing_result_file_is_reported_clearly(self) -> None:
        """El trabajo figura terminado pero su archivo ya no está.

        Pasa cuando la limpieza periódica borró un resultado viejo. Sin este control el
        envío fallaría con un error de sistema y el cliente vería "el servidor cortó" en
        vez de una explicación.
        """
        server = await self.running_server()
        job = self.a_finished_job()
        job.output_path.unlink()  # type: ignore[union-attr]
        server.jobs.add(job)

        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.download(job.job_id, self.working_directory / "x.jpg")

        self.assertEqual(raised.exception.code, messages.INTERNAL)

    async def test_a_job_of_another_user_cannot_be_downloaded(self) -> None:
        """La regla de propiedad se aplica también al descargar."""
        server = await self.running_server()
        job = self.a_finished_job()
        job.user = "ana"
        server.jobs.add(job)

        client = await self.connected_client(server)  # se conecta como augusto

        with self.assertRaises(messages.ServerError) as raised:
            await client.download(job.job_id, self.working_directory / "x.jpg")

        self.assertEqual(raised.exception.code, messages.FORBIDDEN)

# ──────────────────────── familias de direcciones ────────────────────────


class AddressFamilies(ServerTestCase):
    """En qué familias de direcciones queda escuchando el servidor."""



    async def test_the_server_answers_both_address_families(self) -> None:
        """Sin un host concreto se escucha en IPv4 y en IPv6, y las dos familias responden.

        Se recorre cada socket en lugar de asumir que comparten puerto: con el puerto 0 el
        sistema le asigna uno distinto a cada uno. Con un puerto fijo sí sería el mismo.
        """
        server = await self.running_server(host=None)

        listening_sockets = server._server.sockets  # type: ignore[union-attr]
        families = {listening.family for listening in listening_sockets}
        self.assertEqual(families, {socket.AF_INET, socket.AF_INET6})

        loopback_of = {socket.AF_INET: "127.0.0.1", socket.AF_INET6: "::1"}

        for listening in listening_sockets:
            port = listening.getsockname()[1]
            reader, writer = await self.open_raw_connection(loopback_of[listening.family], port)
            await protocol.send_message(writer, {
                messages.TYPE_FIELD: messages.HISTORY, "user": "augusto", "limit": 1
            })
            response = await protocol.receive_header(reader)

            self.assertEqual(response[messages.TYPE_FIELD], messages.OK)
            self.assertEqual(response["jobs"], [])



    async def test_a_concrete_address_listens_on_a_single_family(self) -> None:
        """Al indicar una dirección, se escucha solo en la familia de esa dirección."""
        server = await self.running_server(host="127.0.0.1")

        families = {listening.family for listening in server._server.sockets}  # type: ignore[union-attr]
        self.assertEqual(families, {socket.AF_INET})


if __name__ == "__main__":
    unittest.main()
