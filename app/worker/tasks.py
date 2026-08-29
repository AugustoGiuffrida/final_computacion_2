"""Las operaciones sobre imágenes: lo que ejecutan los workers.

Cada operación del catálogo del protocolo es una tarea de Celery. Por la cola viajan el
identificador del trabajo, la ruta del archivo y los parámetros —nunca la imagen—: los
workers ven el mismo volumen que el servidor.

Todas las tareas comparten la misma firma y la misma forma de resultado, porque el
monitor del servidor las trata igual sin importar cuál sea.

ESTADO: las cuatro operaciones que alcanzan con Pillow. `anonymize` y `sanitize`
necesitan detección de caras (OpenCV) y vienen después.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from app.common import config
from app.worker.celery_app import celery_app

# El tag GPSInfo del estándar EXIF: si está presente, la foto dice dónde se tomó.
GPS_TAG = 34853


def output_path_for(job_id: str, suffix: str) -> Path:
    """Crea la carpeta del resultado y arma la ruta del archivo de salida.

    Args:
        job_id: Identificador del trabajo; nombra su carpeta bajo `results/`.
        suffix: Extensión del archivo de salida, con el punto: '.jpg', '.webp'…

    Returns:
        La ruta donde la tarea debe guardar su resultado.
    """
    directory = config.RESULTS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)

    return directory / f"out{suffix}"


@celery_app.task
def inspect(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Audita qué revela la imagen, sin modificarla. Es la única sin archivo de salida.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original. Llega como texto porque por la cola
            viaja JSON, que no tiene rutas.
        parameters: No usa ninguno; está por la firma común.

    Returns:
        `{"result": {...}}` con formato, dimensiones, modo de color, cuántas entradas
        de metadatos tiene y si contiene coordenadas GPS.
    """
    with Image.open(input_path) as image:
        metadata = image.getexif()
        report = {
            "format": image.format,
            "size": list(image.size),
            "mode": image.mode,
            "metadata_entries": len(metadata),
            "has_gps": GPS_TAG in metadata,
        }

    return {"result": report}


@celery_app.task
def clean(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Elimina los metadatos re-guardando los píxeles.

    No hay que "borrar" nada: Pillow solo escribe metadatos si se le pasan, así que
    guardar la imagen a secas es exactamente quedarse con los píxeles.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: No usa ninguno; está por la firma común.

    Returns:
        `{"output_path": ..., "result": {"metadata_removed": True}}`, con la ruta del
        archivo limpio en el mismo formato que el original.
    """
    with Image.open(input_path) as image:
        destination = output_path_for(job_id, Path(input_path).suffix.lower())
        image.save(destination)

    return {"output_path": str(destination), "result": {"metadata_removed": True}}


@celery_app.task
def compress(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Recomprime como JPEG y, si se pidió, reduce el tamaño en píxeles.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: `quality` (1-95, por defecto 80) y `max_size` (lado máximo en
            píxeles; si la imagen lo supera, se reduce respetando la proporción).

    Returns:
        `{"output_path": ..., "result": {...}}` con los bytes originales, los finales y
        el porcentaje ahorrado.
    """
    quality = parameters.get("quality", 80)
    max_size = parameters.get("max_size")

    with Image.open(input_path) as image:
        image = image.convert("RGB")  # JPEG no admite canal alfa
        if max_size:
            image.thumbnail((max_size, max_size))  # achica respetando la proporción

        destination = output_path_for(job_id, ".jpg")
        image.save(destination, "JPEG", quality=quality)

    original_bytes = Path(input_path).stat().st_size
    final_bytes = destination.stat().st_size

    return {
        "output_path": str(destination),
        "result": {
            "original_bytes": original_bytes,
            "final_bytes": final_bytes,
            "saved_percent": round(100 * (1 - final_bytes / original_bytes)),
        },
    }


@celery_app.task
def convert(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Cambia el formato del archivo.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: `format` ('webp', 'jpeg' o 'png'; por defecto 'webp') y `quality`
            (1-95, por defecto 85; PNG la ignora porque su compresión no pierde).

    Returns:
        `{"output_path": ..., "result": {"format": ...}}` con la ruta del archivo en el
        formato nuevo.
    """
    target = parameters.get("format", "webp")
    quality = parameters.get("quality", 85)

    with Image.open(input_path) as image:
        if target == "jpeg":
            image = image.convert("RGB")  # JPEG no admite canal alfa

        suffix = ".jpg" if target == "jpeg" else f".{target}"
        destination = output_path_for(job_id, suffix)
        image.save(destination, target.upper(), quality=quality)

    return {"output_path": str(destination), "result": {"format": target}}
