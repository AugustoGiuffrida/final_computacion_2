"""Pruebas del registro de trabajos en memoria.

No necesitan red ni disco: el registro es una estructura de datos y se prueba como tal.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import tempfile
from pathlib import Path

from app.common import messages
from app.server import database, ipc
from app.server.main.registry import Job, JobRegistry, format_timestamp, new_job


def a_job(user: str = "augusto", operation: str = "anonymize") -> Job:
    """Crea un trabajo de ejemplo.

    Args:
        user: Dueño del trabajo.
        operation: Operación pedida.

    Returns:
        Un trabajo nuevo, en estado `QUEUED`.
    """
    return new_job(user, operation, {"mode": "blur"}, "foto.jpg")


class Creation(unittest.TestCase):
    """Cómo nace un trabajo."""

    def test_a_new_job_starts_queued(self) -> None:
        """Todo trabajo arranca encolado: nadie lo tomó todavía."""
        self.assertEqual(a_job().status, messages.QUEUED)

    def test_each_job_gets_its_own_identifier(self) -> None:
        """Dos trabajos seguidos no comparten identificador.

        Se generan con UUID v4, sin coordinación con nada: no hay contador que llevar ni
        consulta que hacer antes de responderle al cliente.
        """
        identifiers = {a_job().job_id for _ in range(100)}

        self.assertEqual(len(identifiers), 100)

    def test_a_new_job_has_no_result_yet(self) -> None:
        """Un trabajo recién creado no tiene fin, ni error, ni archivo de salida."""
        job = a_job()

        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.error)
        self.assertIsNone(job.output_path)


class Summary(unittest.TestCase):
    """La vista del trabajo que viaja hacia el cliente."""

    def test_the_internal_name_is_translated_to_the_protocol_one(self) -> None:
        """El campo interno `operation` viaja como `op`."""
        summary = a_job(operation="compress").as_summary()

        self.assertEqual(summary["op"], "compress")
        self.assertNotIn("operation", summary)

    def test_the_output_path_never_reaches_the_client(self) -> None:
        """La ruta interna del archivo no sale del servidor."""
        job = a_job()
        job.output_path = "/storage/results/a3f7/out.jpg"  # type: ignore[assignment]

        self.assertNotIn("output_path", job.as_summary())

    def test_the_optional_fields_only_appear_when_they_have_a_value(self) -> None:
        """Un trabajo en curso no manda `finished_at` ni `error` en nulo."""
        summary = a_job().as_summary()

        self.assertNotIn("finished_at", summary)
        self.assertNotIn("error", summary)

    def test_a_finished_job_reports_when_and_how_it_ended(self) -> None:
        """Al terminar, el resumen incluye el momento y el motivo si falló."""
        job = a_job()
        job.status = messages.FAILED
        job.finished_at = datetime.now(timezone.utc)
        job.error = "imagen corrupta"

        summary = job.as_summary()

        self.assertEqual(summary["status"], messages.FAILED)
        self.assertEqual(summary["error"], "imagen corrupta")
        self.assertIn("finished_at", summary)

    def test_the_timestamp_travels_as_text(self) -> None:
        """Las fechas se serializan en ISO 8601, que JSON puede transportar."""
        moment = datetime(2026, 8, 22, 15, 48, 27, tzinfo=timezone.utc)

        self.assertEqual(format_timestamp(moment), "2026-08-22T15:48:27+00:00")


class Lookup(unittest.TestCase):
    """Búsqueda de un trabajo por su identificador."""

    def setUp(self) -> None:
        """Deja un registro con un trabajo de cada usuario."""
        self.registry = JobRegistry()
        self.own_job = a_job(user="augusto")
        self.other_job = a_job(user="ana")

        self.registry.add(self.own_job)
        self.registry.add(self.other_job)

    def test_a_job_is_found_by_its_identifier(self) -> None:
        """El dueño encuentra su trabajo."""
        found = self.registry.find("augusto", self.own_job.job_id)

        self.assertIs(found, self.own_job)

    def test_an_unknown_identifier_is_reported_as_not_found(self) -> None:
        """Un identificador inventado no existe."""
        with self.assertRaises(messages.RequestError) as raised:
            self.registry.find("augusto", "no-existe")

        self.assertEqual(raised.exception.code, messages.JOB_NOT_FOUND)

    def test_a_job_of_another_user_is_forbidden(self) -> None:
        """El trabajo existe, pero no es de quien pregunta.

        Es la regla de propiedad, y vive en el registro para que no dependa de que cada
        manejador se acuerde de verificarla.
        """
        with self.assertRaises(messages.RequestError) as raised:
            self.registry.find("augusto", self.other_job.job_id)

        self.assertEqual(raised.exception.code, messages.FORBIDDEN)


class Listing(unittest.TestCase):
    """Listado de los trabajos de un usuario."""

    def test_an_empty_registry_lists_nothing(self) -> None:
        """Un usuario sin trabajos recibe una lista vacía, no un error."""
        self.assertEqual(JobRegistry().list_for("augusto", 10), [])

    def test_only_the_jobs_of_that_user_are_listed(self) -> None:
        """Los trabajos de otros no aparecen."""
        registry = JobRegistry()
        registry.add(a_job(user="augusto"))
        registry.add(a_job(user="ana"))
        registry.add(a_job(user="augusto"))

        listed = registry.list_for("augusto", 10)

        self.assertEqual(len(listed), 2)
        self.assertTrue(all(job.user == "augusto" for job in listed))

    def test_the_most_recent_job_comes_first(self) -> None:
        """El historial se lee de lo más nuevo a lo más viejo."""
        registry = JobRegistry()
        first = a_job(operation="anonymize")
        second = a_job(operation="clean")
        third = a_job(operation="compress")

        for job in (first, second, third):
            registry.add(job)

        listed = registry.list_for("augusto", 10)

        self.assertEqual([job.operation for job in listed], ["compress", "clean", "anonymize"])

    def test_the_limit_cuts_the_list(self) -> None:
        """Se devuelven a lo sumo tantos trabajos como se hayan pedido."""
        registry = JobRegistry()
        for _ in range(10):
            registry.add(a_job())

        self.assertEqual(len(registry.list_for("augusto", 3)), 3)

    def test_the_limit_cuts_the_most_recent_ones(self) -> None:
        """Al recortar se conservan los más recientes, no los primeros."""
        registry = JobRegistry()
        for operation in ("anonymize", "clean", "compress"):
            registry.add(a_job(operation=operation))

        listed = registry.list_for("augusto", 2)

        self.assertEqual([job.operation for job in listed], ["compress", "clean"])



class ResolutionAgainstTheArchive(unittest.TestCase):
    """Cuando el trabajo no está en memoria, se lo busca en la base."""

    def setUp(self) -> None:
        """Crea una base temporal con un trabajo dentro."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)
        self.database_path = self.working_directory / "jobs.db"

        writer = database.JobWriter(self.database_path)
        writer.insert(
            ipc.ReviewRequest(
                job_id="job-viejo",
                user="ana",
                operation="clean",
                parameters={},
                stored_path=self.working_directory / "foto.jpg",
            ),
            "c" * 64,
        )
        writer.close()

        self.addCleanup(self._temporary_directory.cleanup)

    def test_a_job_from_a_previous_run_is_found(self) -> None:
        """Un trabajo que no está en memoria se recupera de la base.

        Es lo que permite consultar el estado de algo enviado antes de reiniciar el
        servidor, en vez de responder que no existe.
        """
        registro = JobRegistry(database.JobReader(self.database_path))

        job = registro.find("ana", "job-viejo")

        self.assertEqual(job.operation, "clean")
        self.assertEqual(job.content_hash, "c" * 64)

    def test_the_ownership_rule_applies_to_the_archive_too(self) -> None:
        """Venir de la base no exime de la regla de propiedad."""
        registro = JobRegistry(database.JobReader(self.database_path))

        with self.assertRaises(messages.RequestError) as raised:
            registro.find("luis", "job-viejo")

        self.assertEqual(raised.exception.code, messages.FORBIDDEN)

    def test_without_an_archive_only_memory_is_searched(self) -> None:
        """Un registro sin base es solo memoria, que es lo que usan las pruebas."""
        registro = JobRegistry()

        with self.assertRaises(messages.RequestError) as raised:
            registro.find("ana", "job-viejo")

        self.assertEqual(raised.exception.code, messages.JOB_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
