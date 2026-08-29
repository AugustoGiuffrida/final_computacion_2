"""El proceso de ingreso: revisa cada imagen antes de que el trabajo se encole.

El proceso principal lo lanza una vez, al arrancar, y vive hasta que lo apaga. Toda su
vida transcurre dentro de un bucle: sacar un pedido del pipe, resolverlo, devolver la
respuesta por el mismo pipe.

Es un proceso y no un hilo por una única razón: aislamiento ante fallas. Acá se abre
contenido que mandó un cliente cualquiera, con bibliotecas que por debajo son código
nativo; ante una imagen preparada para eso, la falla no llega como una excepción de Python
sino como la muerte del intérprete. Siendo un proceso aparte, muere este y el principal
sigue atendiendo a todos sus clientes.

ESTADO: verifica la imagen, calcula su hash, busca duplicados y registra el trabajo.
Falta persistir los eventos del ciclo de vida, que necesitan que exista quien los produzca.
"""

from __future__ import annotations

import hashlib
import logging
import signal
from multiprocessing.connection import Connection
from pathlib import Path

from PIL import Image

from app.common import config
from app.server import database, ipc

logger = logging.getLogger(__name__)

# Cuánto se lee por vez al calcular el hash. Se lee de a bloques y no todo junto para que
# la memoria que usa el proceso no dependa del tamaño del archivo que le toque.
HASH_BLOCK_SIZE = 64 * 1024


class InvalidImageError(Exception):
    """El archivo no es una imagen que este sistema pueda procesar.

    Lleva al veredicto INVALID. Se distingue de cualquier otra falla porque la culpa es
    del pedido y no del servidor, que es UNAVAILABLE.
    """


def run_intake(connection: Connection, log_level: int, database_path: Path) -> None:
    """Cuerpo del proceso hijo: atiende pedidos hasta que le avisan que termine.

    Es la función que `multiprocessing.Process` ejecuta del otro lado. Tiene que estar al
    nivel del módulo —no ser un método ni una función anidada— porque el método de arranque
    es *spawn*: el hijo no hereda la memoria del padre, sino que importa este módulo de
    cero y busca la función por su nombre.

    Args:
        connection: Su extremo del pipe. Por el mismo camino llegan los pedidos y se
            devuelven los veredictos: las dos direcciones son independientes.
        log_level: Nivel de registro, que se recibe en lugar de heredarse por la misma
            razón: con *spawn* el hijo arranca con el logging sin configurar, y sus
            mensajes se perderían sin que nadie se entere.
        database_path: Dónde está la base. Se recibe en vez de leerla de la configuración
            para que las pruebas puedan apuntar a una temporal.
    """
    logging.basicConfig(
        level=log_level, format="%(asctime)s [ingreso] %(levelname)s %(message)s"
    )

    # Ctrl-C no le llega solo al proceso principal: la señal va a todo el grupo de procesos
    # en primer plano, este incluido. Si se muriera acá, las revisiones en curso quedarían
    # sin respuesta y sus clientes esperando. Ignorándola, termina solo cuando el principal
    # se lo pide por el pipe, que es cuando ya no queda nada pendiente.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    records = database.JobWriter(database_path)
    logger.info("proceso de ingreso en marcha, base en %s", database_path)

    try:
        while True:
            try:
                request = connection.recv()  # bloquea hasta que haya algo que hacer
            except EOFError:
                # El proceso principal cerró su extremo del pipe: se murió sin avisar.
                # Sin este control el hijo quedaría vivo para siempre, hablándole a nadie.
                logger.warning("el proceso principal desapareció")
                return

            if request == ipc.SHUTDOWN:
                logger.info("el proceso principal pidió terminar")
                return

            connection.send(review(request, records))

    finally:
        records.close()


def review(
    request: ipc.ReviewRequest, records: database.JobWriter
) -> ipc.ReviewResponse:
    """Resuelve un pedido de revisión y arma la respuesta.

    Distingue dos clases de problema: que el archivo del cliente no sirva (INVALID) y que
    la revisión haya fallado (UNAVAILABLE). El `except Exception` es deliberado: si una
    excepción escapara moriría el proceso, y quien espera el veredicto se comería su
    plazo entero.

    Returns:
        El veredicto, siempre con el mismo `job_id` del pedido.
    """
    try:
        image_format = verify_image(request.stored_path)
        content_hash = hash_of(request.stored_path)

        original = records.find_duplicate(
            request.user, content_hash, request.operation, request.parameters
        )
        if original is None:
            records.insert(request, content_hash)

    except InvalidImageError as rejection:
        logger.info("%s rechazado: %s", request.job_id, rejection)
        return ipc.ReviewResponse(
            job_id=request.job_id, verdict=ipc.INVALID, detail=str(rejection)
        )

    except Exception as failure:
        logger.exception("no se pudo revisar %s", request.job_id)
        return ipc.ReviewResponse(
            job_id=request.job_id,
            verdict=ipc.UNAVAILABLE,
            detail=f"la revisión falló: {failure}",
        )

    if original is not None:
        logger.info("%s duplica a %s", request.job_id, original)
        return ipc.ReviewResponse(
            job_id=request.job_id,
            verdict=ipc.DUPLICATE,
            content_hash=content_hash,
            original_job_id=original,
        )

    logger.info("%s revisado: %s, %s", request.job_id, image_format, content_hash[:12])

    return ipc.ReviewResponse(
        job_id=request.job_id, verdict=ipc.NEW, content_hash=content_hash
    )


def verify_image(path: Path) -> str:
    """Verifica que el archivo sea una imagen íntegra de un formato soportado.

    Abre el archivo dos veces a propósito: `verify()` revisa la estructura y lo deja
    cerrado, `load()` decodifica los píxeles. A un JPEG al que le faltan cinco bytes del
    final, el primero lo da por bueno y el segundo lo rechaza.

    Returns:
        El formato que reconoció Pillow: 'JPEG' o 'PNG'.

    Raises:
        InvalidImageError: Si no se puede abrir, está corrupta, tiene dimensiones
            desproporcionadas o su formato no está soportado.
    """
    try:
        with Image.open(path) as image:
            image_format = image.format
            image.verify()

        with Image.open(path) as image:
            image.load()

    except Image.DecompressionBombError as bomb:
        # Un archivo chico que se descomprime a una imagen gigantesca: es un ataque
        # conocido, no un error de formato. Se nombra aparte porque no hereda de OSError.
        raise InvalidImageError(f"dimensiones desproporcionadas: {bomb}") from bomb

    except FileNotFoundError:
        # Que el archivo no esté es problema nuestro, no del cliente: propaga y termina
        # en UNAVAILABLE. Va antes del OSError porque hereda de él.
        raise

    except (OSError, ValueError, SyntaxError) as failure:
        raise InvalidImageError(f"no se pudo abrir como imagen: {failure}") from failure

    if image_format not in config.SUPPORTED_IMAGE_FORMATS:
        supported = ", ".join(sorted(config.SUPPORTED_IMAGE_FORMATS))
        raise InvalidImageError(
            f"el contenido es {image_format} y solo se aceptan: {supported}"
        )

    return image_format


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
