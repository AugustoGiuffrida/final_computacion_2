"""Las operaciones sobre imágenes: lo que ejecutan los workers.

Cada operación del catálogo del protocolo es una tarea de Celery. Por la cola viajan el
identificador del trabajo, la ruta del archivo y los parámetros —nunca la imagen—: los
workers ven el mismo volumen que el servidor.

Todas las tareas comparten la misma firma y la misma forma de resultado, porque el
monitor del servidor las trata igual sin importar cuál sea.

El trabajo de cada operación vive en una función suelta —`cover_faces_in`,
`strip_metadata_in`, `shrink_in`— y las tareas son envoltorios. Así la misma lógica sirve
invocada sola y encadenada dentro de `sanitize`, sin duplicarse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from app.common import config
from app.worker import faces
from app.worker.celery_app import celery_app

# El tag GPSInfo del estándar EXIF: si está presente, la foto dice dónde se tomó.
GPS_TAG = 34853


def stage_path(job_id: str, name: str) -> Path:
    """Arma la ruta de un archivo intermedio de la cadena de saneamiento.

    Args:
        job_id: Identificador del trabajo; nombra su carpeta bajo `results/`.
        name: Nombre del archivo intermedio.

    Returns:
        La ruta, con su carpeta ya creada.
    """
    directory = config.RESULTS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)

    return directory / name


def cover_faces_in(
    source: Path, destination: Path, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Cubre las caras de una imagen y guarda el resultado.

    Args:
        source: Imagen a procesar.
        destination: Dónde escribir el resultado.
        parameters: `mode` y `strength`.

    Returns:
        Cuántas caras encontró y con qué modo las cubrió.
    """
    mode = parameters.get("mode", "blur")
    strength = parameters.get("strength", config.DEFAULT_ANONYMIZE_STRENGTH)
    detected = faces.detect(source)

    with Image.open(source) as image:
        faces.cover(image.convert("RGB"), detected, mode, strength).save(destination)

    return {"faces_detected": len(detected), "mode": mode}


def strip_metadata_in(source: Path, destination: Path) -> dict[str, Any]:
    """Guarda solo los píxeles, sin los metadatos.

    No hay que "borrar" nada: Pillow escribe metadatos únicamente si se le pasan, así que
    guardar la imagen a secas es exactamente quedarse con los píxeles.

    En JPEG se guarda con `quality="keep"`, que reutiliza las tablas de compresión del
    archivo original en vez de recomprimir. Sin eso, Pillow usa su calidad por defecto y
    degrada la imagen: dentro de la cadena de saneamiento eso significaba perder calidad
    dos veces, acá y en la compresión final. Y la degradación no era inocua — introducía
    artefactos que le hacían ver al detector una cara donde no había.

    Args:
        source: Imagen a limpiar.
        destination: Dónde escribir el resultado.

    Returns:
        Cuántas entradas de metadatos tenía la original.
    """
    with Image.open(source) as image:
        removed = len(image.getexif())
        # `quality="keep"` solo existe para JPEG; en PNG la compresión no pierde nada.
        options = {"quality": "keep"} if image.format == "JPEG" else {}
        image.save(destination, **options)

    return {"metadata_removed": removed}


def shrink_in(
    source: Path,
    destination: Path,
    parameters: dict[str, Any],
    measure_against: Path | None = None,
) -> dict[str, Any]:
    """Recomprime como JPEG y, si se pidió, reduce el tamaño en píxeles.

    Args:
        source: Imagen a comprimir.
        destination: Dónde escribir el resultado.
        parameters: `quality` (1-95, por defecto 80) y `max_size` (lado máximo).
        measure_against: Contra qué archivo informar el ahorro. Dentro de una cadena,
            `source` es un intermedio y al usuario le importa cuánto se achicó el suyo.

    Returns:
        Los bytes originales, los finales y el porcentaje ahorrado.
    """
    quality = parameters.get("quality", 80)
    max_size = parameters.get("max_size")

    with Image.open(source) as image:
        image = image.convert("RGB")  # JPEG no admite canal alfa
        if max_size:
            image.thumbnail((max_size, max_size))  # achica respetando la proporción

        image.save(destination, "JPEG", quality=quality)

    original_bytes = (measure_against or source).stat().st_size
    final_bytes = destination.stat().st_size

    return {
        "original_bytes": original_bytes,
        "final_bytes": final_bytes,
        "saved_percent": round(100 * (1 - final_bytes / original_bytes)),
    }


def output_path_for(job_id: str, suffix: str) -> Path:
    """La ruta del resultado final de un trabajo: `out` más su extensión.

    Args:
        job_id: Identificador del trabajo.
        suffix: Extensión del archivo, con el punto: '.jpg', '.webp'…
    """
    return stage_path(job_id, f"out{suffix}")


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
def anonymize(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Cubre las caras que encuentre en la imagen.

    Que no encuentre ninguna no es un error: la imagen se guarda igual y el informe lo
    dice. Distinguirlo importa —"no había caras" y "no las detecté" se ven iguales desde
    afuera— y por eso el resultado incluye la cantidad.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: `mode` ('blur', 'pixelate' o 'box'; por defecto 'blur') y `strength`
            (1-100; por defecto 15).

    Returns:
        `{"output_path": ..., "result": {"faces_detected": N, "mode": ...}}`.
    """
    source = Path(input_path)
    destination = output_path_for(job_id, source.suffix.lower())

    return {
        "output_path": str(destination),
        "result": cover_faces_in(source, destination, parameters),
    }


@celery_app.task
def clean(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Elimina los metadatos de la imagen.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: No usa ninguno; está por la firma común.

    Returns:
        `{"output_path": ..., "result": {"metadata_removed": N}}`.
    """
    source = Path(input_path)
    destination = output_path_for(job_id, source.suffix.lower())

    return {
        "output_path": str(destination),
        "result": strip_metadata_in(source, destination),
    }


@celery_app.task
def compress(job_id: str, input_path: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Recomprime la imagen y, si se pidió, reduce su tamaño en píxeles.

    Args:
        job_id: Identificador del trabajo.
        input_path: Ruta de la imagen original.
        parameters: `quality` (1-95, por defecto 80) y `max_size` (lado máximo en
            píxeles; si la imagen lo supera, se reduce respetando la proporción).

    Returns:
        `{"output_path": ..., "result": {...}}` con los bytes originales, los finales y
        el porcentaje ahorrado.
    """
    source = Path(input_path)
    destination = output_path_for(job_id, ".jpg")

    return {
        "output_path": str(destination),
        "result": shrink_in(source, destination, parameters),
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


# ──────────────────────── la cadena de saneamiento ────────────────────────
#
# `sanitize` no es una tarea sino tres encadenadas con `chain`: cada etapa recibe el
# estado que devolvió la anterior, lo modifica y lo pasa. Por el estado viajan dos rutas:
# `path`, la imagen en su forma actual, y `original_path`, que no cambia — las etapas
# trabajan sobre la primera y miden contra la segunda, porque al usuario le importa
# cuánto se achicó SU archivo, no el intermedio que produjo la etapa anterior.
#
# El orden es limpiar → cubrir → comprimir, y no al revés: la etapa que lee el archivo
# original tiene que ser la que informe cuántos metadatos había. Además así los
# intermedios nunca llevan las coordenadas GPS de la foto.


@celery_app.task
def sanitize_strip(state: dict[str, Any]) -> dict[str, Any]:
    """Primera etapa: elimina los metadatos.

    Va primera porque es la única que puede contar cuántos había: cualquier etapa que
    guarde la imagen antes se los lleva puestos sin decirlo.

    Args:
        state: `job_id`, `path` y `original_path` de la imagen original, `parameters` y
            `result` vacío.

    Returns:
        El estado con `path` apuntando al intermedio y el conteo de metadatos agregado.
    """
    destination = stage_path(state["job_id"], "paso1_limpia.jpg")
    state["result"] |= strip_metadata_in(Path(state["path"]), destination)
    state["path"] = str(destination)

    return state


@celery_app.task
def sanitize_cover(state: dict[str, Any]) -> dict[str, Any]:
    """Segunda etapa: cubre las caras.

    Args:
        state: Lo que devolvió `sanitize_strip`.

    Returns:
        El estado con `path` apuntando al intermedio nuevo y el informe de caras.
    """
    destination = stage_path(state["job_id"], "paso2_caras.jpg")
    state["result"] |= cover_faces_in(
        Path(state["path"]), destination, state["parameters"]
    )
    state["path"] = str(destination)

    return state


@celery_app.task
def sanitize_shrink(state: dict[str, Any]) -> dict[str, Any]:
    """Última etapa: recomprime, borra los intermedios y arma la respuesta.

    Args:
        state: Lo que devolvió `sanitize_strip`.

    Returns:
        `{"output_path": ..., "result": {...}}`, la misma forma que devuelven las
        operaciones sueltas: el monitor no distingue de dónde vino.
    """
    destination = output_path_for(state["job_id"], ".jpg")
    state["result"] |= shrink_in(
        Path(state["path"]),
        destination,
        state["parameters"],
        measure_against=Path(state["original_path"]),
    )

    # Los pasos intermedios ya no sirven y ocupan el volumen compartido.
    for intermediate in destination.parent.glob("paso*"):
        intermediate.unlink()

    return {"output_path": str(destination), "result": state["result"]}
