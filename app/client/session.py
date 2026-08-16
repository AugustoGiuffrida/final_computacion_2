"""Capa de red del cliente: la conexión con el servidor y un método por cada pedido.

Este módulo **no imprime nada y no sabe nada de cómo se ve el cliente**. Recibe datos,
habla por el socket y devuelve datos. Esa restricción es deliberada: deja la presentación
del lado de quien llama, y permite probar la conversación con el servidor sin mirar
ninguna salida.

Para informar el avance de una transferencia, la sesión llama una función que le pasan
desde afuera (`on_progress`). Nunca al revés: nada de acá adentro sabe quién muestra ese
avance ni cómo.

Hace cumplir además una regla del protocolo: **una conexión por ejecución**, que
transporta todos los pedidos que hagan falta.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.client import formatting
from app.common import config, messages, protocol

class LocalValidationError(Exception):
    """El archivo no pasó las verificaciones que el cliente hace antes de enviarlo.

    Existe para fallar sin molestar al servidor cuando el problema se puede detectar acá.
    No es un error del protocolo: nunca llegó a haber diálogo.
    """


def validate_image_file(image_path: Path) -> int:
    """Verifica localmente que un archivo se pueda enviar, antes de abrir la conexión.

    Son las tres comprobaciones que el cliente puede hacer solo: que exista, que la
    extensión esté soportada y que no exceda el máximo. Que sea *realmente* una imagen
    requiere decodificarla, y es tarea del proceso de ingreso del servidor.

    Returns:
        El tamaño del archivo en bytes, que después se usa como `payload_size`.

    Raises:
        LocalValidationError: Si el archivo no sirve, con el motivo en el mensaje.
    """
    if not image_path.exists():
        raise LocalValidationError(f"no existe el archivo '{image_path}'")
    if not image_path.is_file():
        raise LocalValidationError(f"'{image_path}' no es un archivo")

    extension = image_path.suffix.lower()
    if extension not in config.SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(config.SUPPORTED_EXTENSIONS))
        raise LocalValidationError(
            f"la extensión '{extension}' no está soportada; se aceptan: {supported}"
        )

    file_size = image_path.stat().st_size
    if file_size == 0:
        raise LocalValidationError(f"el archivo '{image_path}' está vacío")
    if file_size > config.DEFAULT_MAX_IMAGE_SIZE:
        raise LocalValidationError(
            f"la imagen pesa {formatting.format_size(file_size)} y el máximo es "
            f"{formatting.format_size(config.DEFAULT_MAX_IMAGE_SIZE)}"
        )

    return file_size


class ClientSession:
    """Una conexión abierta con el servidor, sobre la que viajan todos los pedidos.

    Se usa como gestor de contexto asíncrono, que es lo que garantiza que el socket se
    cierre incluso si algo falla en el medio:

        async with ClientSession(host, port, user) as session:
            response = await session.submit(Path("foto.jpg"), "anonymize", {})

    Attributes:
        host: Dirección o nombre del servidor. Acepta IPv4, IPv6 o nombre de host.
        port: Puerto donde escucha el servidor.
        user: Nombre que se declara en cada pedido. Se declara, no se autentica: es una
            limitación conocida y documentada del protocolo.
    """

    def __init__(self, host: str, port: int, user: str) -> None:
        """Prepara la sesión sin conectarse todavía."""
        self.host = host
        self.port = port
        self.user = user

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    # ─────────────────────── ciclo de vida de la conexión ───────────────────────

    async def connect(self) -> None:
        """Abre la conexión TCP.

        `open_connection` resuelve el host y elige la familia de direcciones que
        corresponda, así que el mismo código sirve para IPv4 y para IPv6.

        Raises:
            OSError: Si el servidor no está escuchando, el nombre no resuelve o la red no
                está disponible.
        """
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        """Cierra la conexión y espera a que el socket quede liberado.

        Es idempotente: llamarla dos veces, o sobre una sesión que nunca se conectó, no
        hace nada ni falla.
        """
        if self._writer is None:
            return

        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, asyncio.IncompleteReadError):
            # El otro extremo ya había cerrado. No hay nada que liberar ni que informar.
            pass
        finally:
            self._reader, self._writer = None, None

    async def __aenter__(self) -> ClientSession:
        """Abre la conexión al entrar al bloque `async with`."""
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Cierra la conexión al salir del bloque, haya habido error o no."""
        await self.close()

    @property
    def is_connected(self) -> bool:
        """Indica si `connect` se ejecutó y `close` todavía no."""
        return self._writer is not None

    # ─────────────────────────── pedidos del protocolo ───────────────────────────

    async def submit(
        self,
        image_path: Path,
        operation: str,
        parameters: dict[str, Any],
        on_progress: protocol.ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Envía una imagen para que el servidor la procese.

        Es el único pedido que lleva payload. La imagen viaja en bloques y nunca se carga
        entera en memoria. El servidor no espera a que se procese, pero sí espera la
        revisión de su proceso de ingreso, así que la respuesta puede tardar ~100 ms.

        Conviene validar la imagen antes con `validate_image_file`.

        Returns:
            El header de la respuesta, con 'job_id', 'status' y 'deduplicated'. Cuando
            'deduplicated' es True el 'job_id' es el de un trabajo **anterior** que ya
            tenía el resultado, no uno nuevo.

        Raises:
            messages.ServerError: Si el servidor rechazó el pedido (`INVALID_IMAGE`,
                `TOO_LARGE`, `UNKNOWN_OP`).
            OSError: Si el archivo no se puede leer o la conexión se corta.
        """
        request = {
            messages.TYPE_FIELD: messages.SUBMIT,
            "user": self.user,
            "op": operation,
            "params": parameters,
            "filename": image_path.name,
        }

        await protocol.send_file(self._require_writer(), request, image_path, on_progress)
        return await self._receive_response()

    async def status(self, job_id: str) -> dict[str, Any]:
        """Consulta el estado de un trabajo y, si terminó, los datos que produjo.

        Returns:
            El header de la respuesta, con 'status' y —cuando es DONE— 'has_output' y
            'result'. Si falló, trae 'error' con el motivo.

        Raises:
            messages.ServerError: `JOB_NOT_FOUND` si no existe, `FORBIDDEN` si es de otro.
        """
        return await self._request({
            messages.TYPE_FIELD: messages.STATUS,
            "user": self.user,
            "job_id": job_id,
        })

    async def history(self, limit: int) -> list[dict[str, Any]]:
        """Pide los últimos trabajos del usuario, del más reciente al más antiguo.

        Returns:
            Los trabajos, cada uno con 'job_id', 'op', 'status', 'filename' y sus
            timestamps. Lista vacía si el usuario todavía no hizo ninguno.

        Raises:
            messages.ServerError: Si el servidor rechazó el pedido.
        """
        response = await self._request({
            messages.TYPE_FIELD: messages.HISTORY,
            "user": self.user,
            "limit": limit,
        })

        jobs = response.get("jobs", [])
        return jobs if isinstance(jobs, list) else []

    async def download(
        self,
        job_id: str,
        destination: Path | None = None,
        on_progress: protocol.ProgressCallback | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        """Descarga el archivo que produjo un trabajo y lo escribe en disco.

        Los bytes se escriben a medida que llegan, sin acumular el archivo en memoria.

        Args:
            destination: Dónde guardarlo. Si es None se usa el nombre que sugiere el
                servidor, en el directorio actual.

        Returns:
            La ruta donde quedó el archivo y el header de la respuesta.

        Raises:
            messages.ServerError: `NOT_READY` si el trabajo no terminó, `NO_OUTPUT` si la
                operación no genera archivo (el caso de `inspect`), `JOB_NOT_FOUND` o
                `FORBIDDEN`.
            protocol.ProtocolError: Si la transferencia se corta. El archivo parcial se
                borra antes de propagar el error.
        """
        request = {
            messages.TYPE_FIELD: messages.DOWNLOAD,
            "user": self.user,
            "job_id": job_id,
        }

        await protocol.send_message(self._require_writer(), request)

        response = await self._receive_response()
        payload_size = protocol.payload_size_of(response)

        output_path = destination or Path(response.get("filename", f"{job_id}.bin"))
        await self._write_payload_to_disk(output_path, payload_size, on_progress)

        return output_path, response

    # ────────────────────────── espera de un resultado ──────────────────────────

    async def wait_until_finished(
        self,
        job_id: str,
        timeout: float = config.DEFAULT_WAIT_TIMEOUT_SECONDS,
        on_poll: Callable[[dict[str, Any], float], None] | None = None,
    ) -> dict[str, Any]:
        """Consulta el estado periódicamente hasta que el trabajo termine.

        Implementa la política de espera del protocolo: el servidor nunca avisa por su
        cuenta —solo responde— así que es el cliente el que vuelve a preguntar. Entre
        consulta y consulta espera con `asyncio.sleep`, que no bloquea el event loop: la
        barra de progreso se sigue animando durante toda la espera.

        Rendirse por tiempo **no cancela nada**: el trabajo sigue su curso en el worker y
        el resultado queda disponible para pedirlo después con el mismo `job_id`.

        Args:
            on_poll: Se llama después de cada consulta con la respuesta y los segundos
                transcurridos, para ir mostrando el avance.

        Returns:
            El último header de estado recibido. Si se agotó el tiempo es el de la última
            consulta, con el trabajo todavía en curso: quien llama distingue el caso
            mirando si 'status' está en `messages.TERMINAL_STATUSES`.

        Raises:
            messages.ServerError: Si alguna consulta es rechazada.
        """
        loop = asyncio.get_running_loop()
        started_at = loop.time()

        while True:
            response = await self.status(job_id)
            elapsed_seconds = loop.time() - started_at

            if on_poll is not None:
                on_poll(response, elapsed_seconds)

            if response.get("status") in messages.TERMINAL_STATUSES:
                return response
            if elapsed_seconds >= timeout:
                return response

            await asyncio.sleep(config.STATUS_POLL_INTERVAL_SECONDS)

    # ──────────────────────────────── internos ────────────────────────────────

    async def _request(self, header: dict[str, Any]) -> dict[str, Any]:
        """Envía un pedido sin payload y devuelve la respuesta.

        Es el camino de `status` y `history`. El `submit` no lo usa porque manda un
        archivo, y el `download` tampoco porque recibe uno.
        """
        await protocol.send_message(self._require_writer(), header)
        return await self._receive_response()

    async def _receive_response(self) -> dict[str, Any]:
        """Lee el header de la respuesta y lo verifica.

        Raises:
            messages.ServerError: Si el servidor respondió un `error`.
        """
        response = await protocol.receive_header(self._require_reader())
        return messages.raise_if_error(response)

    async def _write_payload_to_disk(
        self,
        output_path: Path,
        payload_size: int,
        on_progress: protocol.ProgressCallback | None,
    ) -> None:
        """Vuelca el payload de la respuesta en un archivo, bloque por bloque."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        received_bytes = 0
        try:
            with open(output_path, "wb") as output_file:
                async for chunk in protocol.stream_payload(
                    self._require_reader(), payload_size
                ):
                    output_file.write(chunk)

                    received_bytes += len(chunk)
                    if on_progress is not None:
                        on_progress(received_bytes, payload_size)
        except BaseException:
            # Un archivo truncado es peor que ninguno: parece una imagen y no lo es.
            output_path.unlink(missing_ok=True)
            raise

    def _require_writer(self) -> asyncio.StreamWriter:
        """Devuelve el stream de escritura, verificando que la sesión esté conectada.

        Raises:
            RuntimeError: Si falta llamar a `connect`. Es un error de programación, no
                una falla de red.
        """
        if self._writer is None:
            raise RuntimeError("la sesión no está conectada: falta llamar a connect()")
        return self._writer

    def _require_reader(self) -> asyncio.StreamReader:
        """Devuelve el stream de lectura, verificando que la sesión esté conectada.

        Raises:
            RuntimeError: Si falta llamar a `connect`.
        """
        if self._reader is None:
            raise RuntimeError("la sesión no está conectada: falta llamar a connect()")
        return self._reader
