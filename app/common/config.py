"""Valores por defecto y límites de la aplicación, en un solo lugar.

Todo lo que sea configurable o pueda cambiar de valor vive acá, para que no haya números
ni rutas repartidos por el código. Los valores que se pueden ajustar por línea de comandos
usan estas constantes como valor inicial.

No confundir con los límites de `protocol.py`: aquellos protegen el framing de valores
disparatados, estos expresan reglas de la aplicación.
"""

from __future__ import annotations

from pathlib import Path

# ─────────────────────────────── red ───────────────────────────────

# Servidor al que se conecta el cliente si no se indica otro.
DEFAULT_HOST = "localhost"

# Puerto alto a propósito: los menores a 1024 requieren privilegios de root.
DEFAULT_PORT = 9000

# None le dice a asyncio.start_server que escuche en todas las interfaces disponibles.
# Abre un socket por familia: uno AF_INET y otro AF_INET6, ambos en el mismo puerto.
LISTEN_ON_ALL_INTERFACES = None

# ────────────────────────── almacenamiento ──────────────────────────

# Raíz del repositorio.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Directorio raíz de los archivos.
STORAGE_DIR = PROJECT_ROOT / "storage"

# Originales, en un subdirectorio por trabajo.
UPLOADS_DIR = STORAGE_DIR / "uploads"

# Procesadas, en un subdirectorio por trabajo.
RESULTS_DIR = STORAGE_DIR / "results"

# La base va debajo del directorio de almacenamiento, así apuntar `--storage-dir` a otro
# lado —que es lo que hacen las pruebas— se lleva la base con él.
DATABASE_NAME = "jobs.db"

# ─────────────────────────── imágenes ───────────────────────────

# Validación de conveniencia del cliente, para fallar temprano y sin molestar al
# servidor. Que el archivo sea realmente una imagen lo verifica el servidor.
SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})

# Lo que el contenido tiene que ser, según Pillow al abrirlo. Distinto de
# SUPPORTED_EXTENSIONS, que mira el nombre: un archivo puede llamarse 'foto.jpg' y ser un
# PNG, un GIF o un ejecutable.
SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG"})

# Regla de la aplicación. Se mantiene por debajo de protocol.MAX_PAYLOAD_SIZE, que es el
# techo defensivo del framing.
DEFAULT_MAX_IMAGE_SIZE = 25 * 1024 * 1024

# ─────────────────────────── operaciones ───────────────────────────

# Única fuente de verdad sobre qué se puede pedir: el cliente arma con esto las opciones
# de su línea de comandos y el servidor valida contra lo mismo.
OPERATION_PARAMETERS = {
    "inspect": (),
    "anonymize": ("mode", "strength"),
    "clean": (),
    "convert": ("format", "quality"),
    "compress": ("quality", "max_size"),
    "sanitize": ("mode", "strength", "quality", "max_size"),
}

# Todos los parámetros que existen, sin repetir. Se deriva del diccionario de arriba para
# que agregar uno no obligue a acordarse de tocar una segunda lista.
ALL_OPERATION_PARAMETERS = tuple(
    dict.fromkeys(
        parameter
        for parameters in OPERATION_PARAMETERS.values()
        for parameter in parameters
    )
)

# Formas de cubrir una cara.
ANONYMIZE_MODES = ("blur", "pixelate", "box")

# Formatos de salida de convert.
CONVERT_FORMATS = ("webp", "jpeg", "png")

# ─────────────────────────── cola de tareas ───────────────────────────

# Redis local, en el contenedor redis-final (puerto 6380: el 6379 lo ocupa otro
# servicio de esta máquina). Broker y backend son bases distintas del mismo Redis:
# la 0 lleva los mensajes "hay trabajo", la 1 los resultados de cada tarea.
CELERY_BROKER_URL = "redis://localhost:6380/0"
CELERY_RESULT_BACKEND = "redis://localhost:6380/1"

# ─────────────────────────── historial ───────────────────────────

# Cuántos trabajos devuelve si el cliente no pide una cantidad.
DEFAULT_HISTORY_LIMIT = 10

# Tope que impone el servidor, elija lo que elija el cliente. Un pedido más grande no se
# rechaza: se recorta a este valor.
MAX_HISTORY_LIMIT = 100

# ────────────────────────── espera del cliente ──────────────────────────

# Cada cuánto reconsulta el cliente con --wait.
STATUS_POLL_INTERVAL_SECONDS = 1.0

# Cuánto espera el cliente antes de rendirse. Rendirse no cancela nada: el trabajo sigue
# su curso y el resultado queda disponible.
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
