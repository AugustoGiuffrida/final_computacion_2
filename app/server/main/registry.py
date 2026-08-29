from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.common import messages
from app.common.messages import Forbidden, JobNotFound
from app.server import database


@dataclass
class Job:
    """Un trabajo aceptado por el servidor.

    Attributes:
        job_id: Identificador único que el servidor generó al aceptarlo.
        user: Quién lo creó. Solo él puede consultarlo y descargarlo.
        operation: Qué se pidió hacer: 'anonymize', 'clean', 'compress'…
        parameters: Los parámetros de esa operación.
        filename: Nombre del archivo tal como lo mandó el cliente. Es informativo: en
            disco el archivo se guarda bajo el `job_id`, no bajo este nombre.
        status: Uno de los cuatro estados del protocolo.
        created_at: Cuándo se aceptó, en UTC.
        finished_at: Cuándo terminó, o None si sigue en curso.
        error: Motivo del fallo, o None si no falló.
        result: Datos que produjo la operación —cuántas caras se detectaron, qué
            metadatos se eliminaron—, o None si todavía no terminó. No entra en el
            resumen: el historial lista trabajos, no resultados.
        output_path: Dónde quedó el archivo producido, o None si todavía no hay.
        content_hash: SHA-256 del contenido, calculado por el proceso de ingreso. Es lo
            que permite reconocer que dos envíos son la misma imagen aunque lleguen con
            nombres distintos. No entra en el resumen: es un dato interno.
    """

    job_id: str
    user: str
    operation: str
    parameters: dict[str, Any]
    filename: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    output_path: Path | None = None
    content_hash: str | None = None

    def as_summary(self) -> dict[str, Any]:
        """Devuelve la vista del trabajo que viaja hacia el cliente.

        Es el punto donde se traduce el nombre interno al del protocolo —`operation`
        viaja como `op`— y donde se decide qué no sale: `output_path` es una ruta interna
        del servidor, inútil para el cliente y que expondría su organización de archivos.

        Los campos opcionales solo aparecen si tienen valor, para no mandar nulos que el
        cliente tendría que interpretar.

        Returns:
            Los campos del trabajo, con las fechas en formato ISO.
        """
        summary: dict[str, Any] = {
            "job_id": self.job_id,
            "op": self.operation,
            "status": self.status,
            "filename": self.filename,
            "created_at": format_timestamp(self.created_at),
        }

        if self.finished_at is not None:
            summary["finished_at"] = format_timestamp(self.finished_at)
        if self.error is not None:
            summary["error"] = self.error

        return summary


def format_timestamp(moment: datetime) -> str:
    """Convierte un instante al formato de texto que viaja por el protocolo.

    Args:
        moment: El instante a convertir.

    Returns:
        La fecha y hora en formato ISO 8601, con precisión de segundos.
    """
    return moment.isoformat(timespec="seconds")


def new_job(
    user: str, operation: str, parameters: dict[str, Any], filename: str
) -> Job:
    """Crea un trabajo nuevo, ya con su identificador y su estado inicial.

    Concentra acá las dos decisiones que quien lo pide no tiene por qué conocer: que el
    identificador es un UUID versión 4 —aleatorio, no enumerable y generado sin
    coordinación con nadie— y que todo trabajo nace en `QUEUED`.

    Args:
        user: Quién lo pide.
        operation: Qué operación aplicar.
        parameters: Los parámetros de esa operación.
        filename: Nombre del archivo tal como lo mandó el cliente.

    Returns:
        El trabajo listo para agregar al registro.
    """
    return Job(
        job_id=str(uuid.uuid4()),
        user=user,
        operation=operation,
        parameters=parameters,
        filename=filename,
        status=messages.QUEUED,
        created_at=datetime.now(timezone.utc),
    )


def job_from_row(row: dict[str, Any]) -> Job:
    """Reconstruye un trabajo a partir de su fila en la base.

    Args:
        row: Los campos tal como los devuelve `database.JobReader`.

    Returns:
        El trabajo, con los textos de la base ya convertidos a `datetime` y `Path`.
    """
    return Job(
        job_id=row["id"],
        user=row["user"],
        operation=row["op"],
        parameters=json.loads(row["params"]),
        filename=row["filename"] or "",
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        finished_at=(
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        ),
        error=row["error"],
        output_path=Path(row["result_path"]) if row["result_path"] else None,
        content_hash=row["sha256"],
    )


class JobRegistry:
    """Los trabajos que el servidor aceptó desde que arrancó.

    Por dentro es un diccionario con el `job_id` como clave, que es el único dato que el
    cliente manda al consultar o descargar. Como los diccionarios de Python conservan el
    orden de inserción, recorrerlo al revés da los trabajos del más reciente al más
    antiguo, que es el orden que pide el historial.

    No hace falta ningún candado: el servidor corre sobre un solo hilo, así que dos
    corrutinas nunca modifican el registro a la vez.
    """

    def __init__(self, archive: database.JobReader | None = None) -> None:
        """Crea un registro vacío.

        Args:
            archive: De dónde sacar los trabajos que no estén en memoria, típicamente de
                ejecuciones anteriores del servidor. Sin él, el registro es solo memoria,
                que es lo que alcanza para las pruebas.
        """
        self._jobs: dict[str, Job] = {}
        self._archive = archive

    def __len__(self) -> int:
        """Devuelve cuántos trabajos hay registrados."""
        return len(self._jobs)

    def add(self, job: Job) -> None:
        """Registra un trabajo recién aceptado.

        Args:
            job: El trabajo a guardar. Su `job_id` pasa a ser la clave.
        """
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job | None:
        """Busca un trabajo por identificador, solo en memoria y sin regla de propiedad.

        Es para uso interno del servidor —el monitor de la cola—, no para responder
        pedidos de clientes: esos pasan por `find`, que exige el usuario.
        """
        return self._jobs.get(job_id)

    def find(self, user: str, job_id: str) -> Job:
        """Busca un trabajo de un usuario.

        El usuario es obligatorio a propósito: así **no se puede consultar un trabajo sin
        declarar de quién sos**, y la regla de propiedad deja de depender de que cada
        manejador se acuerde de verificarla.

        Distinguir `FORBIDDEN` de `JOB_NOT_FOUND` le confirma a quien pregunta que ese
        identificador existe. Es un costo aceptado: el `job_id` es aleatorio y no
        enumerable, así que para llegar a esa respuesta ya haría falta conocerlo.

        Args:
            user: Quién pregunta.
            job_id: Identificador del trabajo buscado.

        Returns:
            El trabajo, si existe y es de ese usuario.

        Raises:
            JobNotFound: Si no existe un trabajo con ese identificador.
            Forbidden: Si el trabajo es de otro usuario.
        """
        job = self._jobs.get(job_id)

        if job is None and self._archive is not None:
            # No está en memoria: puede ser de una ejecución anterior del servidor.
            stored = self._archive.find(job_id)
            if stored is not None:
                job = job_from_row(stored)

        if job is None:
            raise JobNotFound(f"no existe un trabajo con el identificador '{job_id}'")
        if job.user != user:
            raise Forbidden()

        return job

    def list_for(self, user: str, limit: int) -> list[Job]:
        """Devuelve los últimos trabajos de un usuario, del más reciente al más antiguo.

        Recorre el registro completo filtrando por usuario. Con unos miles de trabajos el
        costo es imperceptible; cuando el historial crezca, los trabajos viejos saldrán de
        SQLite y en memoria quedará solo lo reciente.

        Args:
            user: De quién listar los trabajos.
            limit: Cuántos devolver como máximo.

        Returns:
            Los trabajos encontrados, ordenados del más reciente al más antiguo. Lista
            vacía si ese usuario todavía no hizo ninguno.
        """
        found: list[Job] = []

        for job in reversed(self._jobs.values()):
            if job.user != user:
                continue

            found.append(job)
            if len(found) == limit:
                break

        return found
