"""El registro permanente de los trabajos, en SQLite.

Es la contraparte de `JobRegistry`: aquel vive en memoria y se pierde al reiniciar, este
sobrevive. Los dos procesos lo conocen, con papeles distintos y excluyentes: el de ingreso
escribe con `JobWriter` y el principal lee con `JobReader`.

Esa división no es una convención: `JobReader` abre la base en modo solo lectura y SQLite
rechaza cualquier escritura que intente. Es lo que hace viable usar SQLite acá, que admite
muchos lectores y un único escritor.

El esquema está en `schema.sql`, al lado de este archivo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.common import messages
from app.server import ipc

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def canonical_parameters(parameters: dict[str, Any]) -> str:
    """Convierte los parámetros a texto de una única forma posible.

    Los duplicados se buscan comparando esta cadena, así que `{"mode": "blur",
    "strength": 15}` y `{"strength": 15, "mode": "blur"}` tienen que dar el mismo texto.
    Ordenar las claves es lo que lo garantiza.
    """
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"))


class JobWriter:
    """El único que escribe en la base. Lo usa el proceso de ingreso."""

    def __init__(self, path: Path) -> None:
        """Abre la base y crea el esquema si todavía no existe."""
        path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(path)
        # WAL deja que el servidor lea mientras este proceso escribe, sin que ninguno de
        # los dos espere al otro.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA_PATH.read_text())
        self._connection.commit()

    def find_duplicate(
        self, user: str, content_hash: str, operation: str, parameters: dict[str, Any]
    ) -> str | None:
        """Busca un trabajo terminado que ya haya producido este mismo resultado.

        La identidad son cuatro campos y no solo el hash: la misma foto difuminada con
        `strength=15` da otro resultado que con `strength=30`. Y filtra por usuario por
        privacidad: reutilizar el trabajo de otro le revelaría que procesó esa imagen.

        Returns:
            El `job_id` del original, o None si no hay ninguno.
        """
        row = self._connection.execute(
            """
            SELECT id FROM jobs
             WHERE user = :user AND sha256 = :sha256
               AND op = :op AND params = :params AND status = :status
             LIMIT 1
            """,
            {
                "user": user,
                "sha256": content_hash,
                "op": operation,
                "params": canonical_parameters(parameters),
                "status": messages.DONE,
            },
        ).fetchone()

        return row[0] if row else None

    def insert(self, request: ipc.ReviewRequest, content_hash: str) -> None:
        """Registra el trabajo recién aceptado, en estado QUEUED.

        Es el momento en que pasa a existir de forma permanente: hasta acá solo vivía en
        el índice en memoria del servidor, que se pierde al reiniciar.
        """
        self._connection.execute(
            """
            INSERT INTO jobs (id, user, op, params, sha256, filename, status, created_at)
            VALUES (:id, :user, :op, :params, :sha256, :filename, :status, :created_at)
            """,
            {
                "id": request.job_id,
                "user": request.user,
                "op": request.operation,
                "params": canonical_parameters(request.parameters),
                "sha256": content_hash,
                "filename": request.stored_path.name,
                "status": messages.QUEUED,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        self._connection.commit()

    def close(self) -> None:
        """Cierra la base."""
        self._connection.close()


class JobReader:
    """Lee la base sin poder escribirla. Lo usa el proceso principal.

    La conexión se abre en el primer uso y no al construirse: cuando el servidor arranca,
    la base puede no existir todavía, porque quien la crea es el proceso de ingreso.
    """

    def __init__(self, path: Path) -> None:
        """Anota dónde está la base, sin abrirla."""
        self._path = path
        self._connection: sqlite3.Connection | None = None

    def find(self, job_id: str) -> dict[str, Any] | None:
        """Busca un trabajo por su identificador.

        Returns:
            Los campos del trabajo, o None si no existe o la base todavía no fue creada.
        """
        connection = self._open()
        if connection is None:
            return None

        row = connection.execute(
            "SELECT * FROM jobs WHERE id = :id", {"id": job_id}
        ).fetchone()

        return dict(row) if row else None

    def close(self) -> None:
        """Cierra la base, si llegó a abrirse."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _open(self) -> sqlite3.Connection | None:
        """Abre la base en solo lectura, o devuelve None si todavía no existe."""
        if self._connection is not None:
            return self._connection

        if not self._path.exists():
            return None

        self._connection = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        self._connection.row_factory = sqlite3.Row

        return self._connection
