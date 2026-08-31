"""El puente con la cola de tareas: encolar los trabajos y vigilar cómo avanzan.

Dos responsabilidades que van juntas porque comparten estado —los trabajos en vuelo—:

- **Encolar**: al aceptar un trabajo, mandar su tarea al broker con `.delay()`.
- **El monitor**: una corrutina de fondo que consulta el result backend y traduce los
  estados de Celery a los del protocolo: STARTED → PROCESSING, SUCCESS → DONE,
  FAILURE → ERROR.

El monitor existe por una limitación concreta: los workers no pueden avisarle al
servidor —pueden estar en otra máquina o en otro contenedor—. El result backend es el
lugar común: el worker deja ahí su estado, y alguien de este lado tiene que preguntar.

El cliente de Redis que usa Celery es bloqueante, así que tanto encolar como consultar
van a un hilo del pool con `asyncio.to_thread`: un Redis caído no debe congelar el event
loop. Es el mismo criterio de todo el servidor, aplicado a la única biblioteca del
proyecto que no tiene versión asíncrona.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery.result import AsyncResult

from app.common import messages
from app.server.main import registry
from app.worker import tasks

logger = logging.getLogger(__name__)

# Qué tarea ejecuta cada operación del catálogo. Encolar es buscar acá y llamar
# `.delay()`, que es la forma de la guía de la cátedra.
TASK_FOR_OPERATION = {
    "anonymize": tasks.anonymize,
    "inspect": tasks.inspect,
    "clean": tasks.clean,
    "compress": tasks.compress,
    "convert": tasks.convert,
    # ESTADO: falta 'sanitize', que encadena las otras tres.
}

# Cada cuánto mira el monitor los trabajos en vuelo. Más chico, los cambios de estado se
# ven antes; más grande, menos consultas a Redis. Medio segundo no se nota en ninguna de
# las dos direcciones.
MONITOR_INTERVAL_SECONDS = 0.5


class TaskQueue:
    """La cola de tareas vista desde el servidor: encolar, vigilar y traducir estados.

    Attributes:
        in_flight: Cuántos trabajos están encolados o procesándose en este momento.
    """

    def __init__(self) -> None:
        """Prepara la cola, sin monitor todavía."""
        # Un asidero de Celery por trabajo en vuelo: es lo que permite preguntarle al
        # backend por su estado. Se saca de acá cuando el trabajo termina, bien o mal.
        self._handles: dict[str, AsyncResult] = {}
        self._registry: registry.JobRegistry | None = None
        self._monitor: asyncio.Task[None] | None = None

    @property
    def in_flight(self) -> int:
        """Cuántos trabajos están bajo vigilancia en este momento."""
        return len(self._handles)

    def accepts(self, operation: str) -> bool:
        """Si la operación ya tiene una tarea que la ejecute.

        Args:
            operation: Una operación del catálogo del protocolo.

        Returns:
            False para las que todavía no están implementadas en los workers.
        """
        return operation in TASK_FOR_OPERATION

    def start(self, jobs: registry.JobRegistry) -> None:
        """Arranca el monitor. Se llama una vez, desde dentro del event loop.

        Args:
            jobs: El índice en memoria que el monitor actualiza al detectar cambios.
        """
        self._registry = jobs
        self._monitor = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        """Frena el monitor. Los trabajos ya encolados siguen su curso en los workers."""
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None

    async def enqueue(self, job: registry.Job, stored_path: Path) -> None:
        """Manda la tarea del trabajo al broker y lo deja bajo vigilancia.

        Args:
            job: El trabajo recién aceptado, ya registrado en el índice.
            stored_path: Dónde quedó la imagen. Viaja como texto: la cola es JSON.
        """
        task = TASK_FOR_OPERATION[job.operation]

        # `delay` publica el mensaje en Redis y es bloqueante: al hilo del pool.
        handle = await asyncio.to_thread(
            task.delay, job.job_id, str(stored_path), job.parameters
        )
        self._handles[job.job_id] = handle
        logger.info("trabajo %s encolado (tarea %s)", job.job_id, handle.id)

    async def _watch(self) -> None:
        """Bucle del monitor: consulta el backend y aplica lo que cambió."""
        while True:
            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)

            if not self._handles:
                continue

            try:
                # Todas las consultas del ciclo van juntas al hilo del pool: un solo
                # cruce por vuelta, no uno por trabajo.
                snapshots = await asyncio.to_thread(self._poll_backend)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis caído o inalcanzable: se registra y se reintenta en la próxima
                # vuelta. Los clientes no se enteran: sus consultas responden el último
                # estado conocido.
                logger.exception("no se pudo consultar el result backend; se reintenta")
                continue

            for job_id, state, payload in snapshots:
                self._apply(job_id, state, payload)

    def _poll_backend(self) -> list[tuple[str, str, Any]]:
        """Lee el estado de cada trabajo en vuelo. Corre en un hilo del pool.

        Returns:
            Una foto por trabajo: `(job_id, estado de Celery, resultado o None)`. El
            resultado solo se trae cuando el estado es terminal.
        """
        snapshots = []
        for job_id, handle in self._handles.items():
            state = handle.state
            payload = handle.result if state in ("SUCCESS", "FAILURE") else None
            snapshots.append((job_id, state, payload))

        return snapshots

    def _apply(self, job_id: str, state: str, payload: Any) -> None:
        """Traduce un estado de Celery al trabajo del índice. Corre en el event loop.

        Args:
            job_id: El trabajo al que pertenece la foto.
            state: Estado de Celery: PENDING, STARTED, SUCCESS, FAILURE…
            payload: Lo que devolvió la tarea (SUCCESS) o la excepción (FAILURE).
        """
        job = self._registry.get(job_id)
        if job is None:
            self._handles.pop(job_id, None)
            return

        if state == "STARTED" and job.status == messages.QUEUED:
            job.status = messages.PROCESSING
            logger.info("trabajo %s en proceso", job_id)

        elif state == "SUCCESS":
            job.status = messages.DONE
            job.finished_at = datetime.now(timezone.utc)
            job.result = (payload or {}).get("result")
            output = (payload or {}).get("output_path")
            job.output_path = Path(output) if output else None
            self._handles.pop(job_id, None)
            logger.info("trabajo %s terminado", job_id)

        elif state == "FAILURE":
            job.status = messages.FAILED
            job.finished_at = datetime.now(timezone.utc)
            job.error = str(payload)  # la excepción que levantó la tarea
            self._handles.pop(job_id, None)
            logger.warning("trabajo %s falló: %s", job_id, job.error)
