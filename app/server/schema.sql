-- Esquema de la base de datos del sistema.
--
-- Lo crea el proceso de ingreso al arrancar. Es idempotente: se puede ejecutar sobre una
-- base ya creada sin efecto, que es lo que pasa en cada arranque después del primero.

CREATE TABLE IF NOT EXISTS jobs (
  id          TEXT PRIMARY KEY,   -- job_id (UUID v4)
  user        TEXT NOT NULL,
  op          TEXT NOT NULL,      -- anonymize | clean | convert | compress | sanitize | inspect
  params      TEXT NOT NULL,      -- JSON con las claves ordenadas
  sha256      TEXT NOT NULL,      -- huella del contenido de la imagen de entrada
  filename    TEXT,               -- nombre original, solo informativo
  status      TEXT NOT NULL,      -- QUEUED | PROCESSING | DONE | ERROR
  error       TEXT,
  result_path TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);

-- Sostiene la búsqueda de duplicados: sin él, cada envío recorrería la tabla entera.
CREATE INDEX IF NOT EXISTS idx_dedup ON jobs(user, sha256, op, params);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id  TEXT NOT NULL REFERENCES jobs(id),
  kind    TEXT NOT NULL,          -- queued | started | done | failed
  ts      TEXT NOT NULL,
  detail  TEXT
);
