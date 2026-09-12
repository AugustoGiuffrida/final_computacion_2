"""Pruebas de la presentación de los resultados.

Lo que se verifica es la correspondencia con el worker: cada campo que emiten las tareas
tiene su etiqueta, y no hay etiquetas para campos que ninguna tarea emite. Es el desfase
que no se ve en ninguna otra prueba, porque el cliente y el worker no se cruzan en ellas.
"""

from __future__ import annotations

import unittest

from app.client import formatting

# Los campos que emiten las tareas de `worker/tasks.py`, copiados a mano. Si una tarea
# agrega uno, la prueba de abajo pide que se agregue acá y en la tabla de etiquetas.
FIELDS_EMITTED_BY_THE_WORKER = frozenset({
    "format", "size", "mode", "metadata_entries", "bytes",
    "gps", "taken_at", "camera", "serial_number",
    "faces_detected", "metadata_removed",
    "original_bytes", "final_bytes", "saved_percent",
})


class ResultLabels(unittest.TestCase):
    """La tabla de etiquetas coincide con lo que produce el worker."""

    def test_every_field_the_worker_emits_has_a_label(self) -> None:
        """Un campo sin etiqueta sale en inglés, con la primera letra en mayúscula."""
        self.assertEqual(
            FIELDS_EMITTED_BY_THE_WORKER - set(formatting.RESULT_LABELS), set()
        )

    def test_no_label_is_for_a_field_nobody_emits(self) -> None:
        """Una etiqueta huérfana suele ser un nombre mal escrito del campo real."""
        self.assertEqual(
            set(formatting.RESULT_LABELS) - FIELDS_EMITTED_BY_THE_WORKER, set()
        )

    def test_an_unknown_field_is_still_shown(self) -> None:
        """Lo que el cliente no conoce se muestra con su nombre, no desaparece."""
        self.assertEqual(formatting.result_label("campo_nuevo"), "Campo nuevo")


class ResultValues(unittest.TestCase):
    """Cada valor se muestra con la forma que llega del worker, no otra."""

    def test_gps_arrives_as_a_pair(self) -> None:
        """Las coordenadas son una lista de dos números, no un objeto."""
        self.assertEqual(
            formatting.format_result_value("gps", [-32.889458, -68.845839]),
            "-32.889458, -68.845839",
        )

    def test_size_shows_width_by_height(self) -> None:
        self.assertEqual(formatting.format_result_value("size", [1280, 1024]), "1280 × 1024")

    def test_saved_percent_carries_its_sign(self) -> None:
        self.assertEqual(formatting.format_result_value("saved_percent", 63), "63 %")

    def test_byte_fields_get_a_unit(self) -> None:
        self.assertEqual(formatting.format_result_value("bytes", 320284), "312.8 KB")

    def test_taken_at_is_shown_as_the_camera_wrote_it(self) -> None:
        """EXIF escribe `AAAA:MM:DD HH:MM:SS`; no se convierte."""
        self.assertEqual(
            formatting.format_result_value("taken_at", "2024:03:15 14:32:07"),
            "2024:03:15 14:32:07",
        )

    def test_timestamps_are_shown_in_local_time(self) -> None:
        """La misma hora escrita en dos zonas se muestra igual: se convierte, no se copia."""
        self.assertEqual(
            formatting.format_timestamp("2026-09-12T13:57:42+00:00"),
            formatting.format_timestamp("2026-09-12T10:57:42-03:00"),
        )

    def test_privacy_fields_are_only_sensitive_when_present(self) -> None:
        self.assertTrue(formatting.is_privacy_sensitive("gps", [-32.9, -68.8]))
        self.assertFalse(formatting.is_privacy_sensitive("gps", []))
        self.assertFalse(formatting.is_privacy_sensitive("bytes", 320284))
