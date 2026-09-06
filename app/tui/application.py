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
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Label,
    RadioButton,
    RadioSet,
)

from app.client import formatting, session
from app.common import config

# Cada cuánto se vuelve a pedir el historial. Es lo que hace que un trabajo en curso se vea
# avanzar: el estado lo informa el servidor, el cliente no adivina nada.
REFRESH_INTERVAL_SECONDS = 1.0

# Las extensiones que el servidor acepta, para no mostrar archivos que va a rechazar.
VISIBLE_SUFFIXES = frozenset(config.SUPPORTED_EXTENSIONS)


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

            with Vertical(id="jobs"):
                yield Label("Trabajos", classes="title")
                yield DataTable(id="table", cursor_type="row")

        yield Footer()

    async def on_mount(self) -> None:
        """Abre la conexión, prepara la tabla y arranca el refresco periódico."""
        table = self.query_one("#table", DataTable)
        table.add_columns("Estado", "Operación", "Archivo", "Enviado")

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
