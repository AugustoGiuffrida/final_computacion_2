"""Lo que viaja por las colas entre el proceso principal y el proceso de ingreso.

Es lo único que los dos procesos conocen en común, y por eso está solo. Un vistazo a este
archivo alcanza para saber todo lo que pueden decirse: dos estructuras y cinco constantes.

Acá no hay comportamiento a propósito. Todo lo que se pone en una `multiprocessing.Queue`
se serializa con `pickle` para cruzar al otro proceso, así que solo viajan datos planos:
cadenas, números, diccionarios y rutas. Nunca un socket, un `StreamWriter` ni una corrutina.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ────────────────────────── veredictos de una revisión ──────────────────────────

# Imagen válida y que este usuario no procesó antes: el trabajo sigue adelante.
NEW = "new"

# Este usuario ya procesó este mismo contenido con esta misma operación.
DUPLICATE = "duplicate"

# No es una imagen, o está corrupta. Es un problema del pedido.
INVALID = "invalid"

# El ingreso no pudo decidir. Es un problema nuestro, y por eso está separado de INVALID:
# responder "tu imagen es inválida" cuando la falla fue del servidor sería mentirle al
# cliente sobre de quién es la culpa.
UNAVAILABLE = "unavailable"

# El proceso principal lo pone en la cola de pedidos para que el hijo termine su bucle y
# salga por las suyas, en vez de tener que matarlo.
SHUTDOWN = "shutdown"


# ────────────────────────── lo que viaja por las colas ──────────────────────────


@dataclass
class ReviewRequest:
    """Pedido de revisión de una imagen recién recibida.

    Attributes:
        job_id: Identificador del trabajo, ya generado por el proceso principal.
        user: Usuario dueño del trabajo. Hace falta acá porque la búsqueda de duplicados
            filtra por usuario: reutilizar el trabajo de otro le revelaría que procesó esa
            misma imagen.
        operation: Operación pedida.
        parameters: Parámetros de esa operación.
        stored_path: Ruta del archivo ya escrito en el volumen compartido. Viaja la ruta y
            no los bytes: los dos procesos ven el mismo sistema de archivos, y copiar la
            imagen entera por un pipe sería trabajo puro sin ninguna ganancia.
    """

    job_id: str
    user: str
    operation: str
    parameters: dict[str, Any]
    stored_path: Path


@dataclass
class ReviewResponse:
    """Veredicto del ingreso sobre una imagen.

    Attributes:
        job_id: El mismo del pedido. Es lo que permite saber a qué pedido corresponde esta
            respuesta: todas vuelven por una única cola y sin un orden garantizado.
        verdict: NEW, DUPLICATE, INVALID o UNAVAILABLE.
        content_hash: SHA-256 del contenido, cuando se pudo calcular.
        detail: Explicación, cuando el veredicto no es NEW.
        original_job_id: Trabajo ya existente, solo cuando el veredicto es DUPLICATE.
    """

    job_id: str
    verdict: str
    content_hash: str | None = None
    detail: str | None = None
    original_job_id: str | None = None
