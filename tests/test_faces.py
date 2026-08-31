"""Pruebas de la detección de caras y del cubrimiento.

Usan una imagen real con una cara, guardada en `tests/imagenes/`: un clasificador Haar
está entrenado con fotografías y no dispara con dibujos, así que no hay forma de
generarle una entrada válida desde el código.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.worker import faces

IMAGEN_CON_CARA = Path(__file__).parent / "imagenes" / "con_cara.jpg"


class FaceTestCase(unittest.TestCase):
    """Base con un directorio temporal y la imagen de prueba."""

    def setUp(self) -> None:
        """Crea el directorio temporal."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)
        self.addCleanup(self._temporary_directory.cleanup)

    def an_image_without_faces(self, name: str = "paisaje.jpg") -> Path:
        """Guarda una imagen de ruido, donde no hay ninguna cara que encontrar."""
        path = self.working_directory / name
        Image.effect_noise((400, 300), 60).convert("RGB").save(path, "JPEG")

        return path


# ──────────────────────── la detección ────────────────────────


class Detection(FaceTestCase):
    """Qué encuentra `detect` y qué no."""

    def test_a_real_face_is_found(self) -> None:
        """En una foto con una cara, devuelve un rectángulo."""
        found = faces.detect(IMAGEN_CON_CARA)

        self.assertEqual(len(found), 1)

    def test_the_rectangle_is_inside_the_image(self) -> None:
        """El rectángulo cae dentro de los límites de la imagen."""
        with Image.open(IMAGEN_CON_CARA) as image:
            width, height = image.size

        x, y, face_width, face_height = faces.detect(IMAGEN_CON_CARA)[0]

        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + face_width, width)
        self.assertLessEqual(y + face_height, height)

    def test_an_image_without_faces_returns_an_empty_list(self) -> None:
        """No encontrar nada es un resultado válido, no un error."""
        self.assertEqual(faces.detect(self.an_image_without_faces()), [])

    def test_a_file_that_is_not_an_image_returns_an_empty_list(self) -> None:
        """`cv2.imread` devuelve None ante lo que no puede leer; no debe reventar.

        El proceso de ingreso ya verificó la imagen antes de llegar acá, así que esto es
        una segunda línea de defensa.
        """
        basura = self.working_directory / "basura.jpg"
        basura.write_bytes(b"no soy una imagen")

        self.assertEqual(faces.detect(basura), [])


# ──────────────────────── el cubrimiento ────────────────────────


class Covering(FaceTestCase):
    """Los tres modos y cómo responden a la intensidad."""

    def setUp(self) -> None:
        """Carga la imagen y detecta su cara una sola vez."""
        super().setUp()
        self.image = Image.open(IMAGEN_CON_CARA).convert("RGB")
        self.faces = faces.detect(IMAGEN_CON_CARA)
        self.addCleanup(self.image.close)

    def difference_in_the_face(self, covered: Image.Image) -> float:
        """Cuánto cambió la zona de la cara, de 0 a 255.

        Args:
            covered: La imagen ya cubierta.

        Returns:
            La diferencia media por canal entre el original y el cubierto, dentro del
            rectángulo de la cara.
        """
        region = faces.with_margin(self.faces[0], self.image.width, self.image.height)
        before = list(self.image.crop(region).getdata())
        after = list(covered.crop(region).getdata())

        return sum(
            abs(a - b) for pixel_a, pixel_b in zip(before, after)
            for a, b in zip(pixel_a, pixel_b)
        ) / (len(before) * 3)

    def test_blur_changes_the_face(self) -> None:
        """Difuminar altera la zona de la cara de forma apreciable."""
        covered = faces.cover(self.image, self.faces, "blur", 15)

        self.assertGreater(self.difference_in_the_face(covered), 10)

    def test_pixelate_changes_the_face(self) -> None:
        """Pixelar también."""
        covered = faces.cover(self.image, self.faces, "pixelate", 15)

        self.assertGreater(self.difference_in_the_face(covered), 10)

    def test_box_leaves_the_face_black(self) -> None:
        """El modo caja tapa con negro sólido: todos los píxeles en (0, 0, 0)."""
        covered = faces.cover(self.image, self.faces, "box", 15)

        region = faces.with_margin(self.faces[0], self.image.width, self.image.height)
        self.assertEqual(set(covered.crop(region).getdata()), {(0, 0, 0)})

    def test_more_strength_hides_more(self) -> None:
        """La intensidad es monótona: más alto, menos parecido al original.

        Es lo que hace que `--strength` signifique algo para el usuario.
        """
        leve = self.difference_in_the_face(faces.cover(self.image, self.faces, "blur", 5))
        fuerte = self.difference_in_the_face(faces.cover(self.image, self.faces, "blur", 40))

        self.assertGreater(fuerte, leve)

    def test_the_rest_of_the_image_is_untouched(self) -> None:
        """Solo se toca la zona de las caras: el resto queda idéntico."""
        covered = faces.cover(self.image, self.faces, "box", 15)

        esquina = (0, 0, 40, 40)  # lejos de la cara, que está en el centro
        self.assertEqual(
            list(self.image.crop(esquina).getdata()),
            list(covered.crop(esquina).getdata()),
        )

    def test_without_faces_the_image_is_unchanged(self) -> None:
        """Sin caras que cubrir, la copia es igual al original."""
        covered = faces.cover(self.image, [], "blur", 15)

        self.assertEqual(list(self.image.getdata()), list(covered.getdata()))


# ──────────────────────── el margen ────────────────────────


class Margin(FaceTestCase):
    """El rectángulo se agranda, pero nunca se sale de la imagen."""

    def test_the_box_grows_around_the_face(self) -> None:
        """La caja resultante contiene a la original y es más grande."""
        left, top, right, bottom = faces.with_margin((100, 100, 50, 50), 500, 500)

        self.assertLess(left, 100)
        self.assertLess(top, 100)
        self.assertGreater(right, 150)
        self.assertGreater(bottom, 150)

    def test_it_does_not_go_past_the_edges(self) -> None:
        """Una cara pegada al borde no produce coordenadas negativas ni fuera de rango.

        Sin esto, `crop` sobre una caja que se sale devolvería una región con bordes
        vacíos y `paste` la pegaría corrida.
        """
        left, top, right, bottom = faces.with_margin((0, 0, 100, 100), 100, 100)

        self.assertEqual((left, top), (0, 0))
        self.assertEqual((right, bottom), (100, 100))


if __name__ == "__main__":
    unittest.main()
