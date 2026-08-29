"""El proceso de ingreso, visto desde el proceso principal.

Es lo único que toca el pipe de este lado: lanza al hijo, le manda pedidos, recibe sus
respuestas y lo apaga. El resto del servidor solo ve un `await intake.review(...)`, igual
que cualquier otra corrutina.

Se usa un `Pipe` y no dos colas porque acá hay exactamente dos procesos hablando en las
dos direcciones, que es para lo que sirve un pipe. Una `mp.Queue` está construida encima
de un pipe más un candado y un hilo auxiliar, y todo eso resuelve un problema que este
sistema no tiene: varios productores y varios consumidores compartiendo el mismo canal.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from collections.abc import Callable

from app.server import ipc
from app.server.intake import process

logger = logging.getLogger(__name__)

# Cuánto se espera un veredicto antes de darlo por perdido. Es una red de seguridad: si el
# hijo se cuelga en medio de una revisión, su respuesta no va a llegar nunca.
REVIEW_TIMEOUT_SECONDS = 30

# Cuánto se le da al hijo para terminar por las suyas antes de matarlo.
SHUTDOWN_TIMEOUT_SECONDS = 5

# Cada cuánto se vuelve a mirar si el pipe tiene algo. Es la única espera activa del
# archivo: entre vuelta y vuelta el event loop atiende a todos los clientes. Más chico
# responde antes y gasta más; 10 ms son cien miradas por segundo, que no se notan.
POLL_INTERVAL_SECONDS = 0.01


class IntakeChannel:
    """El canal con el proceso de ingreso: lanzarlo, consultarlo y apagarlo.

    Se abre una vez al arrancar el servidor y se cierra al terminar. Entre medio la única
    operación es `review`, que se comporta como cualquier corrutina.

    Attributes:
        child_entry_point: La función que ejecuta el hijo. Por defecto es la de verdad, y
            en uso normal nadie la pasa. Existe como parámetro porque con `spawn` el
            `target` se serializa por su nombre y el hijo lo importa de cero: sustituirlo
            desde afuera es la única forma de que las pruebas puedan tener un hijo que se
            muera o tarde de más.
    """

    def __init__(
        self,
        child_entry_point: Callable[..., None] = process.run_intake,
        log_level: int = logging.INFO,
    ) -> None:
        """Prepara el canal, sin abrirlo todavía.

        Args:
            child_entry_point: Función que corre en el hijo. Tiene que estar al nivel de
                un módulo para que `spawn` pueda encontrarla por su nombre.
            log_level: Nivel de registro que se le pasa al hijo.
        """
        self.child_entry_point = child_entry_point
        self._log_level = log_level

        # Contexto explícito en lugar del método por defecto de la plataforma. Con `fork`
        # el hijo arrancaría siendo una copia de este proceso entero —el event loop, los
        # sockets abiertos— en un estado que nadie puede usar. Con `spawn` arranca limpio,
        # importando solo lo que necesita.
        self._context = multiprocessing.get_context("spawn")

        # Los tres nacen en `_open_channel` o en `start`, y vuelven a None cuando el
        # canal se cierra. Que puedan estar en None es lo que verifican `review` y `stop`
        # antes de usarlos.
        self._process = None
        self._connection = None
        self._receiver = None
        self._stopping = False

        # Un futuro por revisión en curso, indexado por el job_id que va a traer la
        # respuesta. Puede haber muchas a la vez, una por cliente: este diccionario es lo
        # que permite saber a quién le corresponde cada respuesta que llega.
        self._pending: dict[str, asyncio.Future[ipc.ReviewResponse]] = {}

    def start(self) -> None:
        """Lanza el proceso de ingreso y la corrutina que recibe sus respuestas."""
        self._open_channel()
        self._receiver = asyncio.create_task(self._receive_loop())

    async def review(self, request: ipc.ReviewRequest) -> ipc.ReviewResponse:
        """Le pide al ingreso que revise una imagen y espera su veredicto.

        Args:
            request: El pedido, con el `job_id` que va a identificar a la respuesta.

        Returns:
            El veredicto del ingreso. Si el canal está cerrado o la respuesta no llega a
            tiempo, devuelve uno propio con veredicto UNAVAILABLE: para quien llama, "no
            contestó" y "no pudo decidir" son el mismo caso, y conviene que no tenga que
            distinguirlos.
        """
        if self._connection is None:
            return ipc.ReviewResponse(
                job_id=request.job_id,
                verdict=ipc.UNAVAILABLE,
                detail="el proceso de ingreso no está disponible",
            )

        verdict = asyncio.get_running_loop().create_future()
        self._pending[request.job_id] = verdict

        # send() escribe en el pipe
        self._connection.send(request)

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
        """Apaga el proceso de ingreso y cierra el canal.

        Le pide al hijo que termine por las suyas y solo lo mata si no lo hace dentro de
        SHUTDOWN_TIMEOUT_SECONDS. La diferencia importa: terminando por las suyas alcanza
        a contestar las revisiones que tenía a medio hacer, y nadie queda esperando.
        """
        if self._process is None:
            return

        # Marca el apagado como pedido. El hijo, al salir, va a cerrar su extremo del
        # pipe, y sin esta marca el receptor confundiría esa salida con una muerte y
        # lanzaría un hijo nuevo justo cuando estamos apagando.
        self._stopping = True

        try:
            self._connection.send(ipc.SHUTDOWN)
        except (OSError, AttributeError):
            pass  # el canal ya estaba cerrado: no hay a quién pedirle nada

        await self._wait_for_child(SHUTDOWN_TIMEOUT_SECONDS)

        if self._process.is_alive():
            logger.warning("el ingreso no terminó solo; se lo corta por la fuerza")
            self._process.terminate()
            await self._wait_for_child(SHUTDOWN_TIMEOUT_SECONDS)

        if self._receiver is not None:
            self._receiver.cancel()
            self._receiver = None

        # Si el receptor alcanzó a ver el fin de archivo, ya lo cerró. Si no llegó a
        # enterarse antes de que lo cancelaran, el descriptor queda abierto y hay que
        # devolverlo acá.
        if self._connection is not None:
            self._connection.close()
            self._connection = None

        self._fail_pending("el servidor se está apagando")
        self._process = None
        logger.info("proceso de ingreso terminado")

    def _open_channel(self) -> None:
        """Crea el pipe y lanza el proceso hijo con su extremo."""
        ours, theirs = self._context.Pipe()

        self._process = self._context.Process(
            target=self.child_entry_point,
            args=(theirs, self._log_level),
            name="ingreso",
            daemon=True,
        )
        self._process.start()

        # El padre suelta su copia del extremo del hijo. Mientras la conserve, el pipe
        # tendría dos escritores abiertos y no daría fin de archivo aunque el hijo
        # muriera: sin esta línea no habría forma de detectar su muerte.
        theirs.close()

        self._connection = ours
        logger.info("proceso de ingreso lanzado (pid %s)", self._process.pid)

    async def _receive_loop(self) -> None:
        """Recibe los veredictos del hijo y se los entrega a quien los espera.

        Es una corrutina y no un hilo porque nunca se bloquea: `poll()` contesta al
        instante si hay algo, y cuando no hay, el `await` le devuelve el control al event
        loop para que atienda a los clientes.
        """
        while True:
            try:
                # `poll()` sin argumento no espera nada. Con un argumento —`poll(0.01)`—
                # bloquearía ese tiempo, que es justo lo que no se puede hacer acá.
                if not self._connection.poll():
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                self._deliver(self._connection.recv())

            except asyncio.CancelledError:
                raise  # el apagado pidió terminar: esta no se atrapa

            except EOFError:
                if self._handle_child_gone():
                    return

            except Exception:
                # Cualquier otra falla se registra y el bucle sigue. Es lo único que
                # entrega los veredictos: si muriera, todas las revisiones vencerían por
                # timeout y no quedaría rastro de la causa, porque una tarea que muere
                # con excepción no avisa mientras alguien conserve su referencia.
                logger.exception("falla en el receptor de veredictos; se sigue")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _handle_child_gone(self) -> bool:
        """Reacciona a que el hijo haya cerrado su extremo del pipe.

        Returns:
            True si hay que terminar el bucle de recepción, False si se siguió adelante
            con un hijo nuevo.
        """
        self._connection.close()

        if self._stopping:
            self._connection = None
            return True

        # `join(0)` no espera nada: solo le pide al sistema el estado del hijo, que es lo
        # que rellena `exitcode`. Sin esto el código sale en None, porque nadie lo recogió
        # todavía, y el registro pierde el dato más útil para saber qué pasó.
        self._process.join(0)

        logger.error(
            "el proceso de ingreso murió con código %s; se relanza",
            self._process.exitcode if self._process else "desconocido",
        )
        self._fail_pending("el proceso de ingreso murió durante la revisión")
        self._open_channel()
        return False

    def _deliver(self, response: ipc.ReviewResponse) -> None:
        """Le entrega el veredicto a quien lo estaba esperando.

        Args:
            response: El veredicto recién sacado del pipe.
        """
        if not isinstance(response, ipc.ReviewResponse):
            # El hijo mandó algo que no es un veredicto. No debería pasar, pero si pasa
            # conviene enterarse por una línea de registro y no por el canal muerto.
            logger.error("el ingreso mandó algo que no es un veredicto: %r", response)
            return

        verdict = self._pending.pop(response.job_id, None)

        if verdict is None or verdict.done():
            # Llegó tarde: el pedido ya se dio por vencido, o el cliente cortó.
            logger.debug("veredicto sin destinatario: %s", response.job_id)
            return

        verdict.set_result(response)

    async def _wait_for_child(self, timeout: float) -> None:
        """Espera a que el proceso hijo termine, sin frenar el event loop.

        `join()` bloquearía; acá se pregunta con `is_alive()`, que contesta al instante, y
        se cede el control entre pregunta y pregunta. Es el mismo criterio que el bucle de
        recepción, para que en todo el archivo haya una sola forma de esperar.

        Args:
            timeout: Cuántos segundos esperar como mucho.
        """
        limit = time.monotonic() + timeout

        while self._process.is_alive() and time.monotonic() < limit:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _fail_pending(self, reason: str) -> None:
        """Da por perdidas todas las revisiones en curso.

        Args:
            reason: Qué pasó, para que quien esperaba pueda informarlo.
        """
        for job_id, verdict in self._pending.items():
            if not verdict.done():
                verdict.set_result(
                    ipc.ReviewResponse(
                        job_id=job_id, verdict=ipc.UNAVAILABLE, detail=reason
                    )
                )

        self._pending.clear()
