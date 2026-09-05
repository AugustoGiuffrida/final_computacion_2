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

    def test_files_land_inside_that_directory(self) -> None:
        upload = self.upload_in(self.root)

        self.assertEqual(
            tasks.stage_path(upload, "abc", "paso1_limpia.jpg"),
            self.root / "results" / "abc" / "paso1_limpia.jpg",
        )
        self.assertEqual(
            tasks.output_path_for(upload, "abc", ".webp"),
            self.root / "results" / "abc" / "out.webp",
        )


if __name__ == "__main__":
    unittest.main()
