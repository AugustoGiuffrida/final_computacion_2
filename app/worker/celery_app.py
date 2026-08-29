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
)

# El worker encuentra las tareas solo: busca un `tasks.py` en cada paquete listado.
celery_app.autodiscover_tasks(["app.worker"])
