"""La instancia de Celery: la conexión con el broker y la configuración de las tareas.

Es el único objeto que comparten el servidor y los workers: el servidor la usa para
encolar y consultar estados; el worker, para registrar y ejecutar las tareas. La
estructura —este módulo con la instancia, `tasks.py` con las tareas— es la de la guía de
la cátedra (Clase 23).
"""

from __future__ import annotations

from celery import Celery

from app.common import config

celery_app = Celery(
    "images",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # JSON y no pickle: por la cola viajan datos, nunca objetos ejecutables.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Sin esto, un trabajo que un worker ya tomó sigue figurando PENDING. El estado
    # STARTED es el que el monitor traduce a PROCESSING.
    task_track_started=True,

    # El mensaje se confirma DESPUÉS de ejecutar la tarea, no al tomarla: si el worker
    # muere a mitad de una imagen, el broker se la vuelve a dar a otro.
    task_acks_late=True,

    # El worker toma UNA tarea por vez en lugar de pre-reservar un lote. Acompaña a
    # acks_late en la guía de la cátedra: si el worker muere, se traba una tarea, no
    # varias.
    worker_prefetch_multiplier=1,

    # Los resultados en Redis expiran: Redis es estado vivo y efímero, la verdad
    # permanente es SQLite (docs/03).
    result_expires=24 * 60 * 60,

    # Un Redis que no contesta —vivo pero mudo, que es como se ve una partición de red o
    # un contenedor pausado— no levanta ningún error: sin plazo, `delay` espera la
    # respuesta para siempre, y con él el cliente que mandó la imagen y el hilo del pool
    # que lo atiende. Con plazo, la espera se vuelve excepción: el trabajo queda FAILED,
    # el cliente recibe INTERNAL y el hilo vuelve al pool. Tres segundos es mucho para
    # un Redis sano —responde en milisegundos— y poco para uno que no va a responder.
    broker_transport_options={"socket_timeout": 3, "socket_connect_timeout": 3},
    redis_socket_timeout=3,
    redis_socket_connect_timeout=3,

    # El plazo solo no alcanza: cada capa reintenta por su cuenta y, sin tope, la espera
    # sigue siendo larga. `delay` primero se suscribe al resultado en el backend (que
    # por defecto reintenta 20 veces) y después publica en el broker (3 veces). Con estos
    # topes, un Redis mudo cuesta unos veinte segundos como mucho, y no dos minutos.
    result_backend_transport_options={"retry_policy": {"max_retries": 3}},
    task_publish_retry_policy={
        "max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 1,
    },
)

# El worker encuentra las tareas solo: busca un `tasks.py` en cada paquete listado.
celery_app.autodiscover_tasks(["app.worker"])
