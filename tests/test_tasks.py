"""Pruebas de cómo el worker decide dónde escribir.

La regla es que no lo decide: lo deriva de la ruta del original, que eligió el servidor.
Cuando lo decidía por su cuenta —leyendo la configuración— bastaba con arrancar el
servidor con `--storage-dir` para que el original y el resultado quedaran en dos árboles
distintos, sin que nada fallara.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.worker import tasks


class ResultsDirectory(unittest.TestCase):
    """De dónde sale la carpeta de resultados de un trabajo."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def upload_in(self, root: Path, job_id: str = "abc") -> Path:
        """Arma la ruta que tendría un original recibido bajo esa raíz.

        Args:
            root: Raíz del almacenamiento.
            job_id: Identificador del trabajo.

        Returns:
            La ruta del original, como la arma el servidor.
        """
        return root / "uploads" / job_id / "foto.jpg"

    def test_results_are_a_sibling_of_uploads(self) -> None:
        directory = tasks.results_directory_for(self.upload_in(self.root), "abc")

        self.assertEqual(directory, self.root / "results" / "abc")
        self.assertTrue(directory.is_dir())

    def test_follows_the_root_the_server_chose(self) -> None:
        """El bug que motivó el cambio: dos raíces distintas daban el mismo destino."""
        other_root = Path(tempfile.mkdtemp())

        first = tasks.results_directory_for(self.upload_in(self.root), "abc")
        second = tasks.results_directory_for(self.upload_in(other_root), "abc")

        self.assertNotEqual(first, second)
        self.assertEqual(second, other_root / "results" / "abc")

    def test_an_intermediate_coincides_only_by_layout(self) -> None:
        """Por qué la cadena pasa el original y no `state["path"]`.

        Derivar del intermedio da hoy el mismo directorio, porque `results/<job>/archivo`
        tiene la misma profundidad que `uploads/<job>/archivo`. Es una coincidencia de
        cómo están dispuestas las carpetas, no algo garantizado: pasar `original_path` no
        depende de que siga siendo cierta.
        """
        intermediate = self.root / "results" / "abc" / "paso1_limpia.jpg"

        self.assertEqual(
            tasks.results_directory_for(intermediate, "abc"),
            tasks.results_directory_for(self.upload_in(self.root), "abc"),
        )

    def test_the_final_file_is_named_out(self) -> None:
        """El nombre del resultado final se decide en un solo lugar."""
        upload = self.upload_in(self.root)

        self.assertEqual(
            tasks.output_path_for(upload, "abc", ".webp"),
            self.root / "results" / "abc" / "out.webp",
        )


class PrivacyReport(unittest.TestCase):
    """Qué saca la auditoría de los metadatos de una imagen.

    Es el punto de `inspect`: los datos ya se leían al abrir la imagen, pero solo se
    informaba cuántos había. El cliente tenía las etiquetas y el resaltado listos desde
    antes y nunca recibía estos campos.
    """

    def metadata_with(self, **tags):
        """Arma unos metadatos EXIF con los tags indicados.

        Args:
            tags: Números de tag EXIF y sus valores.

        Returns:
            Los metadatos, como los devolvería `Image.getexif()`.
        """
        exif = Image.Exif()
        for tag, value in tags.items():
            exif[int(tag)] = value

        return exif

    def test_an_image_without_metadata_reports_nothing(self) -> None:
        """Sin datos no se inventan campos vacíos: directamente no aparecen."""
        self.assertEqual(tasks.privacy_report(self.metadata_with()), {})

    def test_the_camera_joins_make_and_model(self) -> None:
        report = tasks.privacy_report(
            self.metadata_with(**{"271": "Apple", "272": "iPhone 13"})
        )

        self.assertEqual(report["camera"], "Apple iPhone 13")

    def test_only_the_make_is_enough(self) -> None:
        report = tasks.privacy_report(self.metadata_with(**{"271": "Canon"}))

        self.assertEqual(report["camera"], "Canon")

    def test_the_original_date_wins_over_the_file_date(self) -> None:
        """`DateTime` es cuándo se guardó el archivo; `DateTimeOriginal`, cuándo se sacó."""
        report = tasks.privacy_report(
            self.metadata_with(**{"306": "2020:01:01 00:00:00",
                                  "36867": "2024:03:15 14:32:07"})
        )

        self.assertEqual(report["taken_at"], "2024:03:15 14:32:07")


class Coordinates(unittest.TestCase):
    """La conversión de coordenadas EXIF a grados decimales."""

    def test_the_southern_hemisphere_is_negative(self) -> None:
        grados = tasks.degrees_from(
            (IFDRational(32, 1), IFDRational(53, 1), IFDRational(220488, 10000)), "S"
        )

        self.assertAlmostEqual(grados, -32.889458, places=5)

    def test_the_northern_hemisphere_is_positive(self) -> None:
        grados = tasks.degrees_from(
            (IFDRational(40, 1), IFDRational(0, 1), IFDRational(0, 1)), "N"
        )

        self.assertEqual(grados, 40.0)

    def test_west_is_negative(self) -> None:
        grados = tasks.degrees_from(
            (IFDRational(68, 1), IFDRational(0, 1), IFDRational(0, 1)), "W"
        )

        self.assertEqual(grados, -68.0)


if __name__ == "__main__":
    unittest.main()
