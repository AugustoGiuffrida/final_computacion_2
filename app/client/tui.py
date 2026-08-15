"""Interfaz interactiva de pantalla completa, construida con Textual.

Es la otra cara del cliente: hace exactamente lo mismo que el modo directo —los mismos
cuatro pedidos, por la misma `ClientSession`— pero en vez de ejecutar una acción y
terminar, mantiene una única conexión abierta y deja que el usuario trabaje con el teclado.

Por qué Textual y no cualquier otra biblioteca de interfaz de terminal: **corre sobre
asyncio**. La aplicación y los sockets comparten el mismo event loop, de modo que mientras
una imagen de varios MB viaja en bloques de 64 KB la pantalla se sigue repintando y
responde a las teclas, sin usar un solo hilo. Es la demostración más directa de lo que
asyncio hace, y de por qué el `await` de cada bloque importa.

El panel inferior muestra **los mensajes del protocolo a medida que viajan**. No es
decoración: hace visible el framing —qué header sale, cuántos bytes de payload lo
acompañan, qué responde el servidor— que de otro modo solo se puede explicar en palabras.
Se engancha con el callback `on_frame` de la sesión, así que la capa de red sigue sin
saber que esta interfaz existe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from app.client import formatting, session
from app.common import config, messages, protocol

POLL_SECONDS = 1.0 #Cada cuánto se releen los estados de los trabajos que siguen en curso.

MAX_PROTOCOL_LINES = 500 #Cuántos mensajes conserva el panel de protocolo antes de descartar los viejos.


class SubmitScreen(ModalScreen[dict[str, Any] | None]):
    """Formulario para enviar una imagen: la ruta, la operación y sus parámetros.

    Los campos de parámetros se muestran u ocultan según la operación elegida, porque cada
    una acepta los suyos: no tiene sentido pedir la intensidad del difuminado para `clean`.
    La lista sale de `config.OPERATION_PARAMETERS`, que es la misma que usa la línea de
    comandos.

    Devuelve, al cerrarse, un diccionario con la ruta, la operación y los parámetros; o
    None si el usuario canceló.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancelar"),
    ]

    def compose(self) -> ComposeResult:
        """Arma el contenido del formulario.

        Yields:
            Los widgets del formulario, dentro de su recuadro.
        """
        with Vertical(id="submit-dialog"):
            yield Label("Enviar una imagen", id="submit-title")

            yield Label("Archivo", classes="field-label")
            yield Input(placeholder="ruta de la imagen (JPEG o PNG)", id="path")

            yield Label("Operación", classes="field-label")
            yield Select(
                [(name, name) for name in sorted(config.OPERATION_PARAMETERS)],
                value="sanitize",
                allow_blank=False,
                id="operation",
            )

            with Vertical(id="parameters"):
                yield Label("Cómo cubrir las caras", classes="field-label param-label", id="label-mode")
                yield Select(
                    [(mode, mode) for mode in config.ANONYMIZE_MODES],
                    value=config.ANONYMIZE_MODES[0], allow_blank=False, id="mode",
                )
                yield Label("Intensidad", classes="field-label param-label", id="label-strength")
                yield Input(placeholder="por defecto del servidor", type="integer", id="strength")

                yield Label("Formato de salida", classes="field-label param-label", id="label-format")
                yield Select(
                    [(image_format, image_format) for image_format in config.CONVERT_FORMATS],
                    value=config.CONVERT_FORMATS[0], allow_blank=False, id="format",
                )
                yield Label("Calidad (1-95)", classes="field-label param-label", id="label-quality")
                yield Input(placeholder="por defecto del servidor", type="integer", id="quality")

                yield Label("Lado máximo en píxeles", classes="field-label param-label", id="label-max_size")
                yield Input(placeholder="por defecto del servidor", type="integer", id="max_size")

            yield Label("", id="submit-error")

            with Horizontal(id="submit-buttons"):
                yield Button("Enviar", variant="primary", id="confirm")
                yield Button("Cancelar", id="cancel")

    def on_mount(self) -> None:
        """Ajusta los campos visibles a la operación inicial y pone el foco en la ruta."""
        self.show_parameters_of("sanitize")
        self.query_one("#path", Input).focus()

    def show_parameters_of(self, operation: str) -> None:
        """Muestra solo los campos de parámetros que acepta la operación elegida."""
        accepted = config.OPERATION_PARAMETERS[operation]

        for parameter_name in config.ALL_OPERATION_PARAMETERS:
            is_accepted = parameter_name in accepted
            self.query_one(f"#{parameter_name}").display = is_accepted
            self.query_one(f"#label-{parameter_name}").display = is_accepted

        self.query_one("#parameters").display = bool(accepted)

    @on(Select.Changed, "#operation")
    def operation_changed(self, event: Select.Changed) -> None:
        """Reacciona al cambio de operación ajustando los campos visibles."""
        self.show_parameters_of(str(event.value))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        """Cierra el formulario sin enviar nada."""
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        """Valida lo cargado y cierra el formulario devolviendo el pedido.

        La validación es la misma del modo directo (`session.validate_image_file`): que el
        archivo exista, que la extensión esté soportada y que no exceda el máximo. Si algo
        falla se muestra dentro del formulario y este no se cierra.
        """
        image_path = Path(self.query_one("#path", Input).value).expanduser()
        operation = str(self.query_one("#operation", Select).value)

        try:
            session.validate_image_file(image_path)
        except session.LocalValidationError as error:
            self.query_one("#submit-error", Label).update(f"[red]{error}[/red]")
            return

        self.dismiss({
            "path": image_path,
            "operation": operation,
            "parameters": self.collect_parameters(operation),
        })

    def collect_parameters(self, operation: str) -> dict[str, Any]:
        """Junta los parámetros cargados que corresponden a la operación.

        Los campos vacíos se omiten, para que el servidor aplique su valor por defecto.
        """
        parameters: dict[str, Any] = {}

        for parameter_name in config.OPERATION_PARAMETERS[operation]:
            widget = self.query_one(f"#{parameter_name}")

            if isinstance(widget, Select):
                parameters[parameter_name] = str(widget.value)
            elif isinstance(widget, Input) and widget.value.strip():
                parameters[parameter_name] = int(widget.value)

        return parameters


class ImageClientApp(App[None]):
    """La aplicación: una conexión abierta, la lista de trabajos y el panel de protocolo.

    Attributes:
        host: Dirección del servidor.
        port: Puerto del servidor.
        user: Usuario con el que se declaran los trabajos.
        jobs: Trabajos conocidos, por identificador. Se siembra con el historial al
            arrancar y se actualiza con cada envío y cada consulta.
    """

    TITLE = "Sanitización de imágenes"

    CSS = """
    Screen { background: $surface; }

    #main { height: 1fr; }

    #left { width: 3fr; border-right: solid $primary-darken-2; }
    #right { width: 2fr; padding: 0 1; }

    .panel-title {
        background: $primary-darken-3;
        color: $text;
        text-style: bold;
        padding: 0 1;
        width: 100%;
    }

    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: $accent; }

    #detail { padding: 1 0; }

    #activity { height: auto; padding: 0 1; display: none; }
    #activity-label { width: 30; }
    #activity-bar { width: 1fr; }

    #protocol {
        height: 10;
        border-top: solid $primary-darken-2;
        background: $surface-darken-1;
        padding: 0 1;
    }

    /* ── formulario de envío ── */

    SubmitScreen { align: center middle; }

    #submit-dialog {
        width: 64;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $panel;
        border: thick $primary;
    }

    #submit-title { text-style: bold; width: 100%; padding-bottom: 1; }

    .field-label { color: $text-muted; padding-top: 1; }

    #submit-error { width: 100%; padding-top: 1; }

    #submit-buttons { height: auto; padding-top: 1; align-horizontal: right; }
    #submit-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("n", "new_job", "Enviar imagen"),
        Binding("d", "download", "Descargar"),
        Binding("r", "refresh_history", "Refrescar"),
        Binding("p", "toggle_protocol", "Panel de protocolo"),
        Binding("q", "quit", "Salir"),
    ]

    def __init__(self, host: str, port: int, user: str) -> None:
        """Prepara la aplicación sin conectarse todavía."""
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.jobs: dict[str, dict[str, Any]] = {}
        self.session: session.ClientSession | None = None

        # El historial no trae los datos que produjo cada operación —son del `status`—
        # así que se piden al seleccionar el trabajo. Esto recuerda cuáles ya se pidieron,
        # para no volver a consultar lo mismo cada vez que el cursor pasa por la fila.
        self._detail_requested: set[str] = set()

    # ──────────────────────────── armado de la pantalla ────────────────────────────

    def compose(self) -> ComposeResult:
        """Arma la pantalla principal.

        Yields:
            Los widgets de la aplicación, en el orden en que se apilan.
        """
        yield Header(show_clock=True)

        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Trabajos", classes="panel-title")
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="right"):
                yield Static("Detalle", classes="panel-title")
                yield Static(id="detail")

        with Horizontal(id="activity"):
            yield Label("", id="activity-label")
            yield ProgressBar(id="activity-bar", show_eta=False)

        yield RichLog(id="protocol", markup=True, wrap=False, max_lines=MAX_PROTOCOL_LINES)
        yield Footer()

    def on_mount(self) -> None:
        """Prepara la tabla, se conecta al servidor y arranca la consulta periódica."""
        self.sub_title = f"{self.user} @ {self.host}:{self.port}"

        table = self.query_one("#jobs", DataTable)
        table.add_columns("Estado", "Operación", "Imagen", "Trabajo")

        self.query_one("#detail", Static).update(
            Text("Sin trabajos todavía. Apretá 'n' para enviar una imagen.", style="dim")
        )

        self.connect_and_load()
        self.set_interval(POLL_SECONDS, self.refresh_pending_jobs)

    # ──────────────────────────── conexión y datos ────────────────────────────

    @work(exclusive=True, group="connection")
    async def connect_and_load(self) -> None:
        """Abre la conexión con el servidor y carga el historial del usuario.

        Es lo primero que ocurre al arrancar. Si el servidor no está, se avisa en el panel
        de detalle en vez de terminar la aplicación: el usuario puede volver a intentar
        con 'r'.
        """
        self.session = session.ClientSession(
            self.host, self.port, self.user, on_frame=self.log_frame
        )

        try:
            await self.session.connect()
        except OSError as error:
            self.session = None
            self.show_detail_error("No se pudo conectar", str(error))
            return

        self.log_line(f"[green]conectado a {self.host}:{self.port}[/green]")
        await self.load_history()

    async def load_history(self) -> None:
        """Trae el historial del usuario y reconstruye la tabla con él."""
        if self.session is None:
            return

        try:
            jobs = await self.session.history(limit=50)
        except (messages.ServerError, OSError, protocol.ProtocolError) as error:
            self.show_detail_error("No se pudo leer el historial", str(error))
            return

        for job in jobs:
            job_id = job.get("job_id", "")
            if job_id:
                self.jobs[job_id] = {**self.jobs.get(job_id, {}), **job}

        self.rebuild_table()

    @work(exclusive=True, group="poll")
    async def refresh_pending_jobs(self) -> None:
        """Vuelve a consultar el estado de los trabajos que todavía no terminaron.

        Es el equivalente del `--wait` del modo directo, pero para todos los trabajos a la
        vez y sin bloquear nada: cada consulta es un `await` y entre una y otra la interfaz
        sigue respondiendo.

        El trabajo es exclusivo por grupo, así que si una vuelta tarda más que el intervalo
        no se acumulan consultas encima.
        """
        if self.session is None:
            return

        pending = [
            job_id for job_id, job in self.jobs.items()
            if job.get("status") not in messages.TERMINAL_STATUSES
        ]
        if not pending:
            return

        for job_id in pending:
            try:
                response = await self.session.status(job_id)
            except (messages.ServerError, OSError, protocol.ProtocolError):
                continue  # Un trabajo que no se pudo consultar se reintenta en la vuelta siguiente.

            self.jobs[job_id] = {
                **self.jobs[job_id],
                "status": response.get("status", ""),
                "has_output": response.get("has_output"),
                "result": response.get("result"),
                "error": response.get("error"),
            }

        self.rebuild_table()
        self.show_selected_job()

    # ──────────────────────────────── acciones ────────────────────────────────

    @work
    async def action_new_job(self) -> None:
        """Abre el formulario de envío y, si se confirma, manda la imagen."""
        if self.session is None:
            self.notify("No hay conexión con el servidor.", severity="error")
            return

        request = await self.push_screen_wait(SubmitScreen())
        if request is None:
            return

        image_path: Path = request["path"]
        total_bytes = image_path.stat().st_size
        self.start_activity(f"Enviando {image_path.name}", total_bytes)

        try:
            response = await self.session.submit(
                image_path, request["operation"], request["parameters"],
                on_progress=self.update_activity,
            )
        except (messages.ServerError, OSError, protocol.ProtocolError) as error:
            self.notify(str(error), title="No se pudo enviar", severity="error")
            return
        finally:
            self.stop_activity()

        job_id = response.get("job_id", "")
        self.jobs[job_id] = {
            "job_id": job_id,
            "op": request["operation"],
            "filename": image_path.name,
            "status": response.get("status", messages.QUEUED),
        }
        self.rebuild_table()

        if response.get("deduplicated"):
            self.notify(
                "Esta imagen ya había sido procesada con esta misma operación: "
                "el resultado anterior ya está listo.",
                title="Trabajo reutilizado",
            )
        else:
            self.notify(f"Trabajo {job_id[:8]}… encolado.", title="Imagen aceptada")

    @work
    async def action_download(self) -> None:
        """Descarga el resultado del trabajo seleccionado.

        El archivo se guarda en el directorio actual con el nombre que sugiere el servidor.
        """
        job = self.selected_job()
        if job is None or self.session is None:
            return

        if job.get("status") != messages.DONE:
            self.notify("El trabajo todavía no terminó.", severity="warning")
            return
        if job.get("has_output") is False:
            self.notify(
                "Esta operación no genera archivo: su resultado es el informe del panel.",
                severity="warning",
            )
            return

        self.start_activity("Descargando el resultado", None)

        try:
            written_path, _ = await self.session.download(
                job["job_id"], on_progress=self.update_activity
            )
        except (messages.ServerError, OSError, protocol.ProtocolError) as error:
            self.notify(str(error), title="No se pudo descargar", severity="error")
            return
        finally:
            self.stop_activity()

        self.notify(f"Guardado en {written_path}", title="Descarga completa")

    @work(exclusive=True, group="connection")
    async def action_refresh_history(self) -> None:
        """Vuelve a pedir el historial, reconectando si hiciera falta."""
        if self.session is None or not self.session.is_connected:
            self.connect_and_load()
            return
        await self.load_history()

    def action_toggle_protocol(self) -> None:
        """Muestra u oculta el panel que registra los mensajes del protocolo."""
        panel = self.query_one("#protocol", RichLog)
        panel.display = not panel.display

    # ──────────────────────────── tabla y detalle ────────────────────────────

    def rebuild_table(self) -> None:
        """Vuelve a dibujar la tabla de trabajos conservando la fila seleccionada."""
        table = self.query_one("#jobs", DataTable)
        selected_row = table.cursor_row

        table.clear()
        for job in self.jobs.values():
            status = job.get("status", "")
            table.add_row(
                Text(
                    f"{formatting.status_icon(status)} {status}",
                    style=formatting.status_style(status),
                ),
                job.get("op", "—"),
                job.get("filename") or "—",
                job.get("job_id", "—"),
                key=job.get("job_id"),
            )

        if table.row_count:
            table.move_cursor(row=min(selected_row, table.row_count - 1))

    def selected_job(self) -> dict[str, Any] | None:
        """Devuelve el trabajo que está seleccionado en la tabla."""
        table = self.query_one("#jobs", DataTable)
        if not table.row_count:
            return None

        job_id = str(table.get_row_at(table.cursor_row)[3])
        return self.jobs.get(job_id)

    @on(DataTable.RowHighlighted)
    def row_highlighted(self) -> None:
        """Actualiza el panel de detalle cuando se mueve la selección."""
        self.show_selected_job()

    def show_selected_job(self) -> None:
        """Muestra en el panel derecho todo lo que se sabe del trabajo seleccionado."""
        job = self.selected_job()
        if job is None:
            return

        self.query_one("#detail", Static).update(render_job_detail(job))

        job_id = job.get("job_id", "")
        if job_id and "result" not in job and job_id not in self._detail_requested:
            self._detail_requested.add(job_id)
            self.fetch_job_detail(job_id)

    @work(exclusive=True, group="detail")
    async def fetch_job_detail(self, job_id: str) -> None:
        """Consulta el estado de un trabajo para completar los datos que le faltan.

        Los trabajos que llegan por el historial traen su identidad y su estado, pero no
        los datos que produjo la operación —cuántas caras, qué metadatos, el informe de
        `inspect`—, que viajan en la respuesta de `status`. Se piden al seleccionar el
        trabajo y no al cargar el historial, para no disparar cincuenta consultas de golpe
        por datos que quizás nadie mire.
        """
        if self.session is None:
            return

        try:
            response = await self.session.status(job_id)
        except (messages.ServerError, OSError, protocol.ProtocolError):
            self._detail_requested.discard(job_id)  # Que se pueda reintentar más tarde.
            return

        self.jobs[job_id] = {
            **self.jobs.get(job_id, {}),
            "status": response.get("status", ""),
            "has_output": response.get("has_output"),
            "result": response.get("result") or {},
            "error": response.get("error"),
        }

        selected = self.selected_job()
        if selected is not None and selected.get("job_id") == job_id:
            self.query_one("#detail", Static).update(render_job_detail(self.jobs[job_id]))

    def show_detail_error(self, title: str, detail: str) -> None:
        """Muestra un problema en el panel de detalle.

        Se usa para los fallos que dejan la aplicación sin datos —no poder conectarse, no
        poder leer el historial— donde una notificación pasajera no alcanzaría.
        """
        self.query_one("#detail", Static).update(
            Text.assemble(
                (f"{title}\n", "bold red"),
                (f"{detail}\n\n", ""),
                ("Apretá 'r' para volver a intentar.", "dim"),
            )
        )
        self.log_line(f"[red]{title}: {detail}[/red]")

    # ────────────────────── barra de actividad y protocolo ──────────────────────

    def start_activity(self, description: str, total: int | None) -> None:
        """Muestra la barra de progreso de una transferencia."""
        self.query_one("#activity").display = True
        self.query_one("#activity-label", Label).update(description)
        self.query_one("#activity-bar", ProgressBar).update(total=total, progress=0)

    def update_activity(self, transferred: int, total: int) -> None:
        """Actualiza la barra con el avance de la transferencia en curso.

        Es el callback que recibe `ClientSession`. Se llama después de cada bloque, dentro
        del mismo event loop que dibuja la pantalla: por eso la barra se mueve mientras la
        imagen viaja.
        """
        self.query_one("#activity-bar", ProgressBar).update(total=total, progress=transferred)

    def stop_activity(self) -> None:
        """Oculta la barra de progreso al terminar la transferencia."""
        self.query_one("#activity").display = False

    def log_frame(self, direction: str, header: dict[str, Any]) -> None:
        """Registra en el panel un mensaje del protocolo que salió o llegó.

        Es el enganche que hace visible el framing: se ve el tipo de cada mensaje, sus
        campos relevantes y cuántos bytes de payload lo acompañan.
        """
        is_outgoing = direction == session.SENT
        arrow, style = ("→", "cyan") if is_outgoing else ("←", "green")

        message_type = str(header.get(messages.TYPE_FIELD, "?"))
        if message_type == messages.ERROR:
            style = "red"

        payload_size = header.get(protocol.PAYLOAD_SIZE_FIELD, 0)
        payload_note = (
            f"  [dim]payload {formatting.format_size(payload_size)}[/dim]"
            if payload_size else ""
        )

        self.log_line(
            f"[{style}]{arrow} {message_type:<9}[/{style}]"
            f"{summarize_header(header)}{payload_note}"
        )

    def log_line(self, line: str) -> None:
        """Escribe un renglón en el panel de protocolo."""
        self.query_one("#protocol", RichLog).write(line)

    async def on_unmount(self) -> None:
        """Cierra la conexión al salir de la aplicación."""
        if self.session is not None:
            await self.session.close()


# ─────────────────────────── funciones de presentación ───────────────────────────


def summarize_header(header: dict[str, Any]) -> str:
    """Arma un resumen de una línea con los campos interesantes de un header.

    Se eligen a mano los campos que importan para seguir el diálogo, en vez de volcar el
    JSON entero: el panel tiene que poder leerse de un vistazo mientras las cosas pasan.
    """
    interesting = ("op", "job_id", "status", "code", "limit", "filename")
    parts: list[str] = []

    for field in interesting:
        value = header.get(field)
        if value is None:
            continue
        if field == "job_id":
            value = f"{str(value)[:8]}…"
        parts.append(f"[dim]{field}=[/dim]{value}")

    return "  " + "  ".join(parts) if parts else ""


def render_job_detail(job: dict[str, Any]) -> Group:
    """Arma el detalle de un trabajo para el panel derecho.

    El identificador va en su propio renglón y no dentro de la grilla: mide 36 caracteres
    y, compartiendo el ancho con la columna de etiquetas, se cortaría — justo el dato que
    el usuario necesita entero para poder copiarlo.
    """
    status = job.get("status", "")

    detail = Table.grid(padding=(0, 1))
    detail.add_column(style="dim", justify="right")
    detail.add_column()

    detail.add_row("Operación", job.get("op", "—"))
    detail.add_row("Imagen", job.get("filename") or "—")
    detail.add_row("Estado", formatting.status_markup(status))

    if job.get("created_at"):
        detail.add_row("Enviado", formatting.format_timestamp(job.get("created_at")))

    if status == messages.FAILED:
        detail.add_row("Motivo", f"[red]{job.get('error', 'sin motivo informado')}[/red]")

    if status == messages.DONE:
        detail.add_row(
            "Archivo",
            "[green]listo para descargar (d)[/green]" if job.get("has_output")
            else "[dim]esta operación no genera archivo[/dim]",
        )

    result = job.get("result") or {}
    if result:
        detail.add_row("", "")
        detail.add_row("", "[bold]Resultado[/bold]")
        for field, value in result.items():
            formatted = formatting.format_result_value(field, value)
            if formatting.is_privacy_sensitive(field, value):
                formatted = f"[bold yellow]{formatted}[/bold yellow]"
            detail.add_row(formatting.result_label(field), formatted)

    return Group(
        Text(job.get("job_id", "—"), style="bold", no_wrap=False),
        Text(""),
        detail,
    )
