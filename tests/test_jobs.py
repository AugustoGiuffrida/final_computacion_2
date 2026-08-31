"""Pruebas del puente con la cola de tareas.

El monitor se prueba llamando a `_apply` con estados fabricados: la traducción de estados
es lógica pura y no necesita Redis. Que la cola real funcione se verifica aparte, con un
worker vivo (docs/05).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.common import config, messages
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

    def test_every_operation_of_the_catalog_is_available(self) -> None:
        """Las seis operaciones del protocolo tienen con qué ejecutarse."""
        queue = TaskQueue()

        for operation in config.OPERATION_PARAMETERS:
            with self.subTest(operation=operation):
                self.assertTrue(queue.accepts(operation))

    def test_sanitize_is_accepted_even_though_it_is_not_a_single_task(self) -> None:
        """`sanitize` no está en el diccionario: es una cadena, y se acepta igual."""
        self.assertNotIn("sanitize", jobs.TASK_FOR_OPERATION)
        self.assertTrue(TaskQueue().accepts("sanitize"))

    def test_an_operation_that_does_not_exist_is_rejected(self) -> None:
        """Cualquier otra cosa, no."""
        self.assertFalse(TaskQueue().accepts("inventada"))


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



class FakeHandle:
    """Un asidero de Celery de mentira, con su enlace hacia la tarea anterior.

    No se usa `mock.Mock` a propósito: `parent` es un parámetro real de su constructor
    —sirve para el rastreo de llamadas—, así que `Mock(parent=X).parent` devuelve un mock
    nuevo en lugar de X, y recorrer la cadena no termina nunca.

    Attributes:
        state: El estado que informa esta tarea.
        parent: La tarea anterior de la cadena, o None si es la primera.
    """

    def __init__(self, state: str, parent: FakeHandle | None = None) -> None:
        self.state = state
        self.parent = parent


class ChainStateWalking(unittest.TestCase):
    """Cómo se descubre que una cadena arrancó, cuando su última tarea sigue esperando."""

    def a_handle(self, *states: str) -> FakeHandle:
        """Arma una cadena falsa de asideros, de la última a la primera.

        Args:
            *states: Los estados, empezando por la última tarea de la cadena.

        Returns:
            El asidero de la última, enlazado a sus padres por `.parent`.
        """
        handle = None
        for state in reversed(states):
            handle = FakeHandle(state, handle)

        return handle

    def test_a_single_task_has_no_ancestors(self) -> None:
        """Una tarea suelta no tiene padres: nunca se la confunde con una cadena."""
        self.assertFalse(jobs.has_started(FakeHandle("PENDING")))

    def test_a_chain_that_has_not_started_reports_false(self) -> None:
        """Con las tres etapas esperando, la cadena todavía no arrancó."""
        handle = self.a_handle("PENDING", "PENDING", "PENDING")

        self.assertFalse(jobs.has_started(handle))

    def test_a_chain_whose_first_stage_runs_reports_true(self) -> None:
        """La primera etapa corriendo alcanza para informar PROCESSING.

        Sin esto, un saneamiento se vería QUEUED durante casi todo su procesamiento: el
        asidero que devuelve un `chain` es el de la última tarea, que espera a las otras.
        """
        handle = self.a_handle("PENDING", "PENDING", "STARTED")

        self.assertTrue(jobs.has_started(handle))

    def test_a_chain_with_finished_stages_reports_true(self) -> None:
        """Etapas ya terminadas también cuentan como cadena en marcha."""
        handle = self.a_handle("PENDING", "STARTED", "SUCCESS")

        self.assertTrue(jobs.has_started(handle))


if __name__ == "__main__":
    unittest.main()
