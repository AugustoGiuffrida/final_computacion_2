"""Valores por defecto y límites de la aplicación, en un solo lugar.

Todo lo que sea configurable o pueda cambiar de valor vive acá, para que no haya números
ni rutas repartidos por el código. Los valores que se pueden ajustar por línea de comandos
usan estas constantes como valor inicial.

No confundir con los límites de `protocol.py`: aquellos protegen el framing de valores
disparatados, estos expresan reglas de la aplicación.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# ─────────────────────────────── red ───────────────────────────────

DEFAULT_HOST: Final[str] = "localhost"
"""Dirección a la que se conecta el cliente si no se indica otra."""

DEFAULT_PORT: Final[int] = 9000
"""Puerto del servidor.

Se eligió un puerto alto a propósito: los menores a 1024 están reservados para
servicios estándar y en Linux requieren privilegios de root.
"""

LISTEN_ON_ALL_INTERFACES: Final[None] = None
"""Dirección de escucha del servidor.

`None` le indica a `asyncio.start_server` que escuche en todas las interfaces
disponibles, tanto IPv4 como IPv6 (socket dual-stack).
"""

# ────────────────────────── almacenamiento ──────────────────────────

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""Raíz del repositorio, calculada desde la ubicación de este archivo."""

STORAGE_DIR: Final[Path] = PROJECT_ROOT / "storage"
"""Directorio raíz de los archivos del sistema."""

UPLOADS_DIR: Final[Path] = STORAGE_DIR / "uploads"
"""Imágenes originales, en un subdirectorio por trabajo."""

RESULTS_DIR: Final[Path] = STORAGE_DIR / "results"
"""Imágenes procesadas, en un subdirectorio por trabajo."""

# ─────────────────────────── imágenes ───────────────────────────

SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".png"})
"""Extensiones que el cliente acepta enviar.

Es una validación de conveniencia para fallar temprano y sin molestar al servidor.
Que el archivo sea realmente una imagen se verifica del lado del servidor.
"""

DEFAULT_MAX_IMAGE_SIZE: Final[int] = 25 * 1024 * 1024
"""Peso máximo de una imagen aceptada, en bytes.

Es una regla de la aplicación y debe mantenerse por debajo de
`protocol.MAX_PAYLOAD_SIZE`, que es el techo defensivo del framing.
"""

# ─────────────────────────── operaciones ───────────────────────────

OPERATION_PARAMETERS: Final[dict[str, tuple[str, ...]]] = {
    "inspect": (),
    "anonymize": ("mode", "strength"),
    "clean": (),
    "convert": ("format", "quality"),
    "compress": ("quality", "max_size"),
    "sanitize": ("mode", "strength", "quality", "max_size"),
}
"""Operaciones disponibles y los parámetros que acepta cada una.

Es la única fuente de verdad sobre qué se puede pedir: el cliente arma con esto las
opciones de su línea de comandos y el servidor valida contra lo mismo.
"""

ANONYMIZE_MODES: Final[tuple[str, ...]] = ("blur", "pixelate", "box")
"""Formas de cubrir una cara detectada."""

CONVERT_FORMATS: Final[tuple[str, ...]] = ("webp", "jpeg", "png")
"""Formatos de salida admitidos por la operación de conversión."""

# ────────────────────────── espera del cliente ──────────────────────────

STATUS_POLL_INTERVAL_SECONDS: Final[float] = 1.0
"""Cada cuánto vuelve a consultar el estado el cliente en modo `--wait`."""

DEFAULT_WAIT_TIMEOUT_SECONDS: Final[int] = 300
"""Cuánto espera el cliente en modo `--wait` antes de rendirse.

Rendirse no cancela nada: el trabajo sigue su curso en el servidor y el resultado
queda disponible para consultarlo después con su identificador.
"""
