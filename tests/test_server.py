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
from app.common import messages, protocol
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

    async def _close_everything(self) -> None:
        """Cierra los clientes y los servidores que la prueba haya levantado."""
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



    async def test_a_known_request_is_answered_as_not_implemented_yet(self) -> None:
        """Los cuatro pedidos del protocolo se reconocen, pero todavía no se atienden.

        Es el estado esperado de esta etapa: el framing y el despacho funcionan; la lógica de
        cada pedido llega cuando el profesor apruebe los componentes que necesita.
        """
        server = await self.running_server()
        client = await self.connected_client(server)

        with self.assertRaises(messages.ServerError) as raised:
            await client.history(limit=10)

        self.assertEqual(raised.exception.code, messages.INTERNAL)
        self.assertIn("history", raised.exception.message)



    async def test_an_unknown_request_type_is_rejected(self) -> None:
        """Un tipo de mensaje que no existe se rechaza con BAD_REQUEST."""
        server = await self.running_server()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.listening_port)

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
        _, bad_writer = await asyncio.open_connection("127.0.0.1", server.listening_port)
        absurd_size = protocol.MAX_HEADER_SIZE + 1
        bad_writer.write(protocol.encode_length(absurd_size))
        await bad_writer.drain()
        await asyncio.sleep(0.05)
        bad_writer.close()
        await bad_writer.wait_closed()

        # El servidor sigue atendiendo con normalidad.
        good_client = await self.connected_client(server)
        with self.assertRaises(messages.ServerError):
            await good_client.history(limit=1)



    async def test_a_client_that_disappears_does_not_affect_the_others(self) -> None:
        """Si un cliente se corta de golpe, los demás siguen atendidos."""
        server = await self.running_server()

        abandoned_client = await self.connected_client(server)
        surviving_client = await self.connected_client(server)

        # Se cierra sin avisar, en medio de la sesión.
        abandoned_client._writer.transport.abort()  # type: ignore[union-attr]
        await asyncio.sleep(0.05)

        with self.assertRaises(messages.ServerError):
            await surviving_client.history(limit=1)

        self.assertEqual(server.connected_clients, 1)





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
            reader, writer = await asyncio.open_connection(loopback_of[listening.family], port)
            await protocol.send_message(writer, {
                messages.TYPE_FIELD: messages.HISTORY, "user": "augusto", "limit": 1
            })
            response = await protocol.receive_header(reader)

            # Todavía no implementado.

            self.assertEqual(response[messages.TYPE_FIELD], messages.ERROR)
            writer.close()
            await writer.wait_closed()



    async def test_a_concrete_address_listens_on_a_single_family(self) -> None:
        """Al indicar una dirección, se escucha solo en la familia de esa dirección."""
        server = await self.running_server(host="127.0.0.1")

        families = {listening.family for listening in server._server.sockets}  # type: ignore[union-attr]
        self.assertEqual(families, {socket.AF_INET})


if __name__ == "__main__":
    unittest.main()
