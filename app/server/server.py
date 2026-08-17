"""Proceso principal del servidor: atiende a los clientes y despacha sus pedidos.

Es el único punto de contacto de los clientes y **la pieza que nunca debe bloquearse**.
Sobre un solo hilo atiende N conexiones concurrentes: `asyncio.start_server` crea una
corrutina por cada cliente que llega, y todas se turnan en los `await`.

De eso se derivan las dos reglas que este módulo respeta:

- **Nada de trabajo de CPU acá adentro.** La suspensión de asyncio es cooperativa: si una
  corrutina calcula sin ceder el control, el event loop no puede atender a nadie más y se
  congelan todos los clientes conectados.
- **Cada pedido se consume completo**, incluso si se va a rechazar. Los bytes que queden
  sin leer siguen en el buffer del socket, y el mensaje siguiente los interpretaría como
  su propio prefijo de longitud: el diálogo quedaría desfasado sin forma de detectarlo.

ESTADO: primera etapa. El servidor acepta conexiones, lee los mensajes respetando el
framing y responde. Todavía no procesa ningún pedido: cada uno recibe un error de "no
implementado". Los componentes que faltan —el proceso de ingreso, la cola de tareas, la
base de datos— están diseñados en `docs/` y pendientes de aprobación.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.common import config, messages, protocol

logger = logging.getLogger(__name__)


class ImageServer:
    """Servidor TCP que atiende pedidos del protocolo de la aplicación.

    Se instancia con la configuración, se arranca con `start` y se detiene con `stop`.
    La configuración se guarda en la instancia porque `asyncio.start_server` solo le pasa
    al handler el reader y el writer de la conexión: cualquier otro dato tiene que llegarle
    por acá.

    Attributes:
        host: Dirección en la que escucha. `None` significa todas las interfaces
            disponibles, IPv4 e IPv6 a la vez (socket dual-stack).
        port: Puerto en el que escucha.
        connected_clients: Cuántas conexiones hay abiertas en este momento.
    """

    def __init__(
        self,
        host: str | None = config.LISTEN_ON_ALL_INTERFACES,
        port: int = config.DEFAULT_PORT,
    ) -> None:
        """Prepara el servidor sin abrir todavía el socket de escucha.

        Args:
            host: Dirección de escucha, o None para todas las interfaces.
            port: Puerto de escucha.
        """
        self.host = host
        self.port = port
        self.connected_clients = 0
        self._server: asyncio.Server | None = None

    # ─────────────────────── ciclo de vida ───────────────────────

    async def start(self) -> None:
        """Abre el socket de escucha y empieza a aceptar conexiones.

        `asyncio.start_server` hace por debajo la secuencia completa de un servidor TCP
        —crear el socket, reclamar la dirección con `bind`, ponerlo en modo pasivo con
        `listen`— y deja corriendo el bucle de `accept` dentro del event loop. Por cada
        conexión aceptada crea una tarea nueva que ejecuta `handle_client`.

        Returns:
            None. Al volver, el servidor ya está aceptando conexiones.

        Raises:
            OSError: Si el puerto está ocupado o la dirección no está disponible.
        """
        self._server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )

        for listening_socket in self._server.sockets:
            address = listening_socket.getsockname()
            logger.info("escuchando en %s", format_address(address))

    @property
    def listening_port(self) -> int:
        """Puerto en el que el servidor quedó escuchando.

        Hace falta cuando se arranca con el puerto 0, que le pide al sistema operativo
        que elija uno libre: recién después de abrir el socket se sabe cuál fue.

        Returns:
            El puerto asignado.

        Raises:
            RuntimeError: Si todavía no se llamó a `start`.
        """
        if self._server is None or not self._server.sockets:
            raise RuntimeError("el servidor no está escuchando: falta llamar a start()")
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Deja de aceptar conexiones y espera a que se cierren las abiertas.

        Cierra primero el socket de escucha, para que no entren clientes nuevos mientras
        se termina de atender a los que ya estaban.

        Returns:
            None.
        """
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()
        logger.info("servidor detenido")

    async def serve_until_stopped(self, stop_requested: asyncio.Event) -> None:
        """Mantiene el servidor en funcionamiento hasta que se pida detenerlo.

        Args:
            stop_requested: Evento que alguien más activa para pedir el apagado. Lo
                activa el manejador de señales de `main`.

        Returns:
            None. Vuelve cuando el servidor ya se detuvo por completo.
        """
        await self.start()
        await stop_requested.wait()
        await self.stop()

    # ─────────────────────── atención de un cliente ───────────────────────

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Atiende una conexión de principio a fin. Corre una vez por cliente.

        `asyncio.start_server` la ejecuta como una tarea independiente, así que una
        excepción acá afecta solo a este cliente: los demás siguen atendidos.

        El bucle lee pedidos hasta que el cliente cierra. Esa desconexión no es un error
        —es la forma normal de terminar— y se detecta porque `receive_header` levanta
        `IncompleteReadError` al encontrarse la conexión vacía.

        Args:
            reader: Stream de lectura de esta conexión.
            writer: Stream de escritura de esta conexión.

        Returns:
            None. Al volver, la conexión quedó cerrada.
        """
        address = format_address(writer.get_extra_info("peername"))
        self.connected_clients += 1
        logger.info("conectado %s (%d en total)", address, self.connected_clients)

        try:
            await self.serve_requests(reader, writer)

        except asyncio.IncompleteReadError:
            logger.info("desconectado %s", address)

        except protocol.ProtocolError as error:
            # El cliente no respeta el formato. Se informa y se corta: si el framing está
            # roto, no hay forma de saber dónde empieza el mensaje siguiente.
            logger.warning("protocolo inválido de %s: %s", address, error)
            await try_to_report(writer, messages.BAD_REQUEST, str(error))

        except (ConnectionResetError, BrokenPipeError):
            logger.info("%s se cayó sin cerrar", address)

        finally:
            self.connected_clients -= 1
            await close_quietly(writer)

    async def serve_requests(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Lee y atiende pedidos uno por uno, hasta que el cliente cierre.

        Args:
            reader: Stream de lectura de esta conexión.
            writer: Stream de escritura de esta conexión.

        Returns:
            None. Vuelve cuando el cliente cerró la conexión.

        Raises:
            asyncio.IncompleteReadError: Cuando el cliente cierra. Es la salida normal.
            protocol.ProtocolError: Si un mensaje no respeta el formato.
        """
        while True:
            header = await protocol.receive_header(reader)
            payload_size = protocol.payload_size_of(header)
            request_type = header.get(messages.TYPE_FIELD)

            logger.info(
                "pedido '%s' (%d bytes de payload)", request_type, payload_size
            )

            await self.dispatch(header, payload_size, reader, writer)

    async def dispatch(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Deriva el pedido al manejador que corresponda según su tipo.

        Consume el payload en todos los casos, también cuando el pedido se rechaza: los
        bytes que quedaran sin leer desfasarían todos los mensajes siguientes.

        Args:
            header: Header del pedido, ya deserializado.
            payload_size: Bytes de payload que siguen al header, según lo declarado.
            reader: Stream de lectura, para consumir el payload.
            writer: Stream de escritura, para responder.

        Returns:
            None.
        """
        request_type = header.get(messages.TYPE_FIELD)

        if request_type not in messages.REQUEST_TYPES:
            await discard_payload(reader, payload_size)
            await respond_error(
                writer,
                messages.BAD_REQUEST,
                f"tipo de pedido desconocido: '{request_type}'",
            )
            return

        # Etapa 1: el framing y el despacho funcionan, pero ningún pedido se atiende
        # todavía. Cada uno se irá implementando en su propio manejador.
        await discard_payload(reader, payload_size)
        await respond_error(
            writer,
            messages.INTERNAL,
            f"el servidor todavía no implementa '{request_type}'",
        )


# ─────────────────────────── funciones auxiliares ───────────────────────────


async def discard_payload(reader: asyncio.StreamReader, payload_size: int) -> None:
    """Lee y descarta el payload de un pedido que no se va a usar.

    Hace falta incluso al rechazar un pedido: el payload ya viene en camino y hay que
    sacarlo del socket para que el mensaje siguiente empiece donde debe.

    Args:
        reader: Stream de lectura de la conexión.
        payload_size: Cuántos bytes descartar. Si es cero, no hace nada.

    Returns:
        None.
    """
    if payload_size == 0:
        return

    async for _chunk in protocol.stream_payload(reader, payload_size):
        pass  # se lee para vaciar el socket; el contenido no interesa


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

    Returns:
        None.
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

    Returns:
        None.
    """
    try:
        await respond_error(writer, code, detail)
    except (OSError, protocol.ProtocolError):
        pass


async def close_quietly(writer: asyncio.StreamWriter) -> None:
    """Cierra la conexión sin propagar errores del cierre.

    Va en el `finally` del handler: si no se cierra, el descriptor queda ocupado, y un
    servidor que pierde descriptores termina sin poder aceptar a nadie más.

    Args:
        writer: Stream de escritura de la conexión.

    Returns:
        None.
    """
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, asyncio.IncompleteReadError):
        pass  # el otro extremo ya había cerrado: no hay nada que liberar


def format_address(address: object) -> str:
    """Convierte una dirección de socket en algo legible para el registro.

    Args:
        address: Lo que devuelven `getsockname` o `getpeername`: una tupla de dos
            elementos en IPv4 y de cuatro en IPv6.

    Returns:
        La dirección como `host:puerto`, o su representación textual si no tiene esa
        forma.
    """
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return str(address)
