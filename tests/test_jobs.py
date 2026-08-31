"""Pruebas del puente con la cola de tareas.

El monitor se prueba llamando a `_apply` con estados fabricados: la traducción de estados
es lógica pura y no necesita Redis. Que la cola real funcione se verifica aparte, con un
worker vivo (docs/05).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.common import messages
from app.server.main import jobs, registry
from app.server.main.jobs import TaskQueue


class MonitorTranslation(unittest.TestCase):
    """Cómo el monitor traduce los estados de Celery a los del protocolo."""

    def setUp(self) -> None:
        """Arma una cola con un registro y un trabajo en vuelo."""
        self.registry = registry.JobRegistry()
        self.job = registry.new_job("ana", "clean", {}, "foto.jpg")
        self.registry.add(self.job)

        self.queue = TaskQueue()
        self.queue._registry = self.registry
        self.queue._handles[self.job.job_id] = object()  # el asidero no se usa en _apply

    def test_started_becomes_processing(self) -> None:
        """Un worker tomó el trabajo: el cliente lo ve PROCESSING."""
        self.queue._apply(self.job.job_id, "STARTED", None)

        self.assertEqual(self.job.status, messages.PROCESSING)
        self.assertEqual(self.queue.in_flight, 1)  # sigue bajo vigilancia

    def test_success_becomes_done_with_its_result(self) -> None:
        """La tarea terminó: estado, resultado, archivo y fecha de fin."""
        payload = {
            "output_path": "/tmp/resultado.jpg",
            "result": {"metadata_removed": True},
        }

        self.queue._apply(self.job.job_id, "SUCCESS", payload)

        self.assertEqual(self.job.status, messages.DONE)
        self.assertEqual(self.job.result, {"metadata_removed": True})
        self.assertEqual(self.job.output_path, Path("/tmp/resultado.jpg"))
        self.assertIsNotNone(self.job.finished_at)
        self.assertEqual(self.queue.in_flight, 0)  # ya no hay nada que vigilar

    def test_success_without_a_file_leaves_no_output_path(self) -> None:
        """`inspect` termina bien sin archivo: el estado no debe inventar uno."""
        self.queue._apply(self.job.job_id, "SUCCESS", {"result": {"has_gps": False}})

        self.assertEqual(self.job.status, messages.DONE)
        self.assertIsNone(self.job.output_path)

    def test_failure_becomes_failed_with_the_reason(self) -> None:
        """La tarea reventó: el motivo queda en el trabajo, legible para el cliente."""
        self.queue._apply(self.job.job_id, "FAILURE", ValueError("imagen corrupta"))

        self.assertEqual(self.job.status, messages.FAILED)
        self.assertIn("imagen corrupta", self.job.error)
        self.assertEqual(self.queue.in_flight, 0)

    def test_pending_changes_nothing(self) -> None:
        """Mientras nadie la tome, el trabajo sigue QUEUED."""
        self.queue._apply(self.job.job_id, "PENDING", None)

        self.assertEqual(self.job.status, messages.QUEUED)

    def test_a_job_that_left_the_registry_is_dropped(self) -> None:
        """Un trabajo que ya no está en el índice deja de vigilarse, sin error."""
        self.queue._handles["fantasma"] = object()

        self.queue._apply("fantasma", "SUCCESS", {})

        self.assertNotIn("fantasma", self.queue._handles)


class OperationAvailability(unittest.TestCase):
    """Qué operaciones tienen tarea, mientras falten las de OpenCV."""

    def test_the_implemented_operations_are_available(self) -> None:
        """Las cinco que ya tienen tarea."""
        queue = TaskQueue()

        for operation in ("inspect", "clean", "compress", "convert", "anonymize"):
            self.assertTrue(queue.accepts(operation))

    def test_sanitize_is_not_yet(self) -> None:
        """`sanitize` encadena las otras y todavía no está."""
        self.assertFalse(TaskQueue().accepts("sanitize"))


class Enqueueing(unittest.IsolatedAsyncioTestCase):
    """El encolado guarda el asidero para poder vigilar el trabajo."""

    async def test_enqueue_stores_the_handle_under_the_job_id(self) -> None:
        """Después de encolar, el trabajo figura en vuelo."""
        queue = TaskQueue()
        job = registry.new_job("ana", "clean", {}, "foto.jpg")
        fake_handle = mock.Mock(id="task-123")

        with mock.patch.dict(
            jobs.TASK_FOR_OPERATION, {"clean": mock.Mock(delay=lambda *a: fake_handle)}
        ):
            await queue.enqueue(job, Path("/tmp/foto.jpg"))

        self.assertEqual(queue.in_flight, 1)
        self.assertIs(queue._handles[job.job_id], fake_handle)


if __name__ == "__main__":
    unittest.main()
