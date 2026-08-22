"""Pruebas del framing de mensajes.

Levantan un servidor de prueba sobre localhost y hacen viajar mensajes de verdad por un
socket. Es la única forma de verificar que el framing resuelve el problema real: que TCP
entrega un flujo continuo y los mensajes pueden llegar partidos de cualquier manera.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.common import protocol


class ConnectedPair(unittest.IsolatedAsyncioTestCase):
    """Base con un servidor local y los dos extremos de una conexión ya abierta.

    Attributes:
        client_reader: Stream de lectura del lado del cliente.
        client_writer: Stream de escritura del lado del cliente.
        server_reader: Stream de lectura del lado del servidor.
        server_writer: Stream de escritura del lado del servidor.
    """

    async def asyncSetUp(self) -> None:
        """Levanta el servidor y establece la conexión antes de cada prueba."""
        server_side: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_client_connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            server_side.set_result((reader, writer))

        self._server = await asyncio.start_server(on_client_connected, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]

        self.client_reader, self.client_writer = await asyncio.open_connection(
            "127.0.0.1", port
        )
        self.server_reader, self.server_writer = await server_side

    async def asyncTearDown(self) -> None:
        """Cierra los dos extremos y el servidor después de cada prueba.

        Los writers se cierran antes que el servidor: `wait_closed` espera a que no
        quede ninguna conexión abierta, y si alguna sigue viva no retorna nunca.
        """
        for writer in (self.client_writer, self.server_writer):
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.IncompleteReadError):
                pass  # el otro extremo ya había cerrado

        self._server.close()
        await self._server.wait_closed()


class MessageRoundTrip(ConnectedPair):
    """Un mensaje enviado por un extremo llega igual al otro."""

    async def test_a_message_without_payload_survives_the_round_trip(self) -> None:
        """Un header sin payload llega igual del otro lado."""
        sent_header = {"type": "status", "user": "augusto", "job_id": "a3f7b2c1"}
        await protocol.send_message(self.client_writer, sent_header)

        received_header = await protocol.receive_header(self.server_reader)

        self.assertEqual(received_header["type"], "status")
        self.assertEqual(received_header["user"], "augusto")
        self.assertEqual(protocol.payload_size_of(received_header), 0)

    async def test_two_messages_in_a_row_do_not_get_mixed(self) -> None:
        """Dos mensajes seguidos se leen como dos mensajes, no como uno solo.

        Es exactamente lo que el framing tiene que garantizar: TCP no conserva la
        separación entre envíos.
        """
        await protocol.send_message(self.client_writer, {"type": "first"}, b"aaa")
        await protocol.send_message(self.client_writer, {"type": "second"}, b"bbbb")

        first_header = await protocol.receive_header(self.server_reader)
        first_payload = await protocol.receive_payload(
            self.server_reader, protocol.payload_size_of(first_header)
        )
        second_header = await protocol.receive_header(self.server_reader)
        second_payload = await protocol.receive_payload(
            self.server_reader, protocol.payload_size_of(second_header)
        )

        self.assertEqual(first_header["type"], "first")
        self.assertEqual(first_payload, b"aaa")
        self.assertEqual(second_header["type"], "second")
        self.assertEqual(second_payload, b"bbbb")


class FileTransfer(ConnectedPair):
    """Transferencia de archivos que no entran en un solo bloque."""

    async def asyncSetUp(self) -> None:
        """Agrega un directorio temporal al montaje de la conexión."""
        await super().asyncSetUp()
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)

    async def asyncTearDown(self) -> None:
        """Borra el directorio temporal además de cerrar la conexión."""
        self._temporary_directory.cleanup()
        await super().asyncTearDown()

    async def test_a_file_larger_than_the_chunk_arrives_complete(self) -> None:
        """Un archivo que no entra en un solo bloque llega entero y sin alterarse.

        El tamaño elegido obliga a varias iteraciones del bucle de envío y de lectura,
        que es donde aparecerían los errores de contabilidad de bytes.
        """
        original_content = bytes(range(256)) * 1500  # 384.000 bytes: casi seis bloques
        source_file = self.working_directory / "imagen.jpg"
        source_file.write_bytes(original_content)
        destination_file = self.working_directory / "recibida.jpg"

        async def receive_into_file() -> None:
            header = await protocol.receive_header(self.server_reader)
            with open(destination_file, "wb") as output_file:
                async for chunk in protocol.stream_payload(
                    self.server_reader, protocol.payload_size_of(header)
                ):
                    output_file.write(chunk)

        await asyncio.gather(
            protocol.send_file(
                self.client_writer, {"type": "submit", "filename": "imagen.jpg"}, source_file
            ),
            receive_into_file(),
        )

        self.assertEqual(destination_file.read_bytes(), original_content)


class MalformedMessages(ConnectedPair):
    """Mensajes que no respetan el formato producen errores claros."""

    async def test_a_cut_in_the_middle_of_the_payload_is_reported(self) -> None:
        """Si la conexión se corta a mitad del payload, la lectura falla en vez de mentir.

        El error es el mismo que informa `receive_header` ante un corte: `readexactly` no
        devuelve nunca menos de lo pedido, así que la interrupción se detecta sola.
        """
        # Anuncia diez bytes pero envía tres y cierra.
        self.client_writer.write(
            protocol.pack_header({"type": "submit", "payload_size": 10})
        )
        self.client_writer.write(b"abc")
        await self.client_writer.drain()
        self.client_writer.close()
        await self.client_writer.wait_closed()

        header = await protocol.receive_header(self.server_reader)

        with self.assertRaises(asyncio.IncompleteReadError):
            async for _chunk in protocol.stream_payload(
                self.server_reader, protocol.payload_size_of(header)
            ):
                pass

    async def test_an_absurd_header_size_is_rejected_before_reading_it(self) -> None:
        """Un prefijo disparatado se rechaza sin intentar leer esa cantidad de bytes."""
        absurd_size = protocol.MAX_HEADER_SIZE + 1
        self.client_writer.write(protocol.encode_length(absurd_size))
        await self.client_writer.drain()

        with self.assertRaisesRegex(protocol.ProtocolError, "máximo"):
            await protocol.receive_header(self.server_reader)

    async def test_a_header_that_is_not_json_is_rejected(self) -> None:
        """Un header que no es JSON válido produce un error claro, no un crash."""
        garbage = b"esto no es json"
        self.client_writer.write(protocol.encode_length(len(garbage)))
        self.client_writer.write(garbage)
        await self.client_writer.drain()

        with self.assertRaisesRegex(protocol.ProtocolError, "JSON"):
            await protocol.receive_header(self.server_reader)


class HeaderPacking(unittest.TestCase):
    """Armado del header, sin necesidad de red."""

    def test_the_header_carries_the_declared_payload_size(self) -> None:
        """El tamaño del payload se agrega sin tocar el header original."""
        original_header = {"type": "submit"}
        packed = protocol.pack_header(
            {**original_header, protocol.PAYLOAD_SIZE_FIELD: 42}
        )

        header_size = protocol.decode_length(packed[: protocol.LENGTH_PREFIX_SIZE])
        decoded_header = json.loads(packed[protocol.LENGTH_PREFIX_SIZE :])

        self.assertEqual(header_size, len(packed) - protocol.LENGTH_PREFIX_SIZE)
        self.assertEqual(decoded_header[protocol.PAYLOAD_SIZE_FIELD], 42)
        self.assertNotIn("payload_size", original_header)  # no se modificó el original


if __name__ == "__main__":
    unittest.main()
