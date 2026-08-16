"""Presentación del cliente: muestra por pantalla el resultado de cada acción.

Todo lo que hay acá es presentación. La conversación con el servidor la resuelve
`session.ClientSession`; estas funciones se limitan a llamar sus métodos y a mostrar lo
que devuelven.

Mantener la presentación separada de la red es lo que permite probar cada una por su
lado: la sesión se prueba sin mirar ninguna salida, y estas funciones sin abrir un socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from app.client import formatting, session
from app.common import messages

# Frase que se muestra mientras se espera, según en qué estado esté el trabajo.
WAITING_DESCRIPTIONS = {
    messages.QUEUED: "En cola, esperando un worker…",
    messages.PROCESSING: "Procesando la imagen…",
    messages.DONE: "Listo",
    messages.FAILED: "Falló",
}


def build_console() -> Console:
    """Crea la consola de Rich que usa todo el modo directo.

    Rich detecta solo si el destino es una terminal: si la salida se redirige a un archivo
    o a otro programa, suprime los colores y deja texto plano, de modo que la salida sigue
    siendo utilizable en un pipe.
    """
    return Console()


def build_transfer_progress(console: Console) -> Progress:
    """Arma la barra de progreso de las transferencias de imágenes.

    Muestra cuánto se transfirió del total, a qué velocidad y hace cuánto, que es lo que
    se quiere ver mientras una imagen de varios MB viaja en bloques.
    """
    return Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}"),
        BarColumn(complete_style="cyan", finished_style="green"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


# ──────────────────────────────── submit ────────────────────────────────


async def run_submit(
    client_session: session.ClientSession,
    console: Console,
    image_path: Path,
    operation: str,
    parameters: dict[str, Any],
    wait_for_result: bool,
    timeout: float,
    output_path: Path | None,
) -> int:
    """Envía una imagen y, si se pidió con --wait, espera el resultado y lo descarga.

    Returns:
        0 si todo salió bien, 1 si el trabajo falló o si se agotó la espera.
    """
    response = await upload_image(client_session, console, image_path, operation, parameters)
    job_id = response.get("job_id", "")

    if not wait_for_result:
        console.print(
            f"\nConsultá el estado con: [bold cyan]--action status --job-id {job_id}[/bold cyan]"
        )
        return 0

    final_status = response.get("status", "")
    if final_status not in messages.TERMINAL_STATUSES:
        final_status = await wait_showing_progress(client_session, console, job_id, timeout)

    return await report_finished_job(
        client_session, console, job_id, final_status, timeout, output_path
    )


async def upload_image(
    client_session: session.ClientSession,
    console: Console,
    image_path: Path,
    operation: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Sube la imagen mostrando una barra de progreso e imprime el comprobante.

    Returns:
        El header de la respuesta al submit.
    """
    file_size = image_path.stat().st_size

    with build_transfer_progress(console) as progress:
        upload = progress.add_task(f"Enviando {image_path.name}", total=file_size)

        def show_progress(sent_bytes: int, _total: int) -> None:
            progress.update(upload, completed=sent_bytes)

        response = await client_session.submit(
            image_path, operation, parameters, on_progress=show_progress
        )

    console.print(render_submit_receipt(response, image_path, operation, file_size))

    if response.get("deduplicated"):
        console.print(
            "[yellow]Esta imagen ya había sido procesada con esta misma operación.[/yellow] "
            "El resultado anterior está listo para descargar."
        )

    return response


async def report_finished_job(
    client_session: session.ClientSession,
    console: Console,
    job_id: str,
    final_status: str,
    timeout: float,
    output_path: Path | None,
) -> int:
    """Muestra el desenlace de un trabajo esperado y descarga el archivo si lo hay.

    Returns:
        0 si el trabajo terminó bien, 1 si falló o si se agotó la espera.
    """
    if final_status == messages.FAILED:
        status_response = await client_session.status(job_id)
        console.print(
            f"[red]El trabajo falló:[/red] {status_response.get('error', 'sin motivo informado')}"
        )
        return 1

    if final_status != messages.DONE:
        console.print(
            f"\n[yellow]Se agotó la espera de {timeout:.0f} s.[/yellow] El trabajo sigue su "
            f"curso: el resultado va a quedar disponible."
        )
        console.print(
            f"Volvé a consultarlo con: [bold cyan]--action status --job-id {job_id}[/bold cyan]"
        )
        return 1

    status_response = await client_session.status(job_id)
    print_result_data(console, status_response.get("result", {}))

    if not status_response.get("has_output", False):
        console.print(
            "\n[dim]Esta operación no genera archivo: su resultado es el informe de arriba.[/dim]"
        )
        return 0

    await run_download(client_session, console, job_id, output_path)
    return 0


async def wait_showing_progress(
    client_session: session.ClientSession,
    console: Console,
    job_id: str,
    timeout: float,
) -> str:
    """Espera a que el trabajo termine, mostrando en qué estado está.

    La espera usa `asyncio.sleep` entre consultas, así que no bloquea: el spinner se sigue
    animando porque el event loop tiene el control mientras tanto.

    Returns:
        El último estado observado. Puede no ser terminal si se agotó el tiempo.
    """
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        waiting = progress.add_task("En cola…", total=None)

        def show_status(response: dict[str, Any], _elapsed: float) -> None:
            status = response.get("status", "")
            progress.update(waiting, description=WAITING_DESCRIPTIONS.get(status, status))

        final_response = await client_session.wait_until_finished(
            job_id, timeout, on_poll=show_status
        )

    return final_response.get("status", "")


def render_submit_receipt(
    response: dict[str, Any],
    image_path: Path,
    operation: str,
    file_size: int,
) -> Panel:
    """Arma el recuadro con el comprobante de un envío aceptado."""
    status = response.get("status", "")

    receipt = Table.grid(padding=(0, 2))
    receipt.add_column(style="dim", justify="right")
    receipt.add_column()

    receipt.add_row("Trabajo", f"[bold]{response.get('job_id', '—')}[/bold]")
    receipt.add_row("Estado", formatting.status_markup(status))
    receipt.add_row("Operación", operation)
    receipt.add_row("Imagen", f"{image_path.name} ({formatting.format_size(file_size)})")

    title = "Imagen ya procesada" if response.get("deduplicated") else "Imagen aceptada"
    border = "yellow" if response.get("deduplicated") else "cyan"

    return Panel(receipt, title=title, border_style=border, expand=False)


# ──────────────────────────────── status ────────────────────────────────


async def run_status(
    client_session: session.ClientSession,
    console: Console,
    job_id: str,
) -> int:
    """Consulta el estado de un trabajo y lo muestra.

    Returns:
        0 si el trabajo existe y no falló, 1 si su estado es de error.
    """
    response = await client_session.status(job_id)
    status = response.get("status", "")

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim", justify="right")
    summary.add_column()
    summary.add_row("Trabajo", f"[bold]{job_id}[/bold]")
    summary.add_row("Estado", formatting.status_markup(status))

    if status == messages.FAILED:
        summary.add_row("Motivo", f"[red]{response.get('error', 'sin motivo informado')}[/red]")
    elif status == messages.DONE:
        summary.add_row(
            "Archivo",
            "[green]disponible para descargar[/green]" if response.get("has_output")
            else "[dim]esta operación no genera archivo[/dim]",
        )

    console.print(Panel(summary, border_style=formatting.status_style(status), expand=False))
    print_result_data(console, response.get("result", {}))

    return 1 if status == messages.FAILED else 0


def print_result_data(console: Console, result: dict[str, Any]) -> None:
    """Muestra los datos que produjo la operación, si los hay.

    Son los pocos cientos de bytes que viajan en la respuesta de estado: cuántas caras se
    detectaron, qué metadatos se eliminaron, el informe de `inspect`. Los campos que
    revelan información privada se resaltan, porque son el punto de la aplicación.
    """
    if not result:
        return

    data = Table.grid(padding=(0, 2))
    data.add_column(style="dim", justify="right")
    data.add_column()

    for field, value in result.items():
        formatted = formatting.format_result_value(field, value)
        if formatting.is_privacy_sensitive(field, value):
            formatted = f"[bold yellow]{formatted}[/bold yellow]  [dim](dato privado)[/dim]"
        data.add_row(formatting.result_label(field), formatted)

    console.print(Panel(data, title="Resultado", border_style="dim", expand=False))


# ─────────────────────────────── download ───────────────────────────────


async def run_download(
    client_session: session.ClientSession,
    console: Console,
    job_id: str,
    output_path: Path | None,
) -> int:
    """Descarga el archivo de un trabajo terminado.

    Returns:
        0. Los problemas se propagan como excepciones y los atrapa `cli.main`.
    """
    # El tamaño no se conoce hasta que llega el header, así que la barra arranca sin total
    # y se completa en la primera llamada de avance.
    with build_transfer_progress(console) as progress:
        download = progress.add_task("Descargando el resultado", total=None)

        def show(received: int, total: int) -> None:
            progress.update(download, completed=received, total=total)

        written_path, response = await client_session.download(job_id, output_path, show)

    size = response.get("payload_size", 0)
    console.print(
        f"[green]✓[/green] Resultado guardado en [bold]{written_path}[/bold] "
        f"({formatting.format_size(size)})"
    )
    return 0


# ─────────────────────────────── history ───────────────────────────────


async def run_history(
    client_session: session.ClientSession,
    console: Console,
    limit: int,
) -> int:
    """Lista los últimos trabajos del usuario."""
    jobs = await client_session.history(limit)

    if not jobs:
        console.print("[dim]Todavía no hay trabajos para este usuario.[/dim]")
        return 0

    console.print(render_history_table(jobs, client_session.user))
    return 0


def render_history_table(jobs: list[dict[str, Any]], user: str) -> Table:
    """Arma la tabla del historial, del trabajo más reciente al más antiguo."""
    table = Table(
        title=f"Últimos trabajos de {user}",
        title_style="bold",
        border_style="dim",
        header_style="bold cyan",
        expand=False,
    )

    # Un UUID mide 36 caracteres y no se puede recortar: es lo que el usuario tiene que
    # copiar para consultar el trabajo después. Con seis columnas no entra en una terminal
    # de 80, así que se agrupan de a dos datos por celda y quedan cuatro columnas.
    table.add_column("Trabajo", no_wrap=True)
    table.add_column("Operación")
    table.add_column("Estado")
    table.add_column("Enviado")

    for job in jobs:
        filename = job.get("filename") or "—"
        duration = formatting.format_duration(job.get("created_at"), job.get("finished_at"))

        table.add_row(
            f"{job.get('job_id', '—')}\n[dim]{filename}[/dim]",
            job.get("op", "—"),
            formatting.status_markup(job.get("status", "")),
            f"{formatting.format_timestamp(job.get('created_at'))}\n[dim]{duration}[/dim]",
        )

    return table


# ──────────────────────────────── errores ────────────────────────────────


def print_error(console: Console, title: str, detail: str, hint: str = "") -> None:
    """Muestra un error de forma consistente, con una sugerencia si la hay.

    Args:
        console: Conviene pasarle una construida con `Console(stderr=True)`, para que los
            errores no se mezclen con la salida útil cuando el cliente se usa en un pipe.
    """
    body: list[Any] = [Text(detail)]
    if hint:
        body.append(Text(hint, style="dim"))

    console.print(
        Panel(Group(*body), title=f"[red]{title}[/red]", border_style="red", expand=False)
    )
