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
        server = ImageServer(host, 0)
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



    async def test_a_request_without_a_handler_yet_says_so(self) -> None:
        """Un pedido que el protocolo define pero el servidor todavía no atiende.

        Es el estado esperado de esta etapa: el tipo se reconoce —no es `BAD_REQUEST`—
        pero su lógica llega cuando se aprueben los componentes que necesita.
        """
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.status("a3f7b2c1")

        self.assertEqual(raised.exception.code, messages.INTERNAL)
        self.assertIn("status", raised.exception.message)



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


    async def test_the_payload_is_consumed_even_when_the_request_is_rejected(self) -> None:
        """Tras rechazar un pedido con imagen, el mensaje siguiente se lee correctamente.

        Es la prueba más importante de esta etapa. Si el servidor no consumiera el payload de
        un pedido que rechaza, esos bytes quedarían en el socket y el `receive_header` del
        mensaje siguiente los tomaría como su prefijo de longitud: el diálogo quedaría
        desfasado y ninguno de los dos extremos podría detectarlo.
        """
        server = await self.running_server()
        client = await self.connected_client(server)

        image = self.working_directory / "foto.jpg"
        image.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 200_000)  # más de tres bloques

        # Primer pedido: lleva 200 KB de payload y el servidor lo rechaza.
        with self.assertRaises(messages.ServerError):
            await client.submit(image, "inspect", {})

        # Segundo pedido sobre la misma conexión: tiene que entenderse bien.
        with self.assertRaises(messages.ServerError) as raised:
            await client.status("a3f7b2c1")

        self.assertEqual(raised.exception.code, messages.INTERNAL)
        self.assertIn("status", raised.exception.message)



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
