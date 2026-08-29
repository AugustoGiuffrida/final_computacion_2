"""Pruebas del registro permanente en SQLite.

Se usa una base de verdad en un archivo temporal: SQLite es un archivo, así que simularlo
no ahorraría nada y taparía justo lo que hay que verificar.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.common import messages
from app.server import database, ipc


class DatabaseTestCase(unittest.TestCase):
    """Base con una carpeta temporal y un escritor abierto."""

    def setUp(self) -> None:
        """Crea la carpeta temporal y abre la base."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)
        self.database_path = self.working_directory / "jobs.db"
        self.writer = database.JobWriter(self.database_path)

    def tearDown(self) -> None:
        """Cierra la base y borra la carpeta."""
        self.writer.close()
        self._temporary_directory.cleanup()

    def a_request(
        self,
        job_id: str = "job-1",
        user: str = "ana",
        operation: str = "anonymize",
        parameters: dict | None = None,
    ) -> ipc.ReviewRequest:
        """Arma un pedido de revisión con los campos indicados."""
        return ipc.ReviewRequest(
            job_id=job_id,
            user=user,
            operation=operation,
            parameters={"mode": "blur"} if parameters is None else parameters,
            stored_path=self.working_directory / "foto.jpg",
        )

    def a_finished_job(self, request: ipc.ReviewRequest, content_hash: str) -> None:
        """Inserta un trabajo y lo marca como terminado.

        Hace falta porque la deduplicación solo reutiliza trabajos en `DONE`, y todavía no
        existe nada que lleve un trabajo hasta ahí: eso son los workers.
        """
        self.writer.insert(request, content_hash)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?", (messages.DONE, request.job_id)
        )
        connection.commit()
        connection.close()


# ──────────────────────── la forma canónica de los parámetros ────────────────────────


class CanonicalParameters(DatabaseTestCase):
    """El texto con el que se comparan los parámetros al buscar duplicados."""

    def test_the_order_of_the_keys_does_not_matter(self) -> None:
        """Dos diccionarios iguales escritos distinto dan el mismo texto.

        Es lo que permite comparar los parámetros como texto en la consulta SQL en vez de
        tener una columna por parámetro posible.
        """
        primero = database.canonical_parameters({"mode": "blur", "strength": 15})
        segundo = database.canonical_parameters({"strength": 15, "mode": "blur"})

        self.assertEqual(primero, segundo)

    def test_different_values_give_different_text(self) -> None:
        """Cambiar un valor cambia el texto, que es lo que rompe la coincidencia."""
        self.assertNotEqual(
            database.canonical_parameters({"strength": 15}),
            database.canonical_parameters({"strength": 30}),
        )


# ──────────────────────── la búsqueda de duplicados ────────────────────────


class DuplicateSearch(DatabaseTestCase):
    """Cuándo un envío se considera repetido y cuándo no."""

    def test_the_same_four_fields_are_a_duplicate(self) -> None:
        """Mismo usuario, mismo contenido, misma operación y mismos parámetros."""
        self.a_finished_job(self.a_request("job-viejo"), "a" * 64)

        encontrado = self.writer.find_duplicate("ana", "a" * 64, "anonymize", {"mode": "blur"})

        self.assertEqual(encontrado, "job-viejo")

    def test_a_job_that_did_not_finish_is_not_reused(self) -> None:
        """Solo se reutilizan trabajos en DONE.

        Uno que falló no sirve, y uno en curso tampoco: se procesa de nuevo en vez de
        esperar a que termine el que ya está corriendo.
        """
        self.writer.insert(self.a_request("job-en-curso"), "a" * 64)  # queda en QUEUED

        self.assertIsNone(
            self.writer.find_duplicate("ana", "a" * 64, "anonymize", {"mode": "blur"})
        )

    def test_another_user_does_not_see_the_duplicate(self) -> None:
        """La deduplicación filtra por usuario, y el motivo es la privacidad.

        Reutilizar el trabajo de otro le revelaría que procesó esa misma imagen.
        """
        self.a_finished_job(self.a_request("job-de-ana", user="ana"), "a" * 64)

        self.assertIsNone(
            self.writer.find_duplicate("luis", "a" * 64, "anonymize", {"mode": "blur"})
        )

    def test_a_different_parameter_is_not_a_duplicate(self) -> None:
        """La misma foto con otra intensidad da otro resultado, así que es otro trabajo."""
        self.a_finished_job(
            self.a_request("job-viejo", parameters={"strength": 15}), "a" * 64
        )

        self.assertIsNone(
            self.writer.find_duplicate("ana", "a" * 64, "anonymize", {"strength": 30})
        )

    def test_a_different_operation_is_not_a_duplicate(self) -> None:
        """El mismo contenido con otra operación tampoco se reutiliza."""
        self.a_finished_job(self.a_request("job-viejo", operation="anonymize"), "a" * 64)

        self.assertIsNone(
            self.writer.find_duplicate("ana", "a" * 64, "clean", {"mode": "blur"})
        )

    def test_the_keys_in_another_order_still_match(self) -> None:
        """Guardar y buscar con las claves en distinto orden encuentra el duplicado."""
        self.a_finished_job(
            self.a_request("job-viejo", parameters={"mode": "blur", "strength": 15}),
            "a" * 64,
        )

        encontrado = self.writer.find_duplicate(
            "ana", "a" * 64, "anonymize", {"strength": 15, "mode": "blur"}
        )

        self.assertEqual(encontrado, "job-viejo")


# ──────────────────────── escribir y leer ────────────────────────


class WritingAndReading(DatabaseTestCase):
    """Lo que guarda el escritor es lo que encuentra el lector."""

    def test_an_inserted_job_can_be_read_back(self) -> None:
        """Los campos vuelven tal como se guardaron."""
        self.writer.insert(self.a_request("job-1", user="ana"), "b" * 64)

        fila = database.JobReader(self.database_path).find("job-1")

        self.assertEqual(fila["user"], "ana")
        self.assertEqual(fila["op"], "anonymize")
        self.assertEqual(fila["sha256"], "b" * 64)
        self.assertEqual(fila["status"], messages.QUEUED)

    def test_an_unknown_job_is_not_found(self) -> None:
        """Un identificador que no está devuelve None, no una excepción."""
        self.assertIsNone(database.JobReader(self.database_path).find("no-existe"))

    def test_the_reader_cannot_write(self) -> None:
        """La regla del escritor único la hace cumplir SQLite, no una convención nuestra.

        El lector abre la base en modo solo lectura, y cualquier intento de escribir
        termina en un error del motor.
        """
        reader = database.JobReader(self.database_path)
        reader.find("lo que sea")  # fuerza la apertura

        with self.assertRaises(sqlite3.OperationalError):
            reader._connection.execute("DELETE FROM jobs")

        reader.close()

    def test_a_database_that_does_not_exist_yet_is_not_an_error(self) -> None:
        """El servidor puede arrancar antes de que el hijo cree la base.

        Sin esto, cada consulta durante esos primeros milisegundos fallaría con un error
        del motor en vez de responder que el trabajo no existe.
        """
        reader = database.JobReader(self.working_directory / "todavia-no.db")

        self.assertIsNone(reader.find("job-1"))


if __name__ == "__main__":
    unittest.main()
