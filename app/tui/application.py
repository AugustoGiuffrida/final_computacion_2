"""La pantalla del cliente visual: tres paneles sobre una sola conexión.

No hay lógica de red acá. Todo lo que se manda o se consulta pasa por `ClientSession`, la
misma que usa el cliente de terminal y **sin ningún cambio**: si esta interfaz necesitara
tocarla, sería señal de que la separación entre red y presentación no era real.

De la misma forma, los valores se muestran con `client.formatting`, que ya existía para la
terminal. Lo único propio de este módulo es cómo se acomoda la pantalla.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    Select,
)

from app.client import formatting, session
from app.client.cli import positive_integer, quality_level
from app.common import config, messages

# Cada cuánto se vuelve a pedir el historial. Es lo que hace que un trabajo en curso se vea
# avanzar: el estado lo informa el servidor, el cliente no adivina nada.
REFRESH_INTERVAL_SECONDS = 1.0

# Las extensiones que el servidor acepta, para no mostrar archivos que va a rechazar.
VISIBLE_SUFFIXES = frozenset(config.SUPPORTED_EXTENSIONS)

# Cómo se pide cada parámetro y con qué se valida. Las opciones cerradas se eligen de una
# lista; las numéricas se escriben y se validan con los mismos validadores que usa el
# cliente de terminal, para que las dos interfaces rechacen exactamente lo mismo.
PARAMETER_CHOICES = {
    "mode": config.ANONYMIZE_MODES,
    "format": config.CONVERT_FORMATS,
}

PARAMETER_VALIDATORS = {
    "strength": positive_integer,
    "quality": quality_level,
    "max_size": positive_integer,
}

PARAMETER_LABELS = {
    "mode": "Cómo cubrir las caras",
    "strength": "Intensidad (1-100)",
    "format": "Formato de salida",
    "quality": "Calidad (1-95)",
    "max_size": "Lado máximo en píxeles",
}


class ImageTree(DirectoryTree):
    """Un árbol de directorios que muestra solo imágenes enviables.

    Filtrar acá evita el camino más frustrante de la interfaz: elegir un archivo, mandarlo
    y recibir un rechazo que se podía anticipar.
    """

    def filter_paths(self, paths):
        """Deja pasar los directorios y las imágenes de formato soportado.

        Args:
            paths: Lo que hay en el directorio que se está por mostrar.

        Returns:
            Solo los directorios y los archivos con extensión aceptada.
        """
        return [
            path for path in paths
            if path.is_dir() or path.suffix.lower() in VISIBLE_SUFFIXES
        ]


class ImagesApp(App):
    """Cliente visual del servicio de anonimización.

    Attributes:
        user: Con qué nombre se presenta al servidor.
        host: Dirección del servidor.
        port: Puerto del servidor.
        directory: Raíz del árbol de imágenes.
        selected: La imagen elegida, o None si todavía no se eligió ninguna.
    """

    CSS_PATH = "application.tcss"
    TITLE = "Anonimización de imágenes"

    BINDINGS = [
        ("q", "quit", "Salir"),
        ("r", "refresh_jobs", "Refrescar"),
    ]

    def __init__(self, user: str, host: str, port: int, directory: Path) -> None:
        """Guarda la configuración sin conectarse todavía.

        La conexión se abre en `on_mount`, cuando ya hay pantalla donde informar un fallo.
        """
        super().__init__()
        self.user = user
        self.host = host
        self.port = port
        self.directory = directory
        self.selected: Path | None = None
        self.operation: str | None = None
        self._session: session.ClientSession | None = None

    def compose(self) -> ComposeResult:
        """Arma la pantalla: imágenes a la izquierda, operación al medio, trabajos a la
        derecha.

        Returns:
            Los widgets de la pantalla, en orden.
        """
        yield Header()

        with Horizontal():
            with Vertical(id="images"):
                yield Label("Imágenes", classes="title")
                yield ImageTree(self.directory, id="tree")

            with Vertical(id="operation"):
                yield Label("Operación", classes="title")
                # Las operaciones salen del catálogo: agregar una al servidor la hace
                # aparecer acá sin tocar esta pantalla.
                yield RadioSet(
                    *(RadioButton(name) for name in sorted(config.OPERATION_PARAMETERS)),
                    id="operations",
                )
                yield Label("", id="chosen")
                # Se llena y se vacía según la operación elegida: cada una tiene los suyos.
                yield Vertical(id="parameters")
                yield ProgressBar(id="upload", show_eta=False)
                yield Button("Enviar", id="send", variant="primary")

            with Vertical(id="jobs"):
                yield Label("Trabajos", classes="title")
                yield DataTable(id="table", cursor_type="row")

        yield Footer()

    async def on_mount(self) -> None:
        """Abre la conexión, prepara la tabla y arranca el refresco periódico."""
        table = self.query_one("#table", DataTable)
        table.add_columns("Estado", "Operación", "Archivo", "Enviado")
        self.query_one("#upload", ProgressBar).display = False

        self._session = session.ClientSession(self.host, self.port, self.user)
        try:
            await self._session.connect()
        except OSError as failure:
            self.notify(f"No se pudo conectar: {failure}", severity="error", timeout=10)
            return

        self.sub_title = f"{self.user} en {self.host}:{self.port}"
        await self.action_refresh_jobs()
        self.set_interval(REFRESH_INTERVAL_SECONDS, self.action_refresh_jobs)

    async def on_unmount(self) -> None:
        """Cierra la conexión al salir."""
        if self._session is not None:
            await self._session.close()

    def on_directory_tree_file_selected(
        self, event: DirectoryTree.FileSelected
    ) -> None:
        """Anota la imagen elegida y la muestra.

        Args:
            event: Lo que emite el árbol al elegir un archivo.
        """
        self.selected = event.path
        self.query_one("#chosen", Label).update(
            f"Elegida: [bold]{event.path.name}[/bold] "
            f"({formatting.format_size(event.path.stat().st_size)})"
        )

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Cambia la operación elegida y rearma los parámetros que le corresponden.

        Args:
            event: Lo que emite el grupo de opciones al cambiar la selección.
        """
        self.operation = str(event.pressed.label)
        await self.rebuild_parameters()

    async def rebuild_parameters(self) -> None:
        """Deja en pantalla solo los parámetros de la operación elegida.

        Cuáles son lo dice `config.OPERATION_PARAMETERS`, así que una operación nueva en el
        servidor aparece acá con sus campos sin tocar esta pantalla. `clean` e `inspect` no
        tienen ninguno y el área queda vacía, que es la respuesta correcta.
        """
        area = self.query_one("#parameters", Vertical)
        await area.remove_children()

        if self.operation is None:
            return

        for name in config.OPERATION_PARAMETERS[self.operation]:
            await area.mount(Label(PARAMETER_LABELS.get(name, name), classes="field"))
            if name in PARAMETER_CHOICES:
                await area.mount(
                    Select(
                        [(opcion, opcion) for opcion in PARAMETER_CHOICES[name]],
                        id=f"param-{name}",
                        allow_blank=True,
                        prompt="(el que use el servidor)",
                    )
                )
            else:
                await area.mount(
                    Input(placeholder="(el que use el servidor)", id=f"param-{name}")
                )

    def collect_parameters(self) -> dict[str, object]:
        """Junta lo que se cargó en los campos, validándolo.

        Solo se incluye lo que se completó: un campo en blanco significa "el valor por
        defecto del servidor", igual que omitir la bandera en el cliente de terminal.

        Returns:
            Los parámetros a enviar.

        Raises:
            ValueError: Si algún valor numérico no es válido, con el motivo adentro.
        """
        parameters: dict[str, object] = {}

        for name in config.OPERATION_PARAMETERS[self.operation]:
            campo = self.query_one(f"#param-{name}")
            crudo = campo.value

            if crudo in (None, "", Select.BLANK):
                continue

            validador = PARAMETER_VALIDATORS.get(name)
            if validador is None:
                parameters[name] = crudo
                continue

            try:
                parameters[name] = validador(str(crudo))
            except Exception as falla:
                raise ValueError(f"{PARAMETER_LABELS.get(name, name)}: {falla}") from None

        return parameters

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Envía la imagen elegida cuando se aprieta el botón.

        Args:
            event: Lo que emite el botón.
        """
        if event.button.id == "send":
            await self.send_image()

    async def send_image(self) -> None:
        """Manda la imagen al servidor mostrando el avance de la transferencia.

        Todo lo que se comprueba antes de conectar es lo mismo que comprueba el cliente de
        terminal, y con las mismas funciones: acá no hay una segunda versión de las reglas.
        """
        if self.selected is None:
            self.notify("Elegí una imagen del árbol", severity="warning")
            return
        if self.operation is None:
            self.notify("Elegí una operación", severity="warning")
            return
        if self._session is None or not self._session.is_connected:
            self.notify("No hay conexión con el servidor", severity="error")
            return

        try:
            session.validate_image_file(self.selected)
            parameters = self.collect_parameters()
        except (session.LocalValidationError, ValueError) as falla:
            self.notify(str(falla), severity="error", timeout=8)
            return

        barra = self.query_one("#upload", ProgressBar)
        barra.display = True
        barra.update(total=self.selected.stat().st_size, progress=0)

        def avance(enviados: int, _total: int) -> None:
            barra.update(progress=enviados)

        boton = self.query_one("#send", Button)
        boton.disabled = True
        try:
            respuesta = await self._session.submit(
                self.selected, self.operation, parameters, on_progress=avance
            )
        except (messages.ServerError, OSError) as falla:
            self.notify(f"El servidor rechazó el envío: {falla}", severity="error", timeout=10)
            return
        finally:
            boton.disabled = False
            barra.display = False

        if respuesta.get("deduplicated"):
            self.notify("Ya habías procesado esta imagen: se reutiliza el resultado anterior")
        else:
            self.notify(f"Enviada: {self.selected.name} → {self.operation}")

        await self.action_refresh_jobs()

    async def action_refresh_jobs(self) -> None:
        """Vuelve a pedir el historial y repinta la tabla.

        Un trabajo en curso se ve avanzar porque el servidor informa su estado actualizado
        en cada consulta, no porque esta pantalla lo suponga.
        """
        if self._session is None or not self._session.is_connected:
            return

        try:
            jobs = await self._session.history(config.DEFAULT_HISTORY_LIMIT)
        except Exception as failure:
            self.notify(f"No se pudo leer el historial: {failure}", severity="warning")
            return

        table = self.query_one("#table", DataTable)
        table.clear()
        for job in jobs:
            table.add_row(
                formatting.status_markup(job.get("status", "")),
                job.get("op", ""),
                job.get("filename", ""),
                formatting.format_timestamp(job.get("created_at")),
            )
