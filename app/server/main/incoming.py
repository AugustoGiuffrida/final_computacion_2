"""Lo que entra: validar los campos del header y consumir el payload.

Cada función toma el header crudo —un diccionario que armó el cliente y del que no se
puede asumir nada— y devuelve el valor ya verificado, o levanta el error del protocolo que
corresponda. Los handlers las encadenan al principio, de modo que el resto del handler
trabaja sobre datos en los que ya puede confiar.

Las dos últimas se ocupan del payload: una lo guarda y la otra lo descarta, pero las dos
lo leen entero. Un pedido se consume completo incluso cuando se rechaza, porque los bytes
que queden sin leer siguen en el buffer del socket y el mensaje siguiente los
interpretaría como su propio prefijo de longitud.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.common import config, protocol
from app.common.messages import BadRequest, InvalidImage, UnknownOperation


# ──────────────────────── campos del header ────────────────────────

def require_text_field(header: dict[str, Any], field: str) -> str:
    """Lee un campo de texto obligatorio del header.

    Args:
        header: Header del pedido, ya deserializado.
        field: Nombre del campo a leer.

    Returns:
        El valor, sin espacios sobrantes.

    Raises:
        BadRequest: Si falta, está vacío o no es texto.
    """
    value = header.get(field)

    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"falta el campo '{field}' o está vacío")

    return value.strip()


def require_user(header: dict[str, Any]) -> str:
    """El usuario declarado en el pedido. Los cuatro pedidos del protocolo lo llevan."""
    return require_text_field(header, "user")


def read_limit(header: dict[str, Any]) -> int:
    """Lee cuántos trabajos devolver en el historial, con un tope propio del servidor.

    El tope existe porque el límite lo elige el cliente: sin él, un pedido de un millón de
    filas haría trabajar al servidor mucho más de lo razonable. Se recorta en silencio en
    vez de rechazar el pedido, porque pedir de más no es un error del cliente: no tiene
    por qué conocer nuestro tope, y sería un error que no puede corregir.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        La cantidad a devolver, acotada a `config.MAX_HISTORY_LIMIT`.

    Raises:
        BadRequest: Si el valor no es un entero positivo.
    """
    limit = header.get("limit", config.DEFAULT_HISTORY_LIMIT)

    # El bool se descarta aparte porque en Python es subclase de int: sin esto, un
    # 'limit' de `true` pasaría como si fuera 1.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise BadRequest("'limit' debe ser un entero positivo")

    return min(limit, config.MAX_HISTORY_LIMIT)



def require_job_id(header: dict[str, Any]) -> str:
    """El identificador de trabajo del pedido. Lo llevan `status` y `download`."""
    return require_text_field(header, "job_id")


def require_operation(header: dict[str, Any]) -> str:
    """Lee la operación pedida, verificando que exista.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        El nombre de la operación.

    Raises:
        UnknownOperation: Si falta o no es una de las soportadas.
    """
    operation = header.get("op")

    if not isinstance(operation, str) or operation not in config.OPERATION_PARAMETERS:
        supported = ", ".join(sorted(config.OPERATION_PARAMETERS))
        raise UnknownOperation(
            f"la operación '{operation}' no existe; las disponibles son: {supported}"
        )

    return operation


def read_parameters(header: dict[str, Any], operation: str) -> dict[str, Any]:
    """Lee los parámetros del pedido, verificando que correspondan a esa operación.

    Se validan los **nombres**, no los valores: qué significa cada parámetro y qué rangos
    admite lo sabe el worker que lo va a usar. El cliente ya verifica los rangos por
    comodidad, pero eso no se puede dar por hecho — cualquiera puede hablar el protocolo
    sin usar nuestro cliente.

    Un parámetro que la operación no acepta se rechaza en vez de ignorarse: casi siempre
    es una confusión, y silenciarlo haría creer que se aplicó.

    Args:
        header: Header del pedido, ya deserializado.
        operation: La operación, ya validada.

    Returns:
        Los parámetros pedidos, o un diccionario vacío si no vinieron.

    Raises:
        BadRequest: Si no es un objeto, o si trae un parámetro ajeno a la operación.
    """
    parameters = header.get("params", {})

    if not isinstance(parameters, dict):
        raise BadRequest("'params' debe ser un objeto")

    accepted = config.OPERATION_PARAMETERS[operation]
    for name in parameters:
        if name not in accepted:
            detail = (
                f"acepta: {', '.join(accepted)}" if accepted else "no acepta parámetros"
            )
            raise BadRequest(f"'{name}' no es un parámetro de '{operation}'; {detail}")

    return parameters


def safe_filename(header: dict[str, Any]) -> str:
    """Lee el nombre del archivo del pedido, dejándolo seguro para usar como ruta.

    El nombre lo elige el cliente, así que **no se puede confiar en él**. Un valor como
    `../../etc/passwd` construiría una ruta fuera del directorio del trabajo.
    `Path().name` se queda solo con el último componente, lo que elimina esa posibilidad
    de raíz en lugar de intentar detectar los casos peligrosos uno por uno.

    Args:
        header: Header del pedido, ya deserializado.

    Returns:
        El nombre del archivo, sin ninguna parte de ruta.

    Raises:
        BadRequest: Si falta o no queda un nombre de archivo usable.
        InvalidImage: Si la extensión no está soportada.
    """
    raw_name = header.get("filename")

    if not isinstance(raw_name, str) or not raw_name.strip():
        raise BadRequest("falta el campo 'filename' o está vacío")

    name = Path(raw_name).name
    if not name or name in (".", ".."):
        raise BadRequest(f"'{raw_name}' no es un nombre de archivo válido")

    if Path(name).suffix.lower() not in config.SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(config.SUPPORTED_EXTENSIONS))
        raise InvalidImage(
            f"la extensión de '{name}' no está soportada; se aceptan: {supported}"
        )

    return name


def require_non_empty_image(payload_size: int) -> None:
    """Verifica que el envío traiga efectivamente una imagen.

    El límite superior no se controla acá: lo aplica `dispatch` sobre todos los pedidos,
    porque un payload excesivo obliga además a cortar la conexión.

    Args:
        payload_size: Bytes declarados en el header.

    Raises:
        BadRequest: Si el envío viene sin contenido.
    """
    if payload_size == 0:
        raise BadRequest("un envío tiene que traer una imagen")

# ──────────────────────────── el payload ────────────────────────────


async def save_upload(
    reader: asyncio.StreamReader, payload_size: int, destination: Path
) -> None:
    """Vuelca el payload del pedido en un archivo, bloque por bloque.

    No acumula la imagen en memoria: la escribe a medida que llega. Con muchos clientes
    subiendo archivos grandes, esa es la diferencia entre usar unos kilobytes por conexión
    y usar el tamaño entero de cada imagen.

    Args:
        reader: Stream de lectura de la conexión.
        payload_size: Cuántos bytes leer, según lo declarado en el header.
        destination: Dónde escribir. Su directorio se crea si no existe.

    Raises:
        asyncio.IncompleteReadError: Si la conexión se corta antes de completar los bytes
            anunciados. El archivo parcial queda en disco: limpiarlo le toca a quien
            llama, que es el que sabe qué más hay que deshacer.
        OSError: Si no se puede crear el directorio o escribir el archivo.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    with open(destination, "wb") as image_file:
        async for chunk in protocol.stream_payload(reader, payload_size):
            image_file.write(chunk)


async def discard_payload(reader: asyncio.StreamReader, payload_size: int) -> None:
    """Lee y descarta el payload de un pedido que no se va a usar.

    Hace falta incluso al rechazar un pedido: el payload ya viene en camino y hay que
    sacarlo del socket para que el mensaje siguiente empiece donde debe.

    Args:
        reader: Stream de lectura de la conexión.
        payload_size: Cuántos bytes descartar. Si es cero, no hace nada.
    """
    if payload_size == 0:
        return

    async for _chunk in protocol.stream_payload(reader, payload_size):
        pass  # se lee para vaciar el socket; el contenido no interesa
