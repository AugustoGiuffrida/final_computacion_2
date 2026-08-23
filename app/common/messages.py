"""Catálogo del protocolo de aplicación: qué mensajes existen y qué responde cada uno.

ESTADO: pendiente de aprobación del profesor. Vive en un módulo propio, separado del
framing de `protocol.py`, precisamente para que pueda cambiar entero sin arrastrar al
resto del código. `protocol.py` resuelve cómo se delimita un mensaje sobre TCP y no sabe
nada de esta aplicación; este módulo define qué dicen esos mensajes. El framing es el
sobre, este módulo es el idioma de la carta.

Acá solo hay nombres y constantes, no lógica. El cliente los usa para armar sus pedidos e
interpretar las respuestas; el servidor los usará para lo simétrico. Tener un único lugar
donde están escritos evita que una cadena mal tipeada en un extremo se descubra recién en
tiempo de ejecución.

La especificación completa, con ejemplos de cada mensaje, está en `docs/04_protocolo.md`.
"""

from __future__ import annotations

from typing import Any

# ────────────────────────── tipos de mensaje ──────────────────────────

# Campo que dice de qué mensaje se trata. Está en todos.
TYPE_FIELD = "type"

# Pedidos: siempre los inicia el cliente.

# Enviar una imagen. Es el único pedido que lleva payload.
SUBMIT = "submit"

# Consultar el estado de un trabajo y los datos que produjo.
STATUS = "status"

# Pedir el archivo resultante. El payload vuelve.
DOWNLOAD = "download"

# Listar los últimos trabajos del usuario.
HISTORY = "history"

# Los cuatro pedidos que el servidor acepta; cualquier otro tipo es BAD_REQUEST.
REQUEST_TYPES = frozenset({SUBMIT, STATUS, DOWNLOAD, HISTORY})

# Respuestas: siempre las emite el servidor.

# El pedido se atendió; los demás campos dependen de cuál era.
OK = "ok"

# El pedido no se pudo atender. Trae 'code' y 'message'.
ERROR = "error"

# ──────────────────────── estados de un trabajo ────────────────────────

# Encolado; ningún worker lo tomó todavía.
QUEUED = "QUEUED"

# Un worker lo está ejecutando.
PROCESSING = "PROCESSING"

# Terminó bien. Recién acá tiene sentido pedir la descarga.
DONE = "DONE"

# Falló definitivamente; el motivo viene en el campo 'error'. La constante se llama
# FAILED, y no ERROR como su valor, para no chocar con el tipo de mensaje ERROR.
FAILED = "ERROR"

# Estados de los que un trabajo ya no se mueve: es la condición de corte de la espera.
TERMINAL_STATUSES = frozenset({DONE, FAILED})

# ────────────────────────── códigos de error ──────────────────────────

BAD_REQUEST = "BAD_REQUEST"
UNKNOWN_OP = "UNKNOWN_OP"
INVALID_IMAGE = "INVALID_IMAGE"
TOO_LARGE = "TOO_LARGE"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
FORBIDDEN = "FORBIDDEN"
NOT_READY = "NOT_READY"
NO_OUTPUT = "NO_OUTPUT"
INTERNAL = "INTERNAL"

# Explicación de respaldo de cada código, para cuando el servidor no mande un 'message'
# propio o el suyo sea demasiado escueto.
ERROR_EXPLANATIONS = {
    BAD_REQUEST: "el pedido está mal formado o le faltan campos obligatorios",
    UNKNOWN_OP: "la operación pedida no existe",
    INVALID_IMAGE: "el archivo no es una imagen válida o su formato no está soportado",
    TOO_LARGE: "la imagen excede el tamaño máximo que acepta el servidor",
    JOB_NOT_FOUND: "no existe un trabajo con ese identificador",
    FORBIDDEN: "ese trabajo pertenece a otro usuario",
    NOT_READY: "el trabajo todavía no terminó, no hay nada para descargar",
    NO_OUTPUT: "esa operación no genera archivo; su resultado está en la consulta de estado",
    INTERNAL: "el servidor tuvo un problema inesperado",
}


# ────────────────────────── errores del servidor ──────────────────────────


class RequestError(Exception):
    """El servidor no puede atender un pedido.

    No se levanta directamente: se levanta alguna de sus subclases, una por código. El
    código y el comportamiento son atributos de la clase, así que donde se detecta el
    problema solo se escribe la explicación:

        raise BadRequest("falta el campo 'filename' o está vacío")

    Atrapar `RequestError` los sigue atrapando a todos, porque todos heredan de acá.

    Es la contraparte de `ServerError`: aquella la levanta el **cliente** cuando recibe un
    mensaje de error; esta la levanta el **servidor** cuando necesita enviarlo.

    No es un error de comunicación: el mensaje llegó bien y se entendió, pero no se puede
    responder lo que pide. Por eso casi ninguno cierra la conexión; el cliente puede
    seguir usándola para el pedido siguiente.

    Attributes:
        code: El código que viaja al cliente. Lo fija cada subclase.
        closes_connection: Si después de responder hay que cerrar. Lo fija cada subclase.
        detail: La explicación para el usuario.
    """

    code: str = INTERNAL
    closes_connection: bool = False

    def __init__(self, detail: str = "") -> None:
        """Construye el error con su explicación.

        Args:
            detail: Explicación concreta del problema. Si se omite se usa la explicación
                estándar del código, que alcanza cuando el motivo es siempre el mismo.
        """
        self.detail = detail or ERROR_EXPLANATIONS[self.code]
        super().__init__(f"{self.code}: {self.detail}")


class BadRequest(RequestError):
    """Se levanta cuando falta un campo obligatorio o su valor no es del tipo esperado."""

    code = BAD_REQUEST


class UnknownOperation(RequestError):
    """Se levanta cuando el pedido nombra una operación que no está en el catálogo."""

    code = UNKNOWN_OP


class InvalidImage(RequestError):
    """Se levanta cuando el archivo no es una imagen que el servidor pueda procesar."""

    code = INVALID_IMAGE


class TooLarge(RequestError):
    """Se levanta cuando el payload anunciado supera el máximo que acepta el servidor.

    Es el único que cierra la conexión, y no es una decisión de quien lo levanta sino de
    la situación: el cliente anunció bytes que el servidor no va a leer, así que quedarían
    en el socket y el próximo mensaje se leería corrido. Cerrar es la única forma de
    recuperar la sincronización del diálogo.
    """

    code = TOO_LARGE
    closes_connection = True


class JobNotFound(RequestError):
    """Se levanta cuando no hay ningún trabajo con el identificador pedido."""

    code = JOB_NOT_FOUND


class Forbidden(RequestError):
    """Se levanta cuando el trabajo existe pero es de otro usuario."""

    code = FORBIDDEN


class NotReady(RequestError):
    """Se levanta cuando se pide el resultado de un trabajo que todavía no terminó."""

    code = NOT_READY


class NoOutput(RequestError):
    """Se levanta cuando el trabajo terminó pero no dejó ningún archivo para descargar."""

    code = NO_OUTPUT


class InternalError(RequestError):
    """Se levanta cuando el problema es del servidor y no del pedido."""

    code = INTERNAL


class ServerError(Exception):
    """El servidor respondió un mensaje de tipo `error`.

    No es una falla de comunicación —el diálogo funcionó— sino un rechazo del pedido: el
    trabajo no existe, la imagen es inválida, la descarga no está lista. Lleva el código
    además del texto, para que quien la atrape pueda decidir según el caso en vez de
    tener que interpretar un mensaje en prosa.

    Attributes:
        code: Uno de los códigos de este módulo; `INTERNAL` si el servidor no mandó ninguno.
        message: El texto del servidor, o la explicación estándar del código.
    """

    def __init__(self, code: str, message: str = "") -> None:
        """Construye el error a partir de los campos de la respuesta."""
        self.code = code or INTERNAL
        self.message = message or ERROR_EXPLANATIONS.get(self.code, "error desconocido")
        super().__init__(f"{self.code}: {self.message}")


def raise_if_error(header: dict[str, Any]) -> dict[str, Any]:
    """Convierte una respuesta de tipo `error` en excepción, y deja pasar el resto.

    Se llama sobre toda respuesta apenas se recibe, para que el resto del cliente pueda
    asumir que trabaja sobre un `ok` sin preguntar por el tipo en cada lugar.

    Returns:
        El mismo header que recibió, cuando no es un error.

    Raises:
        ServerError: Si el campo 'type' vale `error`.
    """
    if header.get(TYPE_FIELD) == ERROR:
        raise ServerError(header.get("code", ""), header.get("message", ""))

    return header
