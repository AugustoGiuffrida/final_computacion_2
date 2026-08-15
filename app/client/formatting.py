"""Cómo se ve un dato del protocolo cuando se lo muestra: colores, íconos y etiquetas.

Lo usan las dos interfaces —el modo directo y la interactiva— para que un trabajo `DONE`
se vea igual en las dos y no haya dos listas de colores que se puedan desincronizar.

Acá no hay lógica de red ni de negocio: entra un valor del protocolo y sale texto. Los
nombres de estilo son los de Rich, que Textual también entiende por estar construido
sobre él.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from app.common import messages

# Color e ícono de cada estado. El ícono existe para que el estado siga siendo legible en
# una terminal sin color, o para alguien que no distinga bien el verde del rojo.
STATUS_STYLES: Final[dict[str, tuple[str, str]]] = {
    messages.QUEUED: ("yellow", "◷"),
    messages.PROCESSING: ("cyan", "◐"),
    messages.DONE: ("green", "✓"),
    messages.FAILED: ("red", "✗"),
}

# Para un estado que el servidor agregue y este cliente todavía no conozca.
UNKNOWN_STATUS_STYLE: Final[tuple[str, str]] = ("dim", "·")

# Nombre en castellano de cada campo del 'result'. Lo que no esté acá se muestra con su
# nombre crudo, así un campo nuevo del servidor aparece igual en vez de desaparecer.
RESULT_LABELS: Final[dict[str, str]] = {
    "faces_detected": "Caras detectadas",
    "bytes": "Tamaño",
    "original_bytes": "Tamaño original",
    "final_bytes": "Tamaño final",
    "content_type": "Tipo de archivo",
    "gps": "Coordenadas GPS",
    "taken_at": "Fecha de captura",
    "camera": "Cámara",
    "serial_number": "Número de serie",
    "removed_metadata": "Metadatos eliminados",
    "source_format": "Formato de origen",
    "target_format": "Formato de destino",
    "stages": "Etapas aplicadas",
}

# Campos del informe de `inspect` que son, precisamente, la fuga de privacidad que la
# aplicación existe para mostrar. Se resaltan para que salten a la vista en la demo.
PRIVACY_SENSITIVE_FIELDS: Final[frozenset[str]] = frozenset({
    "gps", "taken_at", "camera", "serial_number",
})

# Campos que son cantidades de bytes y conviene mostrar en KB o MB.
BYTE_FIELDS: Final[frozenset[str]] = frozenset({"bytes", "original_bytes", "final_bytes"})


def status_style(status: str) -> str:
    """Devuelve el nombre del estilo de Rich con el que se muestra un estado."""
    return STATUS_STYLES.get(status, UNKNOWN_STATUS_STYLE)[0]


def status_icon(status: str) -> str:
    """Devuelve el carácter que acompaña a un estado."""
    return STATUS_STYLES.get(status, UNKNOWN_STATUS_STYLE)[1]


def status_markup(status: str) -> str:
    """Arma el estado listo para imprimir, por ejemplo '[green]✓ DONE[/green]'."""
    style, icon = STATUS_STYLES.get(status, UNKNOWN_STATUS_STYLE)
    return f"[{style}]{icon} {status}[/{style}]"


def format_size(size_in_bytes: int | float) -> str:
    """Expresa una cantidad de bytes en la unidad que la haga legible: '3.0 MB'."""
    size = float(size_in_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_timestamp(timestamp: str | None) -> str:
    """Acorta una marca ISO a 'dd/mm HH:MM:SS', para que entre en una tabla.

    Returns:
        La fecha acortada, un guion si no había marca, o el texto original si no se pudo
        interpretar.
    """
    if not timestamp:
        return "—"

    try:
        return datetime.fromisoformat(timestamp).strftime("%d/%m %H:%M:%S")
    except ValueError:
        return timestamp


def format_duration(started_at: str | None, finished_at: str | None) -> str:
    """Calcula cuánto tardó un trabajo, como '7 s' o '2 m 14 s'.

    Es más informativo que repetir la hora de fin: de un trabajo terminado lo que interesa
    es cuánto llevó, no a qué hora fue.

    Returns:
        La duración, o un guion si el trabajo no terminó o alguna marca no se entiende.
    """
    if not started_at or not finished_at:
        return "—"

    try:
        elapsed = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    except ValueError:
        return "—"

    total_seconds = int(elapsed.total_seconds())
    if total_seconds < 0:
        return "—"
    if total_seconds < 60:
        return f"{total_seconds} s"
    return f"{total_seconds // 60} m {total_seconds % 60} s"


def format_result_value(field: str, value: Any) -> str:
    """Convierte un valor del 'result' en texto: bytes con unidad, GPS como par, listas
    separadas por comas."""
    if value is None:
        return "—"

    if field in BYTE_FIELDS and isinstance(value, (int, float)):
        return format_size(value)

    if field == "taken_at" and isinstance(value, str):
        return format_timestamp(value)

    if field == "gps" and isinstance(value, dict):
        latitude, longitude = value.get("lat"), value.get("lon")
        if latitude is None or longitude is None:
            return "—"
        return f"{latitude}, {longitude}"

    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "—"

    if isinstance(value, bool):
        return "sí" if value else "no"

    return str(value)


def result_label(field: str) -> str:
    """Devuelve el nombre en castellano de un campo del resultado.

    Returns:
        Su etiqueta traducida, o el nombre con los guiones bajos convertidos en espacios
        si es un campo que este cliente no conoce.
    """
    if field in RESULT_LABELS:
        return RESULT_LABELS[field]
    return field.replace("_", " ").capitalize()


def is_privacy_sensitive(field: str, value: Any) -> bool:
    """Indica si un campo del resultado revela información privada de la foto.

    Son los datos que la aplicación existe para hacer visibles y después eliminar, así que
    se resaltan. Un campo vacío no revela nada.
    """
    return field in PRIVACY_SENSITIVE_FIELDS and value not in (None, "", [], {})
