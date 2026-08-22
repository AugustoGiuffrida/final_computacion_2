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
import shutil
from pathlib import Path
from typing import Any

from app.common import config, messages, protocol
from app.server import registry

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

            logger.info(
                "pedido '%s' (%d bytes de payload)",
                header.get(messages.TYPE_FIELD), payload_size,
            )

            try:
                await self.dispatch(header, payload_size, reader, writer)
            except messages.RequestError as error:
                # Un pedido rechazado es una respuesta más y no corta la conexión: el
                # cliente tiene que poder distinguirlo de una caída del servidor. La
                # excepción son los rechazos que dejan bytes sin leer en el socket.
                logger.info("rechazado: %s", error)
                await respond_error(writer, error.code, error.detail)

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
            messages.RequestError: Si el tipo no existe o todavía no está implementado.
        """
        request_type = header.get(messages.TYPE_FIELD)

        if payload_size > config.DEFAULT_MAX_IMAGE_SIZE:
            raise messages.RequestError(
                messages.TOO_LARGE,
                f"el payload declarado es de {payload_size} bytes y el máximo que se "
                f"acepta es {config.DEFAULT_MAX_IMAGE_SIZE}",
                closes_connection=True,
            )

        if request_type not in messages.REQUEST_TYPES:
            await discard_payload(reader, payload_size)
            raise messages.RequestError(
                messages.BAD_REQUEST, f"tipo de pedido desconocido: '{request_type}'"
            )

        if request_type == messages.HISTORY:
            await self.handle_history(header, payload_size, reader, writer)
        elif request_type == messages.SUBMIT:
            await self.handle_submit(header, payload_size, reader, writer)
        else:
            # `status` y `download` existen en el protocolo pero todavía no se atienden.
            # Cada uno se irá sumando como un `elif` propio a medida que se implemente.
            await discard_payload(reader, payload_size)
            raise messages.RequestError(
                messages.INTERNAL, f"el servidor todavía no implementa '{request_type}'"
            )

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
            messages.RequestError: `BAD_REQUEST` si falta el usuario o el límite es
                inválido.
        """
        await discard_payload(reader, payload_size)

        user = require_user(header)
        limit = read_limit(header)

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
            messages.RequestError: Si el pedido es inválido en cualquiera de sus campos.
            asyncio.IncompleteReadError: Si la transferencia se corta antes de completar
                los bytes anunciados.
        """
        try:
            user = require_user(header)
            operation = require_operation(header)
            parameters = read_parameters(header, operation)
            filename = safe_filename(header)
            require_non_empty_image(payload_size)
        except messages.RequestError:
            # Consumir antes de propagar: los bytes ya vienen en camino y hay que sacarlos
            # del socket para que el mensaje siguiente empiece donde debe.
            await discard_payload(reader, payload_size)
            raise

        job = registry.new_job(user, operation, parameters, filename)
        job_directory = self.uploads_dir / job.job_id

        try:
            await save_upload(reader, payload_size, job_directory / filename)
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


# ──────────────────────── validación de campos comunes ────────────────────────


def require_user(header: dict[str, Any]) -> str:
    """Lee el usuario declarado en el pedido, verificando que esté presente.

    Los cuatro pedidos del protocolo lo llevan, así que se valida en un solo lugar.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        El nombre del usuario, sin espacios sobrantes.

    Raises:
        messages.RequestError: `BAD_REQUEST` si falta, está vacío o no es texto.
    """
    user = header.get("user")

    if not isinstance(user, str) or not user.strip():
        raise messages.RequestError(
            messages.BAD_REQUEST, "falta el campo 'user' o está vacío"
        )

    return user.strip()


def read_limit(header: dict[str, Any]) -> int:
    """Lee cuántos trabajos devolver en el historial, con un tope propio del servidor.

    El tope existe porque el límite lo elige el cliente: sin él, un pedido de un millón de
    filas haría trabajar al servidor mucho más de lo razonable. Se recorta en silencio en
    vez de rechazar el pedido, porque pedir de más no es un error del cliente: no tiene
    por qué conocer nuestro tope, y sería un error que no puede corregir.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        La cantidad a devolver, acotada a `config.MAX_HISTORY_LIMIT`.

    Raises:
        messages.RequestError: `BAD_REQUEST` si el valor no es un entero positivo.
    """
    limit = header.get("limit", config.DEFAULT_HISTORY_LIMIT)

    # El bool se descarta aparte porque en Python es subclase de int: sin esto, un
    # 'limit' de `true` pasaría como si fuera 1.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise messages.RequestError(
            messages.BAD_REQUEST, "'limit' debe ser un entero positivo"
        )

    return min(limit, config.MAX_HISTORY_LIMIT)



def require_operation(header: dict[str, Any]) -> str:
    """Lee la operación pedida, verificando que exista.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        El nombre de la operación.

    Raises:
        messages.RequestError: `UNKNOWN_OP` si falta o no es una de las soportadas.
    """
    operation = header.get("op")

    if not isinstance(operation, str) or operation not in config.OPERATION_PARAMETERS:
        supported = ", ".join(sorted(config.OPERATION_PARAMETERS))
        raise messages.RequestError(
            messages.UNKNOWN_OP,
            f"la operación '{operation}' no existe; las disponibles son: {supported}",
        )

    return operation


def read_parameters(header: dict[str, Any], operation: str) -> dict[str, Any]:
    """Lee los parámetros del pedido, verificando que correspondan a esa operación.

    Se validan los **nombres**, no los valores: qué significa cada parámetro y qué rangos
    admite lo sabe el worker que lo va a usar. El cliente ya verifica los rangos por
    comodidad, pero eso no se puede dar por hecho — cualquiera puede hablar el protocolo
    sin usar nuestro cliente.

    Un parámetro que la operación no acepta se rechaza en vez de ignorarse: casi siempre
    es una confusión, y silenciarlo haría creer que se aplicó.

    Args:
        header: Header del pedido, ya deserializado.
        operation: La operación, ya validada.

    Returns:
        Los parámetros pedidos, o un diccionario vacío si no vinieron.

    Raises:
        messages.RequestError: `BAD_REQUEST` si no es un objeto o trae un parámetro ajeno
            a la operación.
    """
    parameters = header.get("params", {})

    if not isinstance(parameters, dict):
        raise messages.RequestError(messages.BAD_REQUEST, "'params' debe ser un objeto")

    accepted = config.OPERATION_PARAMETERS[operation]
    for name in parameters:
        if name not in accepted:
            detail = (
                f"acepta: {', '.join(accepted)}" if accepted else "no acepta parámetros"
            )
            raise messages.RequestError(
                messages.BAD_REQUEST,
                f"'{name}' no es un parámetro de '{operation}'; {detail}",
            )

    return parameters


def safe_filename(header: dict[str, Any]) -> str:
    """Lee el nombre del archivo del pedido, dejándolo seguro para usar como ruta.

    El nombre lo elige el cliente, así que **no se puede confiar en él**. Un valor como
    `../../etc/passwd` construiría una ruta fuera del directorio del trabajo.
    `Path().name` se queda solo con el último componente, lo que elimina esa posibilidad
    de raíz en lugar de intentar detectar los casos peligrosos uno por uno.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        El nombre del archivo, sin ninguna parte de ruta.

    Raises:
        messages.RequestError: `BAD_REQUEST` si falta o no queda un nombre usable;
            `INVALID_IMAGE` si la extensión no está soportada.
    """
    raw_name = header.get("filename")

    if not isinstance(raw_name, str) or not raw_name.strip():
        raise messages.RequestError(
            messages.BAD_REQUEST, "falta el campo 'filename' o está vacío"
        )

    name = Path(raw_name).name
    if not name or name in (".", ".."):
        raise messages.RequestError(
            messages.BAD_REQUEST, f"'{raw_name}' no es un nombre de archivo válido"
        )

    if Path(name).suffix.lower() not in config.SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(config.SUPPORTED_EXTENSIONS))
        raise messages.RequestError(
            messages.INVALID_IMAGE,
            f"la extensión de '{name}' no está soportada; se aceptan: {supported}",
        )

    return name


def require_non_empty_image(payload_size: int) -> None:
    """Verifica que el envío traiga efectivamente una imagen.

    El límite superior no se controla acá: lo aplica `dispatch` sobre todos los pedidos,
    porque un payload excesivo obliga además a cortar la conexión.

    Args:
        payload_size: Bytes declarados en el header.

    Returns:
        None.

    Raises:
        messages.RequestError: `BAD_REQUEST` si el envío viene sin contenido.
    """
    if payload_size == 0:
        raise messages.RequestError(
            messages.BAD_REQUEST, "un envío tiene que traer una imagen"
        )


# ─────────────────────────── funciones auxiliares ───────────────────────────

async def save_upload(
    reader: asyncio.StreamReader, payload_size: int, destination: Path
) -> None:
    """Vuelca el payload del pedido en un archivo, bloque por bloque.

    No acumula la imagen en memoria: la escribe a medida que llega. Con muchos clientes
    subiendo archivos grandes, esa es la diferencia entre usar unos kilobytes por conexión
    y usar el tamaño entero de cada imagen.

    Args:
        reader: Stream de lectura de la conexión.
        payload_size: Cuántos bytes leer, según lo declarado en el header.
        destination: Dónde escribir. Su directorio se crea si no existe.

    Returns:
        None.

    Raises:
        asyncio.IncompleteReadError: Si la conexión se corta antes de completar los bytes
            anunciados. El archivo parcial queda en disco: limpiarlo le toca a quien
            llama, que es el que sabe qué más hay que deshacer.
        OSError: Si no se puede crear el directorio o escribir el archivo.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    with open(destination, "wb") as image_file:
        async for chunk in protocol.stream_payload(reader, payload_size):
            image_file.write(chunk)




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
