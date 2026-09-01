"""Detección de caras y cómo cubrirlas.

Es lo único del proyecto que usa OpenCV. Se detecta con un **clasificador en cascada
Haar**: ventanas deslizantes sobre la imagen en escala de grises, características de
contraste, y una cascada de etapas donde cada una descarta rápido lo que no es cara.

Se fija OpenCV 4 y no 5 porque la 5 quitó `CascadeClassifier` y sus cascadas del paquete:
solo deja un detector basado en redes que exige bajar un modelo aparte. La cascada viene
incluida, no agrega archivos al repositorio y es explicable.
"""

from __future__ import annotations

from pathlib import Path

import cv2
from PIL import Image, ImageFilter

# La cascada entrenada para caras de frente, incluida en el paquete de OpenCV.
CASCADE_FILE = "haarcascade_frontalface_default.xml"

# Cuánto se agranda cada rectángulo detectado, como fracción de su lado. La cascada
# devuelve una caja ajustada a los rasgos; sin margen quedan a la vista el mentón y las
# orejas, que también identifican.
FACE_MARGIN = 0.12

# Cuánto se achica la imagen para detectar. Buscar caras crece con el área, y en una foto
# de 4000px de ancho no hace falta tanto detalle: se detecta sobre una copia chica y los
# rectángulos se escalan de vuelta.
DETECTION_WIDTH = 800

_detector: cv2.CascadeClassifier | None = None


def detector() -> cv2.CascadeClassifier:
    """Carga la cascada una sola vez por proceso.

    El XML son casi 900 KB y leerlo cuesta decenas de milisegundos; hacerlo por imagen
    sería pagarlo de más. Se carga al primer uso y no al importar, para que el proceso que
    solo importa el módulo no pague nada.

    Returns:
        El clasificador, listo para usar.

    Raises:
        RuntimeError: Si el archivo de la cascada no se pudo cargar.
    """
    global _detector

    if _detector is None:
        _detector = cv2.CascadeClassifier(cv2.data.haarcascades + CASCADE_FILE)
        if _detector.empty():
            raise RuntimeError(f"no se pudo cargar la cascada {CASCADE_FILE}")

    return _detector


def detect(path: Path) -> list[tuple[int, int, int, int]]:
    """Busca caras de frente en la imagen.

    Args:
        path: Ruta de la imagen a analizar.

    Returns:
        Un rectángulo por cara, como `(x, y, ancho, alto)` en píxeles de la imagen
        original. Lista vacía si no hay ninguna, que es un resultado válido y no un error.
    """
    image = cv2.imread(str(path))
    if image is None:
        return []

    height, width = image.shape[:2]
    scale = min(1.0, DETECTION_WIDTH / width)

    small = cv2.resize(image, (round(width * scale), round(height * scale)))
    grayscale = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    found = detector().detectMultiScale(
        grayscale,
        scaleFactor=1.1,   # cuánto crece la ventana de búsqueda en cada pasada
        # Cuántas detecciones vecinas se exigen para aceptar una cara. El 7 está medido:
        # con 5 aparecían falsos positivos —recuadros de 80px en esquinas vacías— al
        # detectar sobre imágenes reescaladas, y con 4 el detector "veía" una cara en un
        # paisaje. Subirlo lo vuelve más exigente: el precio sería perder caras difíciles
        # (de perfil, muy chicas), que esta cascada frontal tampoco detecta bien.
        minNeighbors=7,
        minSize=(30, 30),  # nada más chico que esto es ruido
    )

    # Los rectángulos vuelven a la escala de la imagen original.
    return [
        (round(x / scale), round(y / scale), round(w / scale), round(h / scale))
        for x, y, w, h in found
    ]


def cover(
    image: Image.Image,
    faces: list[tuple[int, int, int, int]],
    mode: str,
    strength: int,
) -> Image.Image:
    """Cubre cada cara con el modo pedido.

    La intensidad se aplica **relativa al tamaño de cada cara**, no en píxeles absolutos:
    así el resultado se ve igual en una foto de celular y en una de cámara.

    Args:
        image: La imagen a modificar; se trabaja sobre una copia.
        faces: Los rectángulos devueltos por `detect`.
        mode: 'blur' (difumina), 'pixelate' (cuadricula) o 'box' (tapa con negro).
        strength: Intensidad, de 1 a 100. Más alto, menos reconocible.

    Returns:
        Una imagen nueva con las caras cubiertas. Si `faces` está vacía, una copia igual.
    """
    covered = image.copy()

    for face in faces:
        region = with_margin(face, covered.width, covered.height)
        left, top, right, bottom = region
        side = right - left

        if mode == "box":
            covered.paste((0, 0, 0), region)
            continue

        patch = covered.crop(region)

        if mode == "blur":
            # El radio crece con la cara: 15 sobre una de 200px son 30px de radio.
            radius = max(2, round(side * strength / 100))
            patch = patch.filter(ImageFilter.GaussianBlur(radius))
        else:  # pixelate
            # Se achica a pocos píxeles y se agranda sin interpolar: quedan bloques.
            blocks = max(2, round(100 / strength))
            patch = patch.resize((blocks, blocks), Image.NEAREST).resize(
                patch.size, Image.NEAREST
            )

        covered.paste(patch, region)

    return covered


def with_margin(
    face: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    """Agranda un rectángulo por FACE_MARGIN, sin salirse de la imagen.

    Args:
        face: El rectángulo `(x, y, ancho, alto)` que devolvió la cascada.
        width: Ancho de la imagen, para no pasarse del borde.
        height: Alto de la imagen.

    Returns:
        La caja como `(izquierda, arriba, derecha, abajo)`, que es lo que espera Pillow.
    """
    x, y, face_width, face_height = face
    grow_x = round(face_width * FACE_MARGIN)
    grow_y = round(face_height * FACE_MARGIN)

    return (
        max(0, x - grow_x),
        max(0, y - grow_y),
        min(width, x + face_width + grow_x),
        min(height, y + face_height + grow_y),
    )
