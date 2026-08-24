"""Pruebas del canal con el proceso de ingreso y del proceso en sí.

Acá se lanzan procesos hijos de verdad: es lo único que puede demostrar que el mecanismo
funciona a través de la frontera entre dos procesos. Lo que sí es de mentira es *qué hace*
el hijo, y por eso el canal recibe su punto de entrada como parámetro: cada prueba le
pasa un hijo que hace exactamente lo que necesita —contestar, tardar, morirse, contestar
desordenado— que es algo que el hijo de verdad no permite provocar.

Las funciones que corren en el hijo están al nivel del módulo porque el método de arranque
es *spawn*: el hijo importa este archivo de cero y busca la función por su nombre.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.server import ipc
from app.server.intake import process
from app.server.main import intake_channel
from app.server.main.intake_channel import IntakeChannel


# ──────────────────────── hijos de mentira ────────────────────────


def child_that_accepts(connection, log_level: int) -> None:
    """Acepta todo lo que le llega, al instante."""
    while True:
        request = connection.recv()
        if request == ipc.SHUTDOWN:
            return
        connection.send(
            ipc.ReviewResponse(
                job_id=request.job_id, verdict=ipc.NEW, content_hash="a" * 64
            )
        )


def child_that_rejects(connection, log_level: int) -> None:
    """Rechaza todo lo que le llega."""
    while True:
        request = connection.recv()
        if request == ipc.SHUTDOWN:
            return
        connection.send(
            ipc.ReviewResponse(
                job_id=request.job_id, verdict=ipc.INVALID, detail="no es una imagen"
            )
        )


def child_that_never_answers(connection, log_level: int) -> None:
    """Recibe los pedidos y no contesta nunca. Provoca el vencimiento del plazo."""
    while True:
        request = connection.recv()
        if request == ipc.SHUTDOWN:
            return


def child_that_dies_on_first_request(connection, log_level: int) -> None:
    """Se muere apenas le piden algo, sin contestar."""
    request = connection.recv()
    if request != ipc.SHUTDOWN:
        raise SystemExit(1)


def child_that_answers_backwards(connection, log_level: int) -> None:
    """Junta tres pedidos y los contesta al revés, para forzar el desorden."""
    acumulados = []
    while True:
        request = connection.recv()
        if request == ipc.SHUTDOWN:
            return
        acumulados.append(request)
        if len(acumulados) == 3:
            for pendiente in reversed(acumulados):
                connection.send(
                    ipc.ReviewResponse(
                        job_id=pendiente.job_id,
                        verdict=ipc.NEW,
                        content_hash=pendiente.job_id * 4,
                    )
                )
            acumulados = []


# ──────────────────────── base de las pruebas ────────────────────────


class IntakeTestCase(unittest.IsolatedAsyncioTestCase):
    """Base que levanta canales y se asegura de apagarlos al terminar.

    Attributes:
        working_directory: Directorio temporal propio de cada prueba.
    """

    def setUp(self) -> None:
        """Crea el directorio temporal y la lista de canales a cerrar."""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.working_directory = Path(self._temporary_directory.name)
        self._channels: list[IntakeChannel] = []

    def tearDown(self) -> None:
        """Borra el directorio temporal."""
        self._temporary_directory.cleanup()

    async def running_channel(self, child_entry_point) -> IntakeChannel:
        """Abre un canal con el hijo indicado y lo registra para apagarlo después.

        Args:
            child_entry_point: La función que va a correr el proceso hijo.

        Returns:
            El canal ya abierto.
        """
        channel = IntakeChannel(child_entry_point, log_level=logging.CRITICAL)
        channel.start()

        self._channels.append(channel)
        self.addAsyncCleanup(self._close_channels)

        return channel

    async def _close_channels(self) -> None:
        """Apaga todos los canales que abrió la prueba."""
        for channel in self._channels:
            await channel.stop()
        self._channels.clear()

    def a_request(self, job_id: str = "job-1", filename: str = "foto.jpg"):
        """Arma un pedido de revisión sobre un archivo que existe.

        Args:
            job_id: Identificador del trabajo.
            filename: Nombre del archivo a crear en el directorio temporal.

        Returns:
            El pedido listo para enviar.
        """
        stored_path = self.working_directory / filename
        stored_path.write_bytes(b"\xff\xd8\xff\xe0" + filename.encode() * 100)

        return ipc.ReviewRequest(
            job_id=job_id,
            user="augusto",
            operation="anonymize",
            parameters={"mode": "blur"},
            stored_path=stored_path,
        )


# ──────────────────────── el intercambio básico ────────────────────────


class ReviewExchange(IntakeTestCase):
    """Un pedido cruza al hijo y su veredicto vuelve."""

    async def test_a_verdict_crosses_back_from_the_child(self) -> None:
        """Lo que contesta el hijo es lo que recibe quien pidió la revisión."""
        channel = await self.running_channel(child_that_accepts)

        verdict = await channel.review(self.a_request())

        self.assertEqual(verdict.verdict, ipc.NEW)
        self.assertEqual(verdict.job_id, "job-1")
        self.assertEqual(verdict.content_hash, "a" * 64)

    async def test_a_rejection_also_crosses_back(self) -> None:
        """El veredicto negativo viaja igual, con su explicación."""
        channel = await self.running_channel(child_that_rejects)

        verdict = await channel.review(self.a_request())

        self.assertEqual(verdict.verdict, ipc.INVALID)
        self.assertEqual(verdict.detail, "no es una imagen")

    async def test_the_event_loop_keeps_running_while_the_child_works(self) -> None:
        """Otras tareas siguen corriendo mientras se espera al hijo.

        Es la prueba de que la espera no bloquea. Con un `recv()` bloqueante dentro del
        event loop, el contador quedaría en cero y todos los clientes conectados se
        congelarían hasta que el hijo contestara.
        """
        channel = await self.running_channel(child_that_accepts)
        beats = 0

        async def other_work() -> None:
            nonlocal beats
            while True:
                await asyncio.sleep(0.001)
                beats += 1

        heartbeat = asyncio.create_task(other_work())
        await channel.review(self.a_request())
        heartbeat.cancel()

        self.assertGreater(beats, 0)


# ──────────────────────── varias revisiones a la vez ────────────────────────


class ConcurrentReviews(IntakeTestCase):
    """Varias revisiones en vuelo y una sola conexión de vuelta."""

    async def test_each_answer_reaches_the_one_who_asked(self) -> None:
        """Diez revisiones simultáneas: cada respuesta vuelve a su pedido."""
        channel = await self.running_channel(child_that_accepts)

        verdicts = await asyncio.gather(
            *(channel.review(self.a_request(f"job-{n}", f"f{n}.jpg")) for n in range(10))
        )

        self.assertEqual([v.job_id for v in verdicts], [f"job-{n}" for n in range(10)])

    async def test_answers_out_of_order_still_reach_the_right_caller(self) -> None:
        """El orden de llegada no importa: la correlación es por `job_id`.

        El hijo de esta prueba junta tres pedidos y los contesta al revés a propósito. Sin
        el diccionario que asocia cada respuesta con su pedido, cada quien se llevaría la
        respuesta de otro.
        """
        channel = await self.running_channel(child_that_answers_backwards)

        verdicts = await asyncio.gather(
            channel.review(self.a_request("job-a", "a.jpg")),
            channel.review(self.a_request("job-b", "b.jpg")),
            channel.review(self.a_request("job-c", "c.jpg")),
        )

        for asked, verdict in zip(("job-a", "job-b", "job-c"), verdicts):
            self.assertEqual(verdict.job_id, asked)
            self.assertEqual(verdict.content_hash, asked * 4)


# ──────────────────────── cuando algo sale mal ────────────────────────


class WhenThingsGoWrong(IntakeTestCase):
    """Plazo vencido, hijo muerto y recuperación."""

    async def test_a_child_that_never_answers_makes_the_review_expire(self) -> None:
        """Sin el plazo, quien pidió la revisión esperaría para siempre."""
        channel = await self.running_channel(child_that_never_answers)

        with mock.patch.object(intake_channel, "REVIEW_TIMEOUT_SECONDS", 0.3):
            verdict = await channel.review(self.a_request())

        self.assertEqual(verdict.verdict, ipc.UNAVAILABLE)
        self.assertIn("a tiempo", verdict.detail or "")

    async def test_a_review_in_flight_fails_when_the_child_dies(self) -> None:
        """Si el hijo muere en el medio, el que esperaba se entera enseguida.

        No espera a que venza el plazo: el pipe da fin de archivo apenas el hijo cierra su
        extremo, y eso alcanza para darlo por perdido.
        """
        channel = await self.running_channel(child_that_dies_on_first_request)

        verdict = await channel.review(self.a_request())

        self.assertEqual(verdict.verdict, ipc.UNAVAILABLE)
        self.assertIn("murió", verdict.detail or "")

    async def test_the_child_is_relaunched_after_dying(self) -> None:
        """Un hijo muerto se reemplaza, y el pedido siguiente se atiende normal."""
        channel = await self.running_channel(child_that_accepts)
        first_pid = channel._process.pid

        channel._process.kill()
        await asyncio.sleep(0.3)  # que el receptor vea el fin de archivo

        verdict = await channel.review(self.a_request("job-post-mortem"))

        self.assertEqual(verdict.verdict, ipc.NEW)
        self.assertNotEqual(channel._process.pid, first_pid)
        self.assertTrue(channel._process.is_alive())

    async def test_reviewing_after_stopping_does_not_hang(self) -> None:
        """Con el canal cerrado, la revisión falla al instante en vez de colgarse."""
        channel = await self.running_channel(child_that_accepts)
        await channel.stop()

        verdict = await channel.review(self.a_request())

        self.assertEqual(verdict.verdict, ipc.UNAVAILABLE)


# ──────────────────────── el apagado ────────────────────────


class Shutdown(IntakeTestCase):
    """El hijo termina por las suyas cuando se lo piden."""

    async def test_the_child_exits_on_its_own(self) -> None:
        """Sale con código 0: terminó, no lo terminaron."""
        channel = await self.running_channel(child_that_accepts)

        await channel.stop()

        self.assertEqual(channel._process, None)

    async def test_stopping_twice_is_harmless(self) -> None:
        """El apagado se puede pedir dos veces sin que falle."""
        channel = await self.running_channel(child_that_accepts)

        await channel.stop()
        await channel.stop()

    async def test_no_extra_threads_are_created(self) -> None:
        """El canal no levanta hilos: todo ocurre en el event loop.

        Es lo que distingue a esta implementación de la alternativa con `mp.Queue`, que
        además de necesitar un hilo lector levanta uno propio por cada cola.
        """
        import threading

        before = threading.active_count()
        channel = await self.running_channel(child_that_accepts)
        await channel.review(self.a_request())

        self.assertEqual(threading.active_count(), before)


# ──────────────────────── lo que hace el hijo de verdad ────────────────────────


class RealChildWork(IntakeTestCase):
    """Las funciones del proceso de ingreso, llamadas directamente."""

    def test_the_hash_matches_the_content(self) -> None:
        """El hash que calcula es el SHA-256 del archivo, leído de a bloques."""
        content = b"\xff\xd8" + bytes(range(256)) * 800  # más de dos bloques
        image = self.working_directory / "grande.jpg"
        image.write_bytes(content)

        self.assertEqual(process.hash_of(image), hashlib.sha256(content).hexdigest())

    def test_the_same_content_gives_the_same_hash(self) -> None:
        """Dos archivos con distinto nombre e igual contenido dan el mismo hash.

        Es el cimiento de la deduplicación: lo que identifica a una imagen es su
        contenido, no cómo la haya llamado el cliente.
        """
        content = b"\xff\xd8" + b"mismo contenido" * 500
        first = self.working_directory / "foto.jpg"
        second = self.working_directory / "copia.jpg"
        first.write_bytes(content)
        second.write_bytes(content)

        self.assertEqual(process.hash_of(first), process.hash_of(second))

    def test_a_missing_file_is_reported_as_unavailable(self) -> None:
        """Una falla al revisar es problema del servidor, no de la imagen.

        Por eso responde UNAVAILABLE y no INVALID: decirle al cliente que su imagen es
        inválida cuando el error fue nuestro sería echarle la culpa de lo que no hizo.
        """
        request = ipc.ReviewRequest(
            job_id="job-1", user="augusto", operation="clean",
            parameters={}, stored_path=self.working_directory / "no-existe.jpg",
        )

        with self.assertLogs(level="ERROR"):
            response = process.review(request)

        self.assertEqual(response.verdict, ipc.UNAVAILABLE)
        self.assertEqual(response.job_id, "job-1")

    def test_a_readable_image_is_accepted_with_its_hash(self) -> None:
        """Una imagen legible se acepta y vuelve con su hash calculado."""
        image = self.working_directory / "foto.jpg"
        image.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 5000)

        response = process.review(
            ipc.ReviewRequest("job-9", "augusto", "clean", {}, image)
        )

        self.assertEqual(response.verdict, ipc.NEW)
        self.assertEqual(response.content_hash, process.hash_of(image))


if __name__ == "__main__":
    unittest.main()
