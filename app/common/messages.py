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

from typing import Any, Final

# ────────────────────────── tipos de mensaje ──────────────────────────

TYPE_FIELD: Final[str] = "type" #Campo que dice de qué mensaje se trata. Está en todos.

# Pedidos: siempre los inicia el cliente.
SUBMIT: Final[str] = "submit" #Enviar una imagen. Es el único pedido que lleva payload.

STATUS: Final[str] = "status" #Consultar el estado de un trabajo y los datos que produjo.

DOWNLOAD: Final[str] = "download" #Pedir el archivo resultante. El payload vuelve.

HISTORY: Final[str] = "history" #Listar los últimos trabajos del usuario.

# Los cuatro pedidos que el servidor acepta; cualquier otro tipo es BAD_REQUEST.
REQUEST_TYPES: Final[frozenset[str]] = frozenset({SUBMIT, STATUS, DOWNLOAD, HISTORY})

# Respuestas: siempre las emite el servidor.
OK: Final[str] = "ok" #El pedido se atendió; los demás campos dependen de cuál era.

ERROR: Final[str] = "error" #El pedido no se pudo atender. Trae 'code' y 'message'.

# ──────────────────────── estados de un trabajo ────────────────────────

QUEUED: Final[str] = "QUEUED" #Encolado; ningún worker lo tomó todavía.

PROCESSING: Final[str] = "PROCESSING" #Un worker lo está ejecutando.

DONE: Final[str] = "DONE" #Terminó bien. Recién acá tiene sentido pedir la descarga.

# Falló definitivamente; el motivo viene en el campo 'error'. La constante se llama
# FAILED, y no ERROR como su valor, para no chocar con el tipo de mensaje ERROR.
FAILED: Final[str] = "ERROR"

# Estados de los que un trabajo ya no se mueve: es la condición de corte de la espera.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({DONE, FAILED})

# ────────────────────────── códigos de error ──────────────────────────

BAD_REQUEST: Final[str] = "BAD_REQUEST"
UNKNOWN_OP: Final[str] = "UNKNOWN_OP"
INVALID_IMAGE: Final[str] = "INVALID_IMAGE"
TOO_LARGE: Final[str] = "TOO_LARGE"
JOB_NOT_FOUND: Final[str] = "JOB_NOT_FOUND"
FORBIDDEN: Final[str] = "FORBIDDEN"
NOT_READY: Final[str] = "NOT_READY"
NO_OUTPUT: Final[str] = "NO_OUTPUT"
INTERNAL: Final[str] = "INTERNAL"

# Explicación de respaldo de cada código, para cuando el servidor no mande un 'message'
# propio o el suyo sea demasiado escueto.
ERROR_EXPLANATIONS: Final[dict[str, str]] = {
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


class RequestError(Exception):
    """El servidor no puede atender un pedido, con el código que lo explica.

    Es la contraparte de `ServerError`: aquella la levanta el **cliente** cuando recibe un
    mensaje de error; esta la levanta el **servidor** cuando necesita enviarlo. Las dos
    llevan el código además del texto, para que quien las atrape pueda decidir según el
    caso en vez de interpretar un mensaje en prosa.

    No es un error de comunicación: el mensaje llegó bien y se entendió, pero no se puede
    responder lo que pide. Por eso **no cierra la conexión**: el cliente puede seguir
    haciendo pedidos.

    Attributes:
        code: Uno de los códigos de este módulo.
        detail: Explicación para el usuario.
    """

    def __init__(self, code: str, detail: str) -> None:
        """Construye el error con su código y su explicación."""
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


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
