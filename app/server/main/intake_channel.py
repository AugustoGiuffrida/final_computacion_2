"""El proceso de ingreso, visto desde el proceso principal.

Es lo único que toca las colas de este lado: lanza al hijo, le manda pedidos, espera sus
respuestas, lo supervisa y lo apaga. El resto del servidor solo ve un
`await intake.review(...)`, igual que cualquier otra corrutina.

Un problema explica casi todo lo que hay acá adentro: `Queue.get()` es bloqueante y el
servidor es asyncio. Si una corrutina llamara a `.get()`, no se frenaría esa corrutina
sino el event loop entero, y con él todos los clientes conectados. Por eso la espera
ocurre en un hilo aparte, y por eso hace falta un mecanismo para devolverle al event loop
lo que ese hilo saca de la cola.

El otro asunto que le da forma a este módulo es qué pasa cuando el hijo muere de golpe.
Una `multiprocessing.Queue` protege la lectura del pipe con un semáforo compartido entre
los dos procesos: si el hijo muere mientras lo tiene tomado, queda tomado para siempre y
la cola no sirve más. Por eso cuando el hijo muere no se relanza solo el proceso, se
rehace el canal entero.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
import threading
from collections.abc import Callable
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from typing import Any

from app.server import ipc
from app.server.intake import process

logger = logging.getLogger(__name__)

# Cuánto se espera un veredicto antes de darlo por perdido. Es una red de seguridad: si el
# hijo muere en medio de una revisión, su respuesta no va a llegar nunca y sin este tope
# el cliente esperaría para siempre.
REVIEW_TIMEOUT_SECONDS = 30

# Cuánto se le da al hijo para terminar por las suyas antes de matarlo.
SHUTDOWN_TIMEOUT_SECONDS = 5

# Cada cuánto el hilo lector deja de esperar para mirar si le pidieron parar. Un hilo
# bloqueado en `get()` no se puede despertar desde afuera, así que espera de a ratos
# cortos. De paso, si el semáforo de la cola quedó huérfano porque el hijo murió con él
# tomado, `get(timeout=...)` levanta `Empty` en lugar de colgarse para siempre.
READER_POLL_SECONDS = 0.5

class IntakeChannel:
    """El canal con el proceso de ingreso: lanzarlo, consultarlo, supervisarlo y apagarlo.

    Se lanza una vez al arrancar el servidor y se apaga al terminar. Entre medio la única
    operación es `review`, que se comporta como cualquier corrutina. Todo el manejo de
    procesos, colas e hilos queda encerrado acá.

    Attributes:
        child_entry_point: La función que ejecuta el hijo. Por defecto es la de verdad, y
            en uso normal nadie la pasa. Existe como parámetro porque con `spawn` el
            `target` se serializa por su nombre y el hijo lo importa de cero: sustituirlo
            desde afuera es la única forma de que las pruebas puedan tener un hijo que se
            muera, tarde de más o conteste desordenado.
    """

    def __init__(
        self,
        child_entry_point: Callable[..., None] = process.run_intake,
        log_level: int = logging.INFO,
    ) -> None:
        """Prepara las colas y el contexto, sin lanzar todavía nada.

        Args:
            child_entry_point: Función que corre en el hijo. Tiene que estar al nivel de
                un módulo para que `spawn` pueda encontrarla por su nombre.
            log_level: Nivel de registro que se le pasa al hijo, que con `spawn` arranca
                con el logging sin configurar y no hereda el del padre.
        """
        self.child_entry_point = child_entry_point
        self._log_level = log_level

        # Contexto explícito en lugar del método por defecto de la plataforma. Con `fork`
        # el hijo arrancaría siendo una copia de este proceso entero: el event loop, los
        # hilos y los sockets abiertos duplicados, en un estado que nadie puede usar. Con
        # `spawn` arranca limpio, importando solo lo que necesita.
        self._context = multiprocessing.get_context("spawn")

        self._process: BaseProcess | None = None
        self._response_reader: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Un futuro por revisión en curso, indexado por el job_id que va a traer la
        # respuesta. Todas vuelven por la misma cola y sin orden garantizado: este
        # diccionario es lo que permite saber a quién le corresponde cada una.
        self._pending: dict[str, asyncio.Future[ipc.ReviewResponse]] = {}

        self._open_channel()

    def start(self) -> None:
        """Lanza el proceso de ingreso y el hilo que recibe sus respuestas.

        Se llama una vez, desde dentro del event loop: necesita una referencia al loop
        para que el hilo lector pueda devolverle lo que saque de la cola.

        Returns:
            None.
        """
        self._loop = asyncio.get_running_loop()
        self._launch_process()
        self._start_reader()

    async def review(self, request: ipc.ReviewRequest) -> ipc.ReviewResponse: #
        """Le pide al ingreso que revise una imagen y espera su veredicto.

        Args:
            request: El pedido, con el `job_id` que va a identificar a la respuesta.

        Returns:
            El veredicto del ingreso. Si no llega a tiempo devuelve uno propio, con
            veredicto UNAVAILABLE: para quien llama, "no contestó" y "no pudo decidir" son
            el mismo caso, y conviene que no tenga que distinguirlos.
        """
        await self._relaunch_if_dead()

        verdict = asyncio.get_running_loop().create_future()
        self._pending[request.job_id] = verdict

        # `put` no bloquea: deja el pedido en un buffer y un hilo interno de la cola lo
        # escribe en el pipe. Por eso solo la lectura necesita un hilo propio.
        self._requests.put(request)

        try:
            return await asyncio.wait_for(verdict, REVIEW_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.error("el ingreso no respondió por %s a tiempo", request.job_id)
            return ipc.ReviewResponse(
                job_id=request.job_id,
                verdict=ipc.UNAVAILABLE,
                detail="el proceso de ingreso no respondió a tiempo",
            )
        finally:
            self._pending.pop(request.job_id, None)

    async def stop(self) -> None:
        """Apaga el proceso de ingreso y el hilo lector, en ese orden.

        Le pide al hijo que termine por las suyas y solo lo mata si no lo hace dentro de
        SHUTDOWN_TIMEOUT_SECONDS. La diferencia importa: terminando por las suyas alcanza
        a contestar las revisiones que tenía a medio hacer, y nadie queda esperando.

        Returns:
            None.
        """
        if self._process is None:
            return

        self._requests.put(ipc.SHUTDOWN) #mando mjs msj terminar el proceso hijo
        await asyncio.to_thread(self._process.join, SHUTDOWN_TIMEOUT_SECONDS) #join: espera a que el proceso se muera

        if self._process.is_alive():
            logger.warning("el ingreso no terminó solo; se lo corta por la fuerza")
            self._process.terminate()
            await asyncio.to_thread(self._process.join)

        await self._close_channel()

        self._fail_pending("el servidor se está apagando")
        self._process = None
        logger.info("proceso de ingreso terminado")

    def _open_channel(self) -> None:
        """Crea un par de colas nuevo, con el aviso de parada de su hilo lector.

        Se llama al construir y cada vez que hay que rehacer el canal, porque un hijo que
        muere de golpe puede dejar las colas inservibles.

        Returns:
            None.
        """
        self._requests = self._context.Queue()
        self._responses = self._context.Queue()
        self._stop_reading = threading.Event()

    async def _close_channel(self) -> None:
        """Descarta el canal: frena el hilo lector y cierra las dos colas.

        Es el espejo de `_open_channel`. Cierra las dos colas y no solo la que leía el
        hilo, porque lo que se está descartando es el canal entero: después de esto o se
        abre uno nuevo, o el servidor se apaga.

        El orden importa. El hilo tiene que haber terminado antes de que se cierre la cola
        de la que estaba leyendo, y por eso el `join` va primero.

        Returns:
            None.
        """
        self._stop_reading.set()

        if self._response_reader is not None:
            await asyncio.to_thread(self._response_reader.join, SHUTDOWN_TIMEOUT_SECONDS)
            self._response_reader = None

        # Las colas son dos pipes: cerrarlas devuelve sus descriptores al sistema.
        self._requests.close()
        self._responses.close()

    def _start_reader(self) -> None:
        """Arranca el hilo que saca respuestas de la cola.

        La cola y el aviso de parada se le pasan **por parámetro** y no se los deja leer
        de `self`: cuando el canal se rehace, `self._responses` pasa a ser otra cola, y un
        hilo que la leyera de `self` se pondría a leer la cola del hijo nuevo en medio de
        su propio apagado.

        Returns:
            None.
        """
        self._response_reader = threading.Thread(
            target=self._collect_responses,
            args=(self._responses, self._stop_reading),
            name="respuestas-de-ingreso",
            daemon=True,
        )
        self._response_reader.start()

    def _launch_process(self) -> None:
        """Crea y arranca el proceso hijo.

        `daemon=True` hace que no sobreviva al padre: si el servidor se muere de golpe, no
        queda un huérfano leyendo de una cola que nadie va a volver a usar.

        Returns:
            None.
        """
        self._process = self._context.Process(
            target=self.child_entry_point,
            args=(self._requests, self._responses, self._log_level),
            name="ingreso",
            daemon=True,
        )
        self._process.start()
        logger.info("proceso de ingreso lanzado (pid %s)", self._process.pid)

    async def _relaunch_if_dead(self) -> None:
        """Rehace el canal si el proceso de ingreso murió, antes de mandarle nada.

        No alcanza con relanzar el proceso: si murió mientras esperaba en la cola, se
        llevó tomado el semáforo que protege la lectura y el hijo nuevo se colgaría al
        pedirlo. Hay que descartar las colas junto con el proceso.

        La supervisión es perezosa —se verifica cuando hay algo que revisar, y no con un
        chequeo periódico— porque un ingreso muerto solo molesta cuando hay trabajo. La
        contra es que si murió con revisiones en curso y no entra ningún pedido nuevo,
        esas revisiones esperan su timeout en vez de cortarse enseguida.

        Returns:
            None.
        """
        if self._process is None or self._process.is_alive():
            return

        logger.error(
            "el proceso de ingreso terminó con código %s; se rehace el canal",
            self._process.exitcode,
        )
        self._fail_pending("el proceso de ingreso murió durante la revisión")

        await self._close_channel()
        self._open_channel()
        self._launch_process()
        self._start_reader()

    def _fail_pending(self, reason: str) -> None:
        """Da por perdidas todas las revisiones en curso.

        Args:
            reason: Qué pasó, para que quien esperaba pueda informarlo.

        Returns:
            None.
        """
        for job_id, verdict in self._pending.items():
            if not verdict.done():
                verdict.set_result(
                    ipc.ReviewResponse(
                        job_id=job_id, verdict=ipc.UNAVAILABLE, detail=reason
                    )
                )

        self._pending.clear()

    def _collect_responses(
        self, responses: Queue[Any], stop_reading: threading.Event
    ) -> None:
        """Bucle del hilo lector: saca respuestas de la cola y las lleva al event loop.

        Corre en un hilo y no en una corrutina porque `Queue.get()` bloquea hasta que hay
        algo. Es **un solo** hilo para todas las respuestas, y no uno por pedido: la cola
        es una sola, así que dos `get()` simultáneos se repartirían las respuestas al azar
        y cada uno podría llevarse la del otro.

        Args:
            responses: La cola de la que leer.
            stop_reading: Se activa cuando hay que terminar.

        Returns:
            None.
        """
        while not stop_reading.is_set():
            try:
                response = responses.get(timeout=READER_POLL_SECONDS)
            except queue.Empty:
                continue  # nada por ahora: se vuelve a mirar si hay que parar

            try:
                # El hilo no puede tocar los futuros: son del event loop
                # Le pasa la funcion al loop para que la llame él en la siguiente iteracion
                self._loop.call_soon_threadsafe(self._deliver, response)
            except RuntimeError:
                # El loop ya se cerró: el servidor se está apagando y nadie espera esto.
                return

    def _deliver(self, response: ipc.ReviewResponse) -> None:
        """Le entrega el veredicto a quien lo estaba esperando. Corre en el event loop.

        Args:
            response: El veredicto recién sacado de la cola.

        Returns:
            None.
        """
        verdict = self._pending.pop(response.job_id, None)

        if verdict is None or verdict.done():
            # Llegó tarde: el pedido ya se dio por vencido, o el cliente cortó.
            logger.debug("veredicto sin destinatario: %s", response.job_id)
            return

        verdict.set_result(response)
