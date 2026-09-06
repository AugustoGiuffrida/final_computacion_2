"""La pantalla del cliente visual: tres paneles sobre una sola conexión.

No hay lógica de red acá. Todo lo que se manda o se consulta pasa por `ClientSession`, la
misma que usa el cliente de terminal y **sin ningún cambio**: si esta interfaz necesitara
tocarla, sería señal de que la separación entre red y presentación no era real.

De la misma forma, los valores se muestran con `client.formatting`, que ya existía para la
terminal. Lo único propio de este módulo es cómo se acomoda la pantalla.
"""

from __future__ import annotations

import os
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
    Static,
)

from app.client import formatting, session
from app.client.cli import positive_integer, quality_level
from app.common import config, messages

# Cada cuánto se vuelve a pedir el historial. Es lo que hace que un trabajo en curso se vea
# avanzar: el estado lo informa el servidor, el cliente no adivina nada.
REFRESH_INTERVAL_SECONDS = 1.0

# La operación con la que arranca la pantalla. Que haya una elegida evita que el área de
# parámetros aparezca vacía y el botón quede flotando lejos, sin nada en el medio.
DEFAULT_OPERATION = "sanitize"

NOTHING_CHOSEN = "[dim]ninguna imagen elegida[/dim]"
NO_RESULT_YET = "[dim]elegí un trabajo de la lista[/dim]"

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
        downloads: Dónde se guardan los resultados descargados.
        selected: La imagen elegida, o None si todavía no se eligió ninguna.
        detailed: El trabajo cuyo resultado se está mostrando.
    """

    CSS_PATH = "application.tcss"
    TITLE = "Anonimización de imágenes"

    BINDINGS = [
        ("q", "quit", "Salir"),
        ("r", "refresh_jobs", "Refrescar"),
        ("d", "download", "Descargar"),
    ]

    def __init__(
        self, user: str, host: str, port: int, directory: Path, downloads: Path
    ) -> None:
        """Guarda la configuración sin conectarse todavía.

        La conexión se abre en `on_mount`, cuando ya hay pantalla donde informar un fallo.
        """
        super().__init__()
        self.user = user
        self.host = host
        self.port = port
        self.directory = directory
        self.downloads = downloads
        self.selected: Path | None = None
        self.operation: str | None = None
        self.detailed: str | None = None
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
                yield Label("Carpeta", classes="field")
                # Escribir una carpeta y confirmar cambia la raíz del árbol. Sin esto solo
                # se podría bajar desde la carpeta de arranque, nunca salir de ella.
                yield Input(
                    value=str(self.directory), id="root", placeholder="ruta; ~ vale"
                )
                yield ImageTree(self.directory, id="tree")

            with Vertical(id="operation"):
                # Las operaciones salen del catálogo: agregar una al servidor la hace
                # aparecer acá sin tocar esta pantalla.
                yield RadioSet(
                    *(RadioButton(name) for name in sorted(config.OPERATION_PARAMETERS)),
                    id="operations",
                )
                yield Label(NOTHING_CHOSEN, id="chosen")
                # Se llena y se vacía según la operación elegida: cada una tiene los suyos.
                yield Vertical(id="parameters")
                yield ProgressBar(id="upload", show_eta=False)
                yield Button("Enviar", id="send", variant="primary")

            with Vertical(id="jobs"):
                yield DataTable(id="table", cursor_type="row")
                with Vertical(id="result-panel"):
                    # Lo que produjo el trabajo señalado. Para `inspect` es todo el sentido
                    # de la operación: el informe ES el resultado, no hay archivo.
                    yield Static(NO_RESULT_YET, id="result")

        yield Footer()

    async def on_mount(self) -> None:
        """Abre la conexión, prepara la tabla y arranca el refresco periódico."""
        # Los títulos van en el borde del panel, no en una fila aparte: se lee como un
        # panel y no gasta una línea de alto en cada columna.
        for identificador, titulo in [
            ("#images", " Imágenes "),
            ("#operation", " Operación "),
            ("#jobs", " Trabajos "),
            ("#result-panel", " Resultado "),
        ]:
            self.query_one(identificador).border_title = titulo

        table = self.query_one("#table", DataTable)
        table.add_column("Estado", width=12)
        table.add_column("Operación", width=10)
        table.add_column("Archivo", width=24)
        table.add_column("Enviado", width=16)

        self.query_one("#upload", ProgressBar).display = False

        # Arrancar con una operación elegida y sus parámetros a la vista.
        operaciones = self.query_one("#operations", RadioSet)
        for boton in operaciones.query(RadioButton):
            if str(boton.label) == DEFAULT_OPERATION:
                boton.value = True
                break

        self.update_send_button()

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
        self.update_send_button()
        self.query_one("#chosen", Label).update(
            f"Elegida: [bold]{event.path.name}[/bold] "
            f"({formatting.format_size(event.path.stat().st_size)})"
        )

    def update_send_button(self) -> None:
        """Apaga el botón mientras falte algo, y dice qué falta.

        Es preferible a dejarlo encendido y contestar con un error después de apretarlo:
        el estado de la pantalla ya sabe si se puede enviar, así que conviene mostrarlo.
        """
        boton = self.query_one("#send", Button)

        if self.selected is None:
            boton.label = "Elegí una imagen"
        elif self.operation is None:
            boton.label = "Elegí una operación"
        else:
            boton.label = f"Enviar a {self.operation}"

        boton.disabled = self.selected is None or self.operation is None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Cambia la carpeta que muestra el árbol.

        Se acepta `~` porque es como se escribe la carpeta personal en una terminal, y
        quien usa esto está en una.

        Args:
            event: Lo que emite el campo al confirmarse con Enter.
        """
        if event.input.id != "root":
            return

        # `expandvars` para $HOME y `expanduser` para ~: las dos formas en que se escribe
        # una ruta en una terminal, que es de donde viene quien usa esto.
        elegida = Path(os.path.expandvars(event.value.strip())).expanduser()

        if not elegida.is_absolute():
            # Relativa a la carpeta que se está viendo, no a donde arrancó el programa.
            # Es lo que hace que `..` suba un nivel DESDE ACÁ, y que se pueda repetir:
            # midiéndola desde el arranque, `..` lleva siempre al mismo lugar.
            elegida = self.directory / elegida

        elegida = elegida.resolve()

        if not elegida.is_dir():
            self.notify(f"No es una carpeta: {elegida}", severity="warning")
            # Se restaura la anterior: dejar el texto inválido en pantalla hace creer que
            # el árbol muestra esa carpeta.
            event.input.value = str(self.directory)
            return

        self.directory = elegida
        # Se muestra la ruta ya resuelta y no lo que se escribió: después de un `..` el
        # campo diría `..`, que no informa dónde quedaste parado.
        event.input.value = str(elegida)

        arbol = self.query_one("#tree", ImageTree)
        arbol.path = elegida
        arbol.focus()

    async def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Cambia la operación elegida y rearma los parámetros que le corresponden.

        Args:
            event: Lo que emite el grupo de opciones al cambiar la selección.
        """
        self.operation = str(event.pressed.label)
        await self.rebuild_parameters()
        self.update_send_button()

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

            # Un campo sin completar se omite, y el servidor aplica su valor por defecto.
            # Se comprueba que sea texto con contenido en vez de compararlo contra el
            # centinela de `Select`: ese centinela cambió de forma entre versiones de
            # Textual, y compararlo mal hacía que se enviara al servidor.
            if not isinstance(crudo, str) or not crudo.strip():
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

        # La tabla se repinta entera cada segundo. Sin recordar dónde estaba el cursor, la
        # fila elegida se perdería a cada refresco y no se podría llegar a apretar "d".
        cursor = table.cursor_row

        table.clear()
        for job in jobs:
            # El `job_id` va como clave de la fila: es lo que después pide la descarga, y
            # no tiene por qué ocupar una columna en pantalla.
            table.add_row(
                formatting.status_markup(job.get("status", "")),
                job.get("op", ""),
                job.get("filename", ""),
                formatting.format_timestamp(job.get("created_at")),
                key=job.get("job_id", ""),
            )

        if 0 <= cursor < table.row_count:
            table.move_cursor(row=cursor)

        await self.refresh_result()

    async def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """Cambia el trabajo del que se muestra el resultado.

        Args:
            event: Lo que emite la tabla al moverse el cursor de fila.
        """
        if event.row_key is not None:
            self.detailed = str(event.row_key.value)
            await self.refresh_result()

    async def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Descarga el resultado del trabajo al confirmar su fila con Enter.

        Es la segunda forma de descargar, y la confiable: el atajo `d` no funciona mientras
        se está escribiendo en un campo de parámetros, porque ahí la tecla es una letra.

        Args:
            event: Lo que emite la tabla al confirmar una fila.
        """
        await self.action_download()

    async def refresh_result(self) -> None:
        """Pide el estado del trabajo señalado y muestra lo que produjo.

        Se consulta cada vez, y no se guarda de la vez anterior, porque un trabajo en curso
        va cambiando: su resultado aparece recién cuando termina.

        Los campos que revelan información privada se resaltan, con el mismo criterio que
        el cliente de terminal: son el punto de la aplicación, no un dato más.
        """
        panel = self.query_one("#result", Static)

        if self._session is None or not self._session.is_connected or self.detailed is None:
            panel.update(NO_RESULT_YET)
            return

        try:
            estado = await self._session.status(self.detailed)
        except (messages.ServerError, OSError):
            panel.update("")
            return

        if estado.get("status") == messages.FAILED:
            panel.update(f"[red]{estado.get('error', 'el trabajo falló')}[/red]")
            return

        resultado = estado.get("result") or {}
        if not resultado:
            panel.update("[dim]todavía sin resultado[/dim]")
            return

        renglones = []
        for campo, valor in resultado.items():
            texto = formatting.format_result_value(campo, valor)
            if formatting.is_privacy_sensitive(campo, valor):
                texto = f"[bold yellow]{texto}[/bold yellow] [dim](dato privado)[/dim]"
            renglones.append(f"[dim]{formatting.result_label(campo)}:[/dim] {texto}")

        panel.update("\n".join(renglones))

    async def action_download(self) -> None:
        """Descarga el resultado del trabajo señalado en la tabla.

        Los rechazos que informa el servidor —que el trabajo no terminó, que la operación
        no genera archivo— se muestran tal cual: son respuestas del protocolo, no fallas.
        """
        if self._session is None or not self._session.is_connected:
            self.notify("No hay conexión con el servidor", severity="error")
            return

        table = self.query_one("#table", DataTable)
        if not table.row_count:
            self.notify("No hay ningún trabajo para descargar", severity="warning")
            return

        job_id = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)

        # El nombre lo sugiere el servidor y recién se conoce con la respuesta, así que se
        # baja a un temporal y se renombra al terminar. `.name` por lo mismo que en la
        # sesión: el nombre viene de afuera y no debe sacar la escritura del directorio.
        temporary = self.downloads / f".descarga-{job_id}"
        try:
            self.downloads.mkdir(parents=True, exist_ok=True)
            _, response = await self._session.download(job_id, temporary)
            suggested = Path(response.get("filename", "")).name or f"{job_id}.bin"
            written = self.downloads / suggested
            temporary.replace(written)
        except messages.ServerError as failure:
            self.notify(str(failure), severity="warning", timeout=8)
            return
        except OSError as failure:
            self.notify(f"No se pudo descargar: {failure}", severity="error", timeout=8)
            return

        self.notify(f"Guardado en {written.resolve()}", timeout=8)
