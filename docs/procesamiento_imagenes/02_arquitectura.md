# SafePic — Arquitectura

## 1. Vista general

```
 ┌────────────┐
 │ Cliente 1  │◄──────┐
 │ (CLI)      │       │ socket TCP (protocolo propio:
 └────────────┘       │  header JSON + payload binario)
 ┌────────────┐       │
 │ Cliente N  │◄──────┤
 └────────────┘       ▼
          ┌───────────────────────────────────────────────────────┐
          │ SERVIDOR                                              │
          │  ┌─────────────────────────────┐   IPC                │
          │  │ Proceso principal (asyncio) │  mp.Queue  ┌───────┐ │
          │  │ - acepta N clientes         │───────────►│Auditor│ │
          │  │ - valida y guarda imágenes  │            │(proc. │ │
          │  │ - encola tareas en Celery   │            │ hijo) │ │
          │  │ - responde status/download  │            └───┬───┘ │
          │  └──────────────┬──────────────┘                │     │
          └─────────────────┼───────────────────────────────┼─────┘
                            │ publish (task.delay)          ▼
                            ▼                         ┌──────────┐
                      ┌───────────┐                   │  SQLite  │
                      │   Redis   │                   │ jobs.db  │
                      │ broker +  │                   └──────────┘
                      │ backend   │
                      └─────┬─────┘
                            │ consume
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
       ┌──────────┐   ┌──────────┐   ┌──────────┐
       │ Worker 1 │   │ Worker 2 │   │ Worker N │   ← contenedores escalables
       │ (Celery, │   │          │   │          │     (--scale worker=N)
       │  Pillow, │   │          │   │          │
       │  OpenCV) │   │          │   │          │
       └────┬─────┘   └────┬─────┘   └────┬─────┘
            └──────────────┼──────────────┘
                           ▼
                ┌─────────────────────┐
                │ Volumen compartido  │  storage/uploads/   (originales)
                │ (bind mount Docker) │  storage/results/   (procesadas)
                └─────────────────────┘
```

Principio rector: **el camino de red nunca se bloquea**. Todo lo que puede tardar
(procesar, escribir a disco la auditoría, esperar un worker) ocurre fuera del proceso
que atiende los sockets.

## 2. Componentes y por qué

### 2.1 Servidor de acceso — asyncio
**Qué hace**: acepta conexiones con `asyncio.start_server`, parsea el protocolo,
valida, guarda la imagen original y encola. Es el único punto de contacto de los clientes.

**Por qué asyncio y no threads/procesos por cliente**: atender clientes es I/O-bound
puro (esperar bytes de la red). Un event loop atiende cientos de conexiones en un solo
hilo, sin costo de context-switch ni locks; mientras un `await` espera datos de un
cliente, el loop atiende a los demás. Threads no darían paralelismo real por el GIL y
sí sumarían complejidad de sincronización.

**Detalle**: publicar en Celery (`task.delay`) es una llamada bloqueante a Redis, por
eso se envuelve en `asyncio.to_thread(...)` para no frenar el event loop.

### 2.2 Cola de tareas — Celery + Redis
**Qué hace**: Redis es el *broker* (buzón de tareas) y el *result backend* (estado y
resultado de cada tarea). Los workers consumen del broker y ejecutan con Pillow.

**Por qué una cola distribuida y no `multiprocessing.Pool` / `ProcessPoolExecutor`**:
un pool vive dentro del proceso servidor: si el servidor se reinicia se pierde lo que
estaba en vuelo, y los workers deben estar en la misma máquina. La cola desacopla por
completo: las tareas persisten en Redis aunque no haya workers vivos, los workers
pueden estar en otras máquinas/contenedores, hay reintentos automáticos, y el escalado
es agregar procesos sin tocar código.

**Por qué Redis y no RabbitMQ**: para este alcance Redis es más simple de operar,
cumple los dos roles (broker + backend) con un solo servicio, y la guía de la materia
lo presenta como la opción recomendada para empezar.

**Decisión clave**: por la cola viajan **rutas de archivo, no bytes**. El broker está
pensado para mensajes de control chicos; las imágenes van por el volumen compartido.

### 2.3 Workers — Celery + Pillow + OpenCV
**Qué hacen**: una tarea por operación (`inspect`, `anonymize`, `clean`, `convert`,
`compress`) y `sanitize` como **`chain`** de subtareas: anonymize → clean →
compress/convert, donde cada etapa recibe como entrada la salida de la anterior.
El procesamiento (detección de caras con OpenCV, difuminado y re-encodeo con Pillow)
es CPU-bound: cada worker es un proceso con su propio intérprete → paralelismo real,
sin GIL compartido. El paralelismo se observa entre trabajos: N imágenes en curso se
reparten entre los workers disponibles.

**Manejo de errores**: `autoretry_for` para fallos transitorios (ej: archivo aún no
visible en el volumen), `max_retries` acotado; un fallo definitivo marca la tarea
FAILURE con el motivo, que el cliente ve en `status`.

### 2.4 Auditor — proceso hijo + multiprocessing.Queue (IPC)
**Qué hace**: proceso lanzado por el servidor al arrancar. Recibe eventos por una
`multiprocessing.Queue` (trabajo recibido / encolado / terminado / fallado) y es el
**único escritor** de SQLite. También responde las consultas de historial.

**Por qué existe**: (1) escribir a disco/DB bloquea, y hacerlo en el event loop
congelaría a todos los clientes; encolar el evento con `put()` es casi instantáneo.
(2) SQLite no maneja bien escritores concurrentes; con un único proceso escritor el
problema desaparece sin locks.

**Por qué `mp.Queue` y no pipe/FIFO**: la Queue serializa objetos Python
automáticamente (se envían dicts de evento, no bytes a parsear), es segura ante
múltiples productores y está construida sobre un pipe — es el mismo mecanismo, con
la ergonomía adecuada al caso. Un FIFO tendría sentido si el consumidor fuera un
programa externo en otro lenguaje.

**Cómo se entera el auditor de que un trabajo terminó**: los workers no pueden
escribir en la Queue (viven en otros contenedores). El servidor mantiene el conjunto
de trabajos en vuelo y una corrutina de fondo consulta su estado en el result backend
(vía `to_thread`); al detectar la terminación, notifica al auditor y libera el
trabajo del conjunto. Alternativa con señales de Celery documentada en TODO.

### 2.5 Almacenamiento
- **Volumen compartido** (`storage/`): originales en `uploads/<job_id>/`, resultados
  en `results/<job_id>/`. Montado en servidor y workers — es lo que permite que la
  cola transporte solo rutas.
- **SQLite** (`jobs.db`): historial permanente y estadísticas. Elegido porque es un
  archivo sin administración y el patrón de un-solo-escritor elimina su principal
  limitación.
- **Redis**: estado *efímero* de las tareas (TTL). La división es deliberada:
  estado vivo → Redis; historial permanente → SQLite.

### 2.6 Despliegue — Docker Compose
Servicios: `server`, `redis`, `worker` (replicable), `flower` (opcional, panel web de
la cola). El volumen `storage/` se monta en `server` y `worker`.
Docker hace **demostrable** lo distribuido: escalar workers en vivo, matar un worker
a mitad de un trabajo y ver el reintento.

## 3. Protocolo de red

Sobre TCP no existen "mensajes", solo un stream de bytes → *length-prefixed framing*:

```
+----------------+----------------------+----------------------+
| 4 bytes u32 BE | header JSON (UTF-8)  | payload binario      |
| long. header   |                      | (opcional)           |
+----------------+----------------------+----------------------+
```

El header siempre incluye `type`, `user` y `payload_size` (0 si no hay payload).
Tipos de mensaje:

| Tipo (cliente → servidor) | Campos extra                       | Payload      | Respuesta del servidor            |
|---------------------------|------------------------------------|--------------|-----------------------------------|
| `submit`                  | `op`, `params`, `filename`         | imagen       | `{job_id, status: "QUEUED"}`      |
| `status`                  | `job_id`                           | —            | `{job_id, status, error?}`        |
| `download`                | `job_id`                           | —            | header + payload con el resultado |
| `history`                 | `limit`                            | —            | `{jobs: [...]}`                   |

Errores: respuesta `{type: "error", code, message}` (ej: operación inválida, imagen
corrupta, job inexistente, tamaño excedido).

## 4. Modelo de datos (SQLite)

```sql
CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,      -- UUID
  user        TEXT NOT NULL,
  op          TEXT NOT NULL,
  params      TEXT,                  -- JSON
  filename    TEXT,
  status      TEXT NOT NULL,         -- QUEUED | PROCESSING | DONE | ERROR
  error       TEXT,
  result_path TEXT,
  created_at  TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id  TEXT NOT NULL REFERENCES jobs(id),
  kind    TEXT NOT NULL,             -- received | queued | started | done | failed
  ts      TEXT NOT NULL,
  detail  TEXT
);
```

Las estadísticas (trabajos por usuario, tiempo promedio por operación, tasa de error)
se derivan con consultas sobre estas dos tablas; no se almacenan redundantes.

## 5. Módulos del código

```
final_comp2/
├── docs/
├── app/
│   ├── common/
│   │   ├── protocol.py      # framing (pack/unpack de frames), tipos de mensaje,
│   │   │                    #   constantes compartidas — único lugar donde se define el protocolo
│   │   └── config.py        # rutas de storage, límites (tamaño máx.), defaults
│   ├── client/
│   │   └── client.py        # argparse + asyncio streams; una función por acción
│   ├── server/
│   │   ├── main.py          # argparse, arranque: lanza auditor, instala manejo de señales,
│   │   │                    #   inicia el event loop
│   │   ├── server.py        # asyncio.start_server + un handler por tipo de mensaje
│   │   ├── jobs.py          # puente con Celery: encolar (to_thread), consultar estado,
│   │   │                    #   corrutina de monitoreo de trabajos en vuelo
│   │   └── auditor.py       # proceso hijo: bucle sobre mp.Queue, escritura SQLite,
│   │                        #   consultas de historial
│   └── worker/
│       ├── celery_app.py    # instancia y configuración de Celery (broker, backend, retries)
│       └── tasks.py         # tareas Pillow/OpenCV, una por operación + sanitize (chain)
├── storage/                 # volumen compartido (uploads/, results/)
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

Regla de dependencias: `client` y `server` solo comparten `common/`; `worker` no
importa nada del servidor (solo `common/config`). Eso garantiza que cada componente
pueda desplegarse por separado.

## 6. Flujo completo (resumen numerado)

1. Cliente parsea argumentos, valida el archivo y abre socket TCP.
2. Envía frame `submit` (header + bytes de la imagen).
3. Servidor (corrutina por cliente) lee el frame sin bloquear el loop, valida,
   guarda el original en `storage/uploads/<job_id>/`.
4. Encola la tarea vía `asyncio.to_thread(task.delay, ...)` → mensaje chico en Redis.
5. Notifica `received`+`queued` al auditor por `mp.Queue` y responde `job_id` al cliente.
6. Un worker toma la tarea, procesa con Pillow/OpenCV, escribe en
   `storage/results/<job_id>/`, marca SUCCESS/FAILURE en el backend.
7. La corrutina de monitoreo del servidor detecta el final y notifica al auditor (`done`/`failed`).
8. Cliente consulta `status` y descarga con `download` (o todo junto con `--wait`).

## 7. Tabla de cumplimiento de requisitos

| Requisito obligatorio | Dónde se cumple |
|---|---|
| Sockets, clientes múltiples concurrentes | `server.py` (asyncio.start_server) |
| Mecanismos de IPC | `mp.Queue` servidor ↔ `auditor.py` |
| Asincronismo de I/O | asyncio en servidor y cliente (`--wait`) |
| Cola de tareas distribuidas | Celery + Redis, `tasks.py` |
| Parseo de argumentos CLI | argparse en `client.py` y `main.py` |

| Adicional | Dónde |
|---|---|
| Docker | docker-compose.yml (server, redis, workers, flower) |
| Base de datos | SQLite vía auditor |
| Celery en paralelo | workers escalables + `sanitize` con `chain` |
| Entorno visual | Flower (opcional) |
