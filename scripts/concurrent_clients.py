"""Lanza varios clientes a la vez contra el servidor, para ver la concurrencia.

Cada cliente es un proceso aparte con su propia conexión TCP: es lo mismo que abrir N
terminales y apretar Enter en todas al mismo tiempo. El script los lanza, espera a que
terminen y anota cuándo terminó cada uno.

Las imágenes quedan en `descargas/<fecha y hora>/`, una por cliente.

    python scripts/concurrent_clients.py                  # 8 clientes contra Docker
    python scripts/concurrent_clients.py --clients 3
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Grande a propósito: con una imagen chica el tiempo se va en arrancar Python y no en
# procesar, y cambiar la cantidad de workers no muestra ninguna diferencia.
DEFAULT_IMAGE = PROJECT_ROOT / "img_test" / "grupo_grande.jpg"

DOWNLOADS_DIR = PROJECT_ROOT / "descargas"


def build_parser() -> argparse.ArgumentParser:
    """Arma los argumentos del script."""
    parser = argparse.ArgumentParser(description="Lanza varios clientes a la vez.")
    parser.add_argument(
        "--clients", type=int, default=8, help="cuántos clientes (por defecto: 8)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=9000, help="por defecto el de Docker, 9000"
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_IMAGE, help="imagen a enviar")
    return parser


def launch_client(
    number: int, arguments: argparse.Namespace, run_dir: Path, stamp: str
) -> subprocess.Popen:
    """Arranca un cliente en un proceso aparte, con su salida en un archivo propio."""
    command = [
        sys.executable, "-m", "app.client",
        # Un usuario distinto en cada corrida: la deduplicación es por usuario, y sin esto
        # la segunda corrida devolvería los resultados anteriores sin procesar nada.
        "--user", f"cliente{number}-{stamp}",
        "--host", arguments.host, "--port", str(arguments.port),
        "--action", "submit", "--file", str(arguments.file), "--op", "sanitize",
        "--wait", "--timeout", "300",
        # Un archivo por cliente: con una carpeta, todos usarían el mismo nombre sugerido
        # y se pisarían entre sí.
        "-o", str(run_dir / f"cliente{number}.jpg"),
    ]

    with (run_dir / "logs" / f"cliente{number}.log").open("w") as log:
        return subprocess.Popen(
            command, cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT
        )


def main() -> int:
    """Lanza los clientes, espera a que terminen y resume cómo les fue."""
    arguments = build_parser().parse_args()

    now = datetime.now()
    run_dir = DOWNLOADS_DIR / now.strftime("%Y-%m-%d_%H-%M-%S")
    (run_dir / "logs").mkdir(parents=True)

    # Primero se lanzan todos y recién después se espera: así arrancan juntos de verdad.
    started_at = time.monotonic()
    running: dict[int, subprocess.Popen] = {}
    for number in range(1, arguments.clients + 1):
        running[number] = launch_client(number, arguments, run_dir, now.strftime("%H%M%S"))
        print(f"cliente{number} lanzado (proceso {running[number].pid})")
    print()

    failed: list[int] = []
    try:
        while running:
            for number, process in list(running.items()):
                if process.poll() is None:
                    continue

                elapsed = time.monotonic() - started_at
                if process.returncode == 0:
                    outcome = "ok"
                else:
                    outcome = f"falló, código {process.returncode}"
                    failed.append(number)
                print(f"cliente{number} terminó a los {elapsed:5.1f} s — {outcome}")
                del running[number]

            time.sleep(0.1)

    except KeyboardInterrupt:
        # Ctrl+C no deja clientes huérfanos dando vueltas.
        for process in running.values():
            process.terminate()
        print("\nInterrumpido: se cortaron los clientes que quedaban.")
        return 130

    total = time.monotonic() - started_at
    succeeded = arguments.clients - len(failed)
    print(
        f"\n{succeeded} de {arguments.clients} clientes terminaron bien, "
        f"en {total:.1f} s en total."
    )
    print(f"Imágenes:  {run_dir.relative_to(PROJECT_ROOT)}")
    print(f"Registros: {(run_dir / 'logs').relative_to(PROJECT_ROOT)}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
