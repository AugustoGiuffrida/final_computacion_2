"""Pruebas de la interfaz interactiva.

Textual permite correr una aplicación *sin terminal* y manejarla desde el código con un
`Pilot`, que aprieta teclas y hace clics como lo haría una persona. Eso vuelve verificable
algo que normalmente solo se puede mirar: que la interfaz carga, que el formulario valida,
que un envío llega al servidor.

Del otro lado contesta el servidor de mentira de `tools/fake_server.py`, el mismo que se
usa a mano para probar el cliente. Cuando el servidor real exista, estas pruebas van a
apuntar a él y ese archivo se borra.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, RichLog

from app.client.tui import ImageClientApp, SubmitScreen
from app.common import messages
from tools import fake_server


@pytest.fixture
async def server_port(tmp_path: Path) -> AsyncIterator[int]:
    """Levanta el servidor de mentira en un puerto libre y lo cierra al terminar.

    Args:
        tmp_path: Directorio temporal de la prueba, donde el servidor guarda lo que recibe.

    Yields:
        El puerto donde quedó escuchando.
    """
    simulator = fake_server.FakeServer(processing_seconds=0.1, storage_dir=tmp_path)
    server = await asyncio.start_server(simulator.handle_client, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])

    yield port

    server.close()
    await server.wait_closed()


@pytest.fixture
def image(tmp_path: Path) -> Path:
    """Crea un archivo con extensión de imagen para usar en los envíos.

    El servidor de mentira no decodifica nada, así que el contenido da igual: lo que se
    ejercita es el camino del cliente, no el procesamiento.

    Args:
        tmp_path: Directorio temporal de la prueba.

    Returns:
        La ruta del archivo creado.
    """
    path = tmp_path / "foto.jpg"
    path.write_bytes(bytes(range(256)) * 400)
    return path


@pytest.mark.asyncio
async def test_the_interface_connects_and_shows_an_empty_list(server_port: int) -> None:
    """Al arrancar, la interfaz se conecta y queda con la tabla vacía y sin errores."""
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)

        assert app.session is not None
        assert app.session.is_connected
        assert app.query_one("#jobs", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_sending_an_image_from_the_form_creates_a_job(
    server_port: int, image: Path
) -> None:
    """El formulario envía la imagen y el trabajo aparece en la tabla."""
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)

        assert isinstance(app.screen, SubmitScreen)

        app.screen.query_one("#path", Input).value = str(image)
        await pilot.click("#confirm")
        await pilot.pause(0.5)

        assert len(app.jobs) == 1

        job = next(iter(app.jobs.values()))
        assert job["op"] == "sanitize"
        assert job["filename"] == "foto.jpg"
        assert app.query_one("#jobs", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_a_job_reaches_done_on_its_own(server_port: int, image: Path) -> None:
    """La consulta periódica lleva el trabajo hasta DONE sin que el usuario haga nada.

    Es el equivalente del `--wait` del modo directo: mientras el estado no sea terminal,
    la interfaz vuelve a preguntar, y entre consulta y consulta sigue respondiendo.
    """
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)

        app.screen.query_one("#path", Input).value = str(image)
        await pilot.click("#confirm")
        await pilot.pause(0.5)

        job_id = next(iter(app.jobs))

        # El servidor de mentira tarda dos segundos en cola más una décima procesando.
        for _ in range(40):
            await pilot.pause(0.1)
            if app.jobs[job_id].get("status") == messages.DONE:
                break

        assert app.jobs[job_id]["status"] == messages.DONE
        assert app.jobs[job_id]["result"]  # llegaron los datos de la operación


@pytest.mark.asyncio
async def test_downloading_a_finished_job_writes_the_file(
    server_port: int, image: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el trabajo terminado, la tecla 'd' baja el archivo al directorio actual."""
    monkeypatch.chdir(tmp_path)
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)

        app.screen.query_one("#path", Input).value = str(image)
        await pilot.click("#confirm")
        await pilot.pause(0.5)

        job_id = next(iter(app.jobs))
        for _ in range(40):
            await pilot.pause(0.1)
            if app.jobs[job_id].get("status") == messages.DONE:
                break

        await pilot.press("d")
        await pilot.pause(0.5)

    downloaded = list(tmp_path.glob("foto_sanitize.*"))
    assert downloaded, "el archivo descargado no aparece en el directorio actual"
    assert downloaded[0].stat().st_size == image.stat().st_size


@pytest.mark.asyncio
async def test_the_form_rejects_a_file_that_does_not_exist(server_port: int) -> None:
    """Una ruta inválida se informa dentro del formulario, que no se cierra."""
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)

        app.screen.query_one("#path", Input).value = "/no/existe/foto.jpg"
        await pilot.click("#confirm")
        await pilot.pause(0.2)

        assert isinstance(app.screen, SubmitScreen)  # sigue abierto
        assert app.jobs == {}


@pytest.mark.asyncio
async def test_the_form_only_shows_the_parameters_of_the_chosen_operation(
    server_port: int,
) -> None:
    """Los campos de parámetros siguen a la operación elegida."""
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.2)

        form = app.screen
        assert isinstance(form, SubmitScreen)

        # 'sanitize' acepta mode, strength, quality y max_size, pero no format.
        assert form.query_one("#mode").display is True
        assert form.query_one("#format").display is False

        # 'clean' no acepta ninguno.
        form.show_parameters_of("clean")
        assert form.query_one("#mode").display is False
        assert form.query_one("#parameters").display is False

        # 'convert' acepta format y quality.
        form.show_parameters_of("convert")
        assert form.query_one("#format").display is True
        assert form.query_one("#strength").display is False


@pytest.mark.asyncio
async def test_the_protocol_panel_records_the_messages(server_port: int) -> None:
    """Cada mensaje que va y viene queda registrado en el panel de protocolo."""
    app = ImageClientApp("127.0.0.1", server_port, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.4)

        panel = app.query_one("#protocol", RichLog)

        # La conexión, el pedido de historial y su respuesta.
        assert len(panel.lines) >= 3

        # Se puede ocultar y volver a mostrar.
        await pilot.press("p")
        assert panel.display is False
        await pilot.press("p")
        assert panel.display is True


@pytest.mark.asyncio
async def test_the_interface_survives_a_server_that_is_not_there() -> None:
    """Si no hay servidor, se avisa y la aplicación sigue en pie en vez de caerse."""
    # Puerto donde con altísima probabilidad no escucha nadie.
    app = ImageClientApp("127.0.0.1", 9, "augusto")

    async with app.run_test() as pilot:
        await pilot.pause(0.4)

        assert app.session is None
        assert app.is_running
