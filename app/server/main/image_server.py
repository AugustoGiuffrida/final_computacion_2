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

Acá vive la clase y nada más: la validación de lo que llega está en `incoming.py` y el
armado de lo que sale en `outgoing.py`.

ESTADO: el proceso principal está completo. Acepta conexiones, respeta el framing y
resuelve los cuatro pedidos del protocolo contra un índice en memoria. Lo que falta —el
proceso de ingreso, la cola de tareas, la base de datos— está diseñado en `docs/` y
pendiente de aprobación.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from app.common import config, messages, protocol
from app.common.messages import (
    BadRequest,
    InternalError,
    NoOutput,
    NotReady,
    RequestError,
    TooLarge,
)
from app.server.main import incoming, outgoing, registry

logger = logging.getLogger(__name__)


class ImageServer:
    """Servidor TCP que atiende pedidos del protocolo de la aplicación.

    Se instancia con la configuración, se arranca con `start` y se detiene con `stop`.
    La configuración se guarda en la instancia porque `asyncio.start_server` solo le pasa
    al handler el reader y el writer de la conexión: cualquier otro dato tiene que llegarle
    por acá.

    Attributes:
        host: Dirección en la que escucha. `None` significa todas las interfaces
            disponibles: abre un socket por familia, IPv4 e IPv6, en el mismo puerto.
        port: Puerto en el que escucha.
        storage_dir: Raíz de los archivos del sistema.
        uploads_dir: Dónde se guardan las imágenes recibidas, una carpeta por trabajo.
        connected_clients: Cuántas conexiones hay abiertas en este momento.
        jobs: Registro en memoria de los trabajos aceptados.
    """

    def __init__(
        self,
        host: str | None = config.LISTEN_ON_ALL_INTERFACES,
        port: int = config.DEFAULT_PORT,
        storage_dir: Path = config.STORAGE_DIR,
    ) -> None:
        """Prepara el servidor sin abrir todavía el socket de escucha.

        Args:
            host: Dirección de escucha, o None para todas las interfaces.
            port: Puerto de escucha.
            storage_dir: Raíz de los archivos. Las imágenes recibidas van a su
                subdirectorio `uploads/`. Se puede cambiar para no escribir en el
                directorio real del proyecto, que es lo que hacen las pruebas.
        """
        self.host = host
        self.port = port
        self.storage_dir = storage_dir
        self.uploads_dir = storage_dir / "uploads"
        self.connected_clients = 0
        self.jobs = registry.JobRegistry()
        self._server: asyncio.Server | None = None

    # ─────────────────────── ciclo de vida ───────────────────────

    async def start(self) -> None:
        """Abre los sockets de escucha y empieza a aceptar conexiones.

        `asyncio.start_server` hace por debajo la secuencia completa de un servidor TCP
        —crear el socket, ajustar sus opciones, reclamar la dirección con `bind`, ponerlo
        en modo pasivo con `listen`— y deja corriendo el bucle de `accept` dentro del
        event loop. Por cada conexión aceptada crea una tarea nueva que ejecuta
        `handle_client`.

        Sin un host concreto abre **un socket por familia**, IPv4 e IPv6, sobre el mismo
        puerto. Con una dirección concreta abre solo el de la familia que corresponda.

        Returns:
            None. Al volver, el servidor ya está aceptando conexiones.

        Raises:
            OSError: Si el puerto está ocupado o la dirección no está disponible.
        """
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        ) # host=None hace que getaddrinfo devuelva dos resultados(IPv4 (0.0.0.0) y la de IPv6 (::))

        for listening_socket in self._server.sockets:
            address = listening_socket.getsockname()
            logger.info(
                "escuchando en %s (%s)",
                format_address(address), listening_socket.family.name,
            )

    @property
    def listening_port(self) -> int:
        """Puerto en el que el servidor quedó escuchando.

        Hace falta cuando se arranca con el puerto 0, que le pide al sistema operativo
        que elija uno libre: recién después de abrir el socket se sabe cuál fue.

        Returns:
            El puerto del primer socket de escucha.

        Raises:
            RuntimeError: Si todavía no se llamó a `start`.
        """
        if self._server is None or not self._server.sockets:
            raise RuntimeError("el servidor no está escuchando: falta llamar a start()")
        return int(self._server.sockets[0].getsockname()[1]) #los dos escuchan en el mismo puerto

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
            await outgoing.try_to_report(writer, messages.BAD_REQUEST, str(error))

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

            logger.info(
                "pedido '%s' (%d bytes de payload)",
                header.get(messages.TYPE_FIELD), payload_size,
            )

            try:
                await self.dispatch(header, payload_size, reader, writer)
            except RequestError as error:
                # Un pedido rechazado es una respuesta más y no corta la conexión: el
                # cliente tiene que poder distinguirlo de una caída del servidor. La
                # excepción son los rechazos que dejan bytes sin leer en el socket.
                logger.info("rechazado: %s", error)
                await outgoing.respond_error(writer, error.code, error.detail)

                if error.closes_connection:
                    return

    async def dispatch(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Deriva el pedido al manejador que corresponda según su tipo.

        Distingue dos rechazos que son cosas distintas: un tipo que **no existe** en el
        protocolo es `BAD_REQUEST`, y uno que existe pero **todavía no está implementado**
        es `INTERNAL`.

        Los pedidos que no llegan a un manejador consumen su payload acá antes de
        rechazarse. Los que sí llegan lo consumen ellos: `submit` necesita leerlo, no
        descartarlo.

        Args:
            header: Header del pedido, ya deserializado.
            payload_size: Bytes de payload que siguen al header, según lo declarado.
            reader: Stream de lectura, para consumir el payload.
            writer: Stream de escritura, para responder.

        Returns:
            None.

        Raises:
            TooLarge: Si el payload anunciado supera el máximo aceptado.
            BadRequest: Si el tipo de pedido no está en el catálogo.
        """
        request_type = header.get(messages.TYPE_FIELD)

        if payload_size > config.DEFAULT_MAX_IMAGE_SIZE:
            raise TooLarge(
                f"el payload declarado es de {payload_size} bytes y el máximo que se "
                f"acepta es {config.DEFAULT_MAX_IMAGE_SIZE}"
            )

        if request_type not in messages.REQUEST_TYPES:
            await incoming.discard_payload(reader, payload_size)
            raise BadRequest(f"tipo de pedido desconocido: '{request_type}'")

        if request_type == messages.HISTORY:
            await self.handle_history(header, payload_size, reader, writer)
        elif request_type == messages.SUBMIT:
            await self.handle_submit(header, payload_size, reader, writer)
        elif request_type == messages.STATUS:
            await self.handle_status(header, payload_size, reader, writer)
        else:
            await self.handle_download(header, payload_size, reader, writer)

    # ─────────────────────── manejadores de cada pedido ───────────────────────

    async def handle_history(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Responde los últimos trabajos del usuario, del más reciente al más antiguo.

        Los trabajos salen del registro en memoria, así que solo aparecen los aceptados
        desde que el servidor arrancó. En el diseño completo, los anteriores salen de
        SQLite.

        El payload se consume **antes** de validar: si la validación fallara primero, esos
        bytes quedarían en el socket y el mensaje siguiente los tomaría como su prefijo de
        longitud. La regla vale para los cuatro manejadores: consumir primero, validar
        después.

        Args:
            header: Header del pedido.
            payload_size: Bytes de payload declarados. `history` no lleva ninguno.
            reader: Stream de lectura, para consumir el payload.
            writer: Stream de escritura, para responder.

        Returns:
            None.

        Raises:
            BadRequest: Si falta el usuario o el límite no es un entero positivo.
        """
        await incoming.discard_payload(reader, payload_size)

        user = incoming.require_user(header)
        limit = incoming.read_limit(header)

        jobs = self.jobs.list_for(user, limit)
        logger.info("historial de '%s': %d trabajos", user, len(jobs))

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "jobs": [job.as_summary() for job in jobs],
        })


    async def handle_submit(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Recibe una imagen, la guarda y registra el trabajo.

        Es el único pedido que trae payload y el único que escribe en disco.

        El header se valida **antes** de leer la imagen, para no escribir megabytes que
        van a descartarse. Como una validación fallida dejaría el payload sin leer y
        desfasaría el mensaje siguiente, se consume antes de propagar el rechazo.

        ESTADO: el trabajo queda en `QUEUED` y nada lo hace avanzar; falta la cola de
        tareas. Y `deduplicated` es siempre falso: detectar repetidos es tarea del proceso
        de ingreso, que tampoco existe todavía.

        Args:
            header: Header del pedido.
            payload_size: Bytes de la imagen que siguen al header.
            reader: Stream de lectura, para recibir la imagen.
            writer: Stream de escritura, para responder.

        Returns:
            None.

        Raises:
            RequestError: Si el pedido es inválido en cualquiera de sus campos; la
                subclase depende de qué campo falle.
            asyncio.IncompleteReadError: Si la transferencia se corta antes de completar
                los bytes anunciados.
        """
        try:
            user = incoming.require_user(header)
            operation = incoming.require_operation(header)
            parameters = incoming.read_parameters(header, operation)
            filename = incoming.safe_filename(header)
            incoming.require_non_empty_image(payload_size)
        except RequestError:
            # Consumir antes de propagar: los bytes ya vienen en camino y hay que sacarlos
            # del socket para que el mensaje siguiente empiece donde debe.
            await incoming.discard_payload(reader, payload_size)
            raise

        job = registry.new_job(user, operation, parameters, filename)
        job_directory = self.uploads_dir / job.job_id

        try:
            await incoming.save_upload(reader, payload_size, job_directory / filename)
        except BaseException:
            # Ni el archivo a medias ni el directorio sirven si el trabajo no prospera.
            # Se atrapa BaseException y no Exception porque una cancelación de la tarea
            # llega como CancelledError, que no hereda de Exception.
            shutil.rmtree(job_directory, ignore_errors=True)
            raise

        self.jobs.add(job)
        logger.info(
            "trabajo %s aceptado: '%s' sobre '%s' (%d bytes)",
            job.job_id, operation, filename, payload_size,
        )

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": job.job_id,
            "status": job.status,
            "deduplicated": False,
        })


    async def handle_status(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Responde en qué estado está un trabajo y, si terminó, qué produjo.

        Los campos `has_output` y `result` solo aparecen cuando el trabajo terminó bien:
        el primero le dice al cliente si tiene sentido pedir la descarga, el segundo trae
        los datos de la operación. Si falló, viaja el motivo en `error`.

        ESTADO: hoy todos los trabajos quedan en `QUEUED`, porque nada los procesa. Las
        ramas de terminado y fallado están implementadas y probadas para cuando exista la
        cola de tareas.

        Args:
            header: Header del pedido.
            payload_size: Bytes de payload declarados. `status` no lleva ninguno.
            reader: Stream de lectura, para consumir el payload.
            writer: Stream de escritura, para responder.

        Returns:
            None.

        Raises:
            BadRequest: Si falta algún campo obligatorio.
            JobNotFound: Si no existe un trabajo con ese identificador.
            Forbidden: Si el trabajo es de otro usuario.
        """
        await incoming.discard_payload(reader, payload_size)

        user = incoming.require_user(header)
        job_id = incoming.require_job_id(header)

        job = self.jobs.find(user, job_id)

        response: dict[str, Any] = {
            messages.TYPE_FIELD: messages.OK,
            "job_id": job.job_id,
            "status": job.status,
        }

        if job.status == messages.DONE:
            response["has_output"] = job.output_path is not None
            response["result"] = job.result or {}
        elif job.status == messages.FAILED:
            response["error"] = job.error or ""

        await protocol.send_message(writer, response)

    async def handle_download(
        self,
        header: dict[str, Any],
        payload_size: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Envía el archivo que produjo un trabajo.

        Es el pedido inverso al envío: sin payload de ida, con payload de vuelta. El
        archivo se manda en bloques, así que la memoria no crece con su tamaño.

        Los cuatro controles previos son excluyentes y van del más general al más
        específico: que el trabajo exista y sea de quien pregunta, que haya terminado,
        que haya producido un archivo, y que ese archivo siga estando. Cada uno da un
        error distinto, para que el cliente sepa qué pasó.

        ESTADO: sin nada que procese los trabajos, ninguno llega a `DONE` y en uso normal
        este pedido siempre responde `NOT_READY`. El camino completo está implementado y
        se prueba inyectando un trabajo terminado en el registro.

        Args:
            header: Header del pedido.
            payload_size: Bytes de payload declarados. `download` no lleva ninguno.
            reader: Stream de lectura, para consumir el payload.
            writer: Stream de escritura, para responder y enviar el archivo.

        Returns:
            None.

        Raises:
            JobNotFound: Si no existe un trabajo con ese identificador.
            Forbidden: Si el trabajo es de otro usuario.
            NotReady: Si el trabajo todavía no terminó.
            NoOutput: Si la operación no genera archivo, o si el trabajo falló.
            InternalError: Si el trabajo terminó pero su archivo ya no está.
        """
        await incoming.discard_payload(reader, payload_size)

        user = incoming.require_user(header)
        job_id = incoming.require_job_id(header)

        job = self.jobs.find(user, job_id)

        if job.status == messages.FAILED:
            raise NoOutput(
                "el trabajo falló y no produjo ningún archivo: "
                f"{job.error or 'sin motivo registrado'}"
            )
        if job.status != messages.DONE:
            raise NotReady(
                f"el trabajo todavía está en {job.status}: no hay nada para descargar"
            )
        if job.output_path is None:
            raise NoOutput(
                f"'{job.operation}' no genera archivo; su resultado está en la consulta "
                "de estado"
            )
        if not job.output_path.exists():
            # El trabajo figura terminado pero el archivo no está: lo borró la limpieza
            # periódica de resultados viejos, o alguien tocó el volumen por fuera. Sin
            # este control, `send_file` fallaría con un OSError que cerraría la conexión,
            # y el cliente vería "el servidor cortó" en vez de una explicación.
            raise InternalError("el resultado de ese trabajo ya no está disponible")

        logger.info("descarga de %s: %s", job.job_id, job.output_path.name)

        await protocol.send_file(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": job.job_id,
            "filename": outgoing.suggested_download_name(job),
            "content_type": outgoing.content_type_of(job.output_path),
        }, job.output_path)


# ─────────────────────────── manejo de la conexión ───────────────────────────


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
