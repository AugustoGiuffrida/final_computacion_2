"""Servidor de mentira, para probar el cliente mientras el real no existe.

ESTO NO ES PARTE DEL SISTEMA. Es un andamio de desarrollo: habla el protocolo lo
suficiente como para que el cliente tenga con quién conversar, pero no procesa ninguna
imagen, no encola nada en Celery, no escribe en ninguna base y no verifica que lo que
recibe sea realmente una imagen. Simula el paso del tiempo con un temporizador y devuelve
resultados inventados.

Sirve para dos cosas: ver el cliente funcionando de punta a punta antes de que el servidor
esté escrito, y tener contra qué probar la interfaz. Cuando el servidor real esté listo,
este archivo se borra.

Uso:

    python -m tools.fake_server                 # escucha en el puerto 9000
    python -m tools.fake_server --port 9100 --processing-seconds 8
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from app.common import config, messages, protocol

QUEUED_SECONDS: Final[float] = 2.0 #Cuánto tarda un trabajo simulado en pasar de QUEUED a PROCESSING.

DEFAULT_PROCESSING_SECONDS: Final[float] = 5.0 #Cuánto tarda, después de eso, en llegar a DONE.

#Resultados inventados por operación, para que el cliente tenga algo que mostrar. Imitan
#la forma de los que producirían los workers, no sus valores.
SIMULATED_RESULTS: Final[dict[str, dict[str, Any]]] = {
    "inspect": {
        "faces_detected": 2,
        "gps": {"lat": -32.889459, "lon": -68.845839},
        "taken_at": "2026-07-04T18:22:10",
        "camera": "iPhone 14",
        "serial_number": "F2LW48ZQKPH7",
        "bytes": 3145728,
    },
    "anonymize": {"faces_detected": 3, "bytes": 284915, "content_type": "image/jpeg"},
    "clean": {
        "removed_metadata": ["GPSInfo", "DateTimeOriginal", "Make", "Model"],
        "bytes": 2890112,
        "content_type": "image/jpeg",
    },
    "convert": {
        "source_format": "JPEG", "target_format": "WEBP",
        "bytes": 198433, "content_type": "image/webp",
    },
    "compress": {
        "original_bytes": 3145728, "final_bytes": 284915,
        "bytes": 284915, "content_type": "image/jpeg",
    },
    "sanitize": {
        "stages": ["anonymize", "clean", "compress"],
        "faces_detected": 3,
        "removed_metadata": ["GPSInfo", "DateTimeOriginal"],
        "original_bytes": 3145728, "final_bytes": 251904,
        "bytes": 251904, "content_type": "image/jpeg",
    },
}


class SimulatedJob:
    """Un trabajo inventado, que avanza de estado con el reloj.

    Attributes:
        job_id: Identificador del trabajo.
        user: Usuario que lo creó.
        operation: Operación pedida.
        filename: Nombre original de la imagen.
        upload_path: Dónde quedaron guardados los bytes recibidos.
        created_at: Cuándo se aceptó.
    """

    def __init__(
        self, job_id: str, user: str, operation: str, filename: str, upload_path: Path
    ) -> None:
        """Registra el trabajo y arranca su reloj."""
        self.job_id = job_id
        self.user = user
        self.operation = operation
        self.filename = filename
        self.upload_path = upload_path
        self.created_at = datetime.now()
        self._started_at = asyncio.get_running_loop().time()

    def status(self, processing_seconds: float) -> str:
        """Calcula en qué estado estaría el trabajo según cuánto pasó."""
        elapsed = asyncio.get_running_loop().time() - self._started_at

        if elapsed < QUEUED_SECONDS:
            return messages.QUEUED
        if elapsed < QUEUED_SECONDS + processing_seconds:
            return messages.PROCESSING
        return messages.DONE

    @property
    def has_output(self) -> bool:
        """Indica si la operación produce un archivo descargable."""
        return self.operation != "inspect"

    @property
    def result(self) -> dict[str, Any]:
        """Devuelve el resultado inventado de la operación."""
        return SIMULATED_RESULTS.get(self.operation, {})


class FakeServer:
    """Atiende clientes y les contesta con trabajos simulados.

    Attributes:
        processing_seconds: Cuánto tarda un trabajo en pasar de PROCESSING a DONE.
        jobs: Los trabajos creados desde que arrancó, por identificador.
    """

    def __init__(self, processing_seconds: float, storage_dir: Path) -> None:
        """Prepara el servidor."""
        self.processing_seconds = processing_seconds
        self.storage_dir = storage_dir
        self.jobs: dict[str, SimulatedJob] = {}

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Atiende una conexión hasta que el cliente la cierra."""
        client_address = writer.get_extra_info("peername")
        print(f"  + conexión de {client_address}")

        try:
            while True:
                request = await protocol.receive_header(reader)
                await self.handle_request(request, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass  # El cliente se fue. Es la forma normal de terminar.
        except protocol.ProtocolError as error:
            print(f"  ! error de protocolo: {error}")
        finally:
            print(f"  - se fue {client_address}")
            writer.close()

    async def handle_request(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Despacha un pedido al método que corresponda."""
        request_type = request.get(messages.TYPE_FIELD, "")
        print(f"    → {request_type} {request.get('job_id', request.get('op', ''))}")

        if request_type == messages.SUBMIT:
            await self.handle_submit(request, reader, writer)
        elif request_type == messages.STATUS:
            await self.handle_status(request, writer)
        elif request_type == messages.DOWNLOAD:
            await self.handle_download(request, writer)
        elif request_type == messages.HISTORY:
            await self.handle_history(request, writer)
        else:
            await self.send_error(
                writer, messages.BAD_REQUEST, f"no conozco el pedido '{request_type}'"
            )

    async def handle_submit(
        self,
        request: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Recibe una imagen, la guarda y devuelve un trabajo nuevo."""
        job_id = str(uuid.uuid4())
        upload_directory = self.storage_dir / "uploads" / job_id
        upload_directory.mkdir(parents=True, exist_ok=True)

        upload_path = upload_directory / str(request.get("filename", "recibida.bin"))
        received_bytes = 0

        with open(upload_path, "wb") as image_file:
            async for chunk in protocol.stream_payload(
                reader, protocol.payload_size_of(request)
            ):
                image_file.write(chunk)
                received_bytes += len(chunk)

        operation = str(request.get("op", ""))
        if operation not in config.OPERATION_PARAMETERS:
            await self.send_error(
                writer, messages.UNKNOWN_OP, f"la operación '{operation}' no existe"
            )
            return

        self.jobs[job_id] = SimulatedJob(
            job_id, str(request.get("user", "")), operation,
            str(request.get("filename", "")), upload_path,
        )
        print(f"      trabajo {job_id[:8]}… ({received_bytes} bytes, {operation})")

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": job_id,
            "status": messages.QUEUED,
            "deduplicated": False,
        })

    async def handle_status(
        self, request: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        """Informa el estado simulado de un trabajo."""
        job = self.resolve_job(request)
        if job is None:
            await self.send_error(writer, messages.JOB_NOT_FOUND, "")
            return

        status = job.status(self.processing_seconds)
        response: dict[str, Any] = {
            messages.TYPE_FIELD: messages.OK, "job_id": job.job_id, "status": status,
        }

        if status == messages.DONE:
            response["has_output"] = job.has_output
            response["result"] = job.result

        await protocol.send_message(writer, response)

    async def handle_download(
        self, request: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        """Devuelve la imagen recibida, haciéndola pasar por el resultado."""
        job = self.resolve_job(request)
        if job is None:
            await self.send_error(writer, messages.JOB_NOT_FOUND, "")
            return

        if job.status(self.processing_seconds) != messages.DONE:
            await self.send_error(writer, messages.NOT_READY, "")
            return
        if not job.has_output:
            await self.send_error(writer, messages.NO_OUTPUT, "")
            return

        suggested_name = f"{Path(job.filename).stem}_{job.operation}{Path(job.filename).suffix}"
        await protocol.send_file(writer, {
            messages.TYPE_FIELD: messages.OK,
            "job_id": job.job_id,
            "filename": suggested_name,
            "content_type": job.result.get("content_type", "application/octet-stream"),
        }, job.upload_path)

    async def handle_history(
        self, request: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        """Lista los trabajos del usuario, del más reciente al más antiguo."""
        user = str(request.get("user", ""))
        limit = int(request.get("limit", 10))

        listed = [
            {
                "job_id": job.job_id,
                "op": job.operation,
                "status": job.status(self.processing_seconds),
                "filename": job.filename,
                "created_at": job.created_at.isoformat(timespec="seconds"),
                "finished_at": (
                    (job.created_at + timedelta(
                        seconds=QUEUED_SECONDS + self.processing_seconds
                    )).isoformat(timespec="seconds")
                    if job.status(self.processing_seconds) == messages.DONE else None
                ),
            }
            for job in reversed(list(self.jobs.values()))
            if job.user == user
        ]

        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.OK, "jobs": listed[:limit],
        })

    def resolve_job(self, request: dict[str, Any]) -> SimulatedJob | None:
        """Busca el trabajo del pedido, verificando que sea del usuario que pregunta."""
        job = self.jobs.get(str(request.get("job_id", "")))
        if job is None or job.user != str(request.get("user", "")):
            return None
        return job

    async def send_error(
        self, writer: asyncio.StreamWriter, code: str, detail: str
    ) -> None:
        """Responde un mensaje de error del protocolo."""
        await protocol.send_message(writer, {
            messages.TYPE_FIELD: messages.ERROR,
            "code": code,
            "message": detail or messages.ERROR_EXPLANATIONS.get(code, ""),
        })


async def serve(port: int, processing_seconds: float, storage_dir: Path) -> None:
    """Levanta el servidor y atiende hasta que lo interrumpan.

    Escucha con `host=None`, que en asyncio abre un socket dual-stack: acepta clientes
    IPv4 e IPv6 por el mismo puerto.
    """
    fake_server = FakeServer(processing_seconds, storage_dir)
    server = await asyncio.start_server(
        fake_server.handle_client, config.LISTEN_ON_ALL_INTERFACES, port
    )

    print(f"Servidor de mentira escuchando en el puerto {port} (IPv4 e IPv6).")
    print(f"Un trabajo tarda {QUEUED_SECONDS:.0f} s en cola y {processing_seconds:.0f} s procesando.")
    print("Ctrl+C para cortar.\n")

    async with server:
        await server.serve_forever()


def main() -> None:
    """Parsea los argumentos y arranca el servidor."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.fake_server",
        description="Servidor de mentira para probar el cliente. No procesa imágenes.",
    )
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument(
        "--processing-seconds", type=float, default=DEFAULT_PROCESSING_SECONDS,
        help="cuánto tarda un trabajo simulado en terminar",
    )
    arguments = parser.parse_args()

    try:
        asyncio.run(serve(arguments.port, arguments.processing_seconds, config.STORAGE_DIR))
    except KeyboardInterrupt:
        print("\nCortado.")


if __name__ == "__main__":
    main()
