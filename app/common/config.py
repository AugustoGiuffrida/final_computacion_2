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

DEFAULT_HOST: Final[str] = "localhost" #Servidor al que se conecta el cliente si no se indica otro.

DEFAULT_PORT: Final[int] = 9000 #Puerto alto a propósito: los menores a 1024 requieren privilegios de root.

# None le dice a asyncio.start_server que escuche en todas las interfaces disponibles,
# tanto IPv4 como IPv6: es el socket dual-stack.
LISTEN_ON_ALL_INTERFACES: Final[None] = None

# ────────────────────────── almacenamiento ──────────────────────────

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2] #Raíz del repositorio.

STORAGE_DIR: Final[Path] = PROJECT_ROOT / "storage" #Directorio raíz de los archivos.

UPLOADS_DIR: Final[Path] = STORAGE_DIR / "uploads" #Originales, en un subdirectorio por trabajo.

RESULTS_DIR: Final[Path] = STORAGE_DIR / "results" #Procesadas, en un subdirectorio por trabajo.

# ─────────────────────────── imágenes ───────────────────────────

# Validación de conveniencia del cliente, para fallar temprano y sin molestar al
# servidor. Que el archivo sea realmente una imagen lo verifica el servidor.
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".png"})

# Regla de la aplicación. Se mantiene por debajo de protocol.MAX_PAYLOAD_SIZE, que es el
# techo defensivo del framing.
DEFAULT_MAX_IMAGE_SIZE: Final[int] = 25 * 1024 * 1024

# ─────────────────────────── operaciones ───────────────────────────

# Única fuente de verdad sobre qué se puede pedir: el cliente arma con esto las opciones
# de su línea de comandos y el servidor valida contra lo mismo.
OPERATION_PARAMETERS: Final[dict[str, tuple[str, ...]]] = {
    "inspect": (),
    "anonymize": ("mode", "strength"),
    "clean": (),
    "convert": ("format", "quality"),
    "compress": ("quality", "max_size"),
    "sanitize": ("mode", "strength", "quality", "max_size"),
}

# Todos los parámetros que existen, sin repetir. Se deriva del diccionario de arriba para
# que agregar uno no obligue a acordarse de tocar una segunda lista.
ALL_OPERATION_PARAMETERS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        parameter
        for parameters in OPERATION_PARAMETERS.values()
        for parameter in parameters
    )
)

ANONYMIZE_MODES: Final[tuple[str, ...]] = ("blur", "pixelate", "box") #Formas de cubrir una cara.

CONVERT_FORMATS: Final[tuple[str, ...]] = ("webp", "jpeg", "png") #Formatos de salida de convert.

# ────────────────────────── espera del cliente ──────────────────────────

STATUS_POLL_INTERVAL_SECONDS: Final[float] = 1.0 #Cada cuánto reconsulta el cliente con --wait.

# Cuánto espera el cliente antes de rendirse. Rendirse no cancela nada: el trabajo sigue
# su curso y el resultado queda disponible.
DEFAULT_WAIT_TIMEOUT_SECONDS: Final[int] = 300
