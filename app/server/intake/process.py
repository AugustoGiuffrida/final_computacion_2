"""El proceso de ingreso: revisa cada imagen antes de que el trabajo se encole.

El proceso principal lo lanza una vez, al arrancar, y vive hasta que lo apaga. Toda su
vida transcurre dentro de un bucle: sacar un pedido de la cola, resolverlo, poner la
respuesta en la otra cola.

Es un proceso y no un hilo por una única razón: aislamiento ante fallas. Acá se abre
contenido que mandó un cliente cualquiera, con bibliotecas que por debajo son código
nativo; ante una imagen preparada para eso, la falla no llega como una excepción de Python
sino como la muerte del intérprete. Siendo un proceso aparte, muere este y el principal
sigue atendiendo a todos sus clientes.

ESTADO: primera etapa. Calcula el hash del contenido y responde `new`. La verificación con
Pillow —que es la que produce el veredicto `invalid`— y la búsqueda de duplicados en
SQLite —que produce `duplicate`— se agregan después.
"""

from __future__ import annotations

import hashlib
import logging
import signal
from multiprocessing.queues import Queue
from pathlib import Path
from typing import Any

from app.server import ipc

logger = logging.getLogger(__name__)

# Cuánto se lee por vez al calcular el hash. Se lee de a bloques y no todo junto para que
# la memoria que usa el proceso no dependa del tamaño del archivo que le toque.
HASH_BLOCK_SIZE = 64 * 1024


def run_intake(requests: Queue[Any], responses: Queue[Any], log_level: int) -> None:
    """Cuerpo del proceso hijo: atiende pedidos hasta que le avisan que termine.

    Es la función que `multiprocessing.Process` ejecuta del otro lado. Tiene que estar al
    nivel del módulo —no ser un método ni una función anidada— porque el método de arranque
    es *spawn*: el hijo no hereda la memoria del padre, sino que importa este módulo de
    cero y busca la función por su nombre.

    Args:
        requests: Cola por donde llegan los `ReviewRequest` y, al final, el sentinela.
        responses: Cola por donde se devuelven los `ReviewResponse`.
        log_level: Nivel de registro, que se recibe en lugar de heredarse por la misma
            razón: con *spawn* el hijo arranca con el logging sin configurar, y sus
            mensajes se perderían sin que nadie se entere.

    Returns:
        None.
    """
    logging.basicConfig(
        level=log_level, format="%(asctime)s [ingreso] %(levelname)s %(message)s"
    )

    # Ctrl-C no le llega solo al proceso principal: la señal va a todo el grupo de procesos
    # en primer plano, este incluido. Si se muriera acá, las revisiones en curso quedarían
    # sin respuesta y sus clientes esperando. Ignorándola, termina solo cuando el principal
    # se lo pide por la cola, que es cuando ya no queda nada pendiente.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    logger.info("proceso de ingreso en marcha")

    while True:
        request = requests.get()  # bloquea hasta que haya algo que hacer

        if request == ipc.SHUTDOWN:
            logger.info("el proceso principal pidió terminar")
            return

        responses.put(review(request))


def review(request: ipc.ReviewRequest) -> ipc.ReviewResponse:
    """Resuelve un pedido de revisión y arma la respuesta.

    Atrapa cualquier excepción a propósito, y no un tipo de error en particular. Es el
    borde de un proceso cuyo trabajo es sobrevivir a entradas hostiles: si una excepción
    escapara, moriría el proceso y quien esperaba esta respuesta se quedaría esperando
    hasta que venza su timeout. Lo único que este resguardo no puede cubrir es lo que
    Python no puede atrapar —una falla dentro de código nativo— y para eso está el hecho
    de ser un proceso aparte.

    Args:
        request: El pedido a resolver.

    Returns:
        El veredicto, siempre con el mismo `job_id` del pedido.
    """
    try:
        content_hash = hash_of(request.stored_path)
    except Exception as failure:
        logger.exception("no se pudo revisar %s", request.job_id)
        return ipc.ReviewResponse(
            job_id=request.job_id,
            verdict=ipc.UNAVAILABLE,
            detail=f"la revisión falló: {failure}",
        )

    logger.info("%s revisado: %s", request.job_id, content_hash[:12])

    return ipc.ReviewResponse(
        job_id=request.job_id,
        verdict=ipc.NEW,
        content_hash=content_hash,
    )


def hash_of(path: Path) -> str:
    """Calcula el SHA-256 del contenido de un archivo, leyéndolo de a bloques.

    Args:
        path: Ruta del archivo a leer.

    Returns:
        El hash en hexadecimal, de 64 caracteres.
    """
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while block := file.read(HASH_BLOCK_SIZE):
            digest.update(block)

    return digest.hexdigest()
