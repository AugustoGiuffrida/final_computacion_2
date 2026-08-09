# Propuesta A — «PixelForge»: Servidor concurrente de procesamiento de imágenes

## 1. Descripción verbal de la aplicación

Quiero armar una aplicación cliente-servidor donde múltiples clientes envían imágenes
a un servidor central para que éste les aplique procesamientos pesados (miniaturas,
filtros, marca de agua, extracción de metadatos/OCR) y les devuelva el resultado.

El **cliente** es una herramienta CLI (con `argparse`) que se conecta por **socket TCP**
al servidor, envía una imagen junto con la operación pedida (`--op thumbnail|blur|watermark|ocr`)
y luego consulta el estado del trabajo o espera el resultado.

El **servidor** está construido sobre **asyncio**: acepta N clientes concurrentes sin
bloquear el event loop (la recepción de archivos es I/O puro). El servidor **no procesa
las imágenes él mismo**: cada pedido se convierte en una tarea que se publica en una
**cola de tareas distribuida (Celery + Redis)**. Uno o más **workers Celery** —procesos
independientes, posiblemente en otras máquinas o contenedores— consumen la cola y
ejecutan el procesamiento CPU-bound con Pillow/pytesseract. Así el trabajo pesado se
paraleliza en procesos separados y el servidor queda libre para atender conexiones.

Aquí se usa **concurrencia** (asyncio) para atender múltiples clientes, y **paralelismo**
(workers Celery = procesos separados) para el procesamiento CPU-bound; esta separación
respeta la lección del GIL vista en clase: I/O-bound → async, CPU-bound → procesos.

Además, el servidor mantiene un **proceso auxiliar de auditoría/estadísticas** con el que
se comunica mediante **IPC (Queue de multiprocessing)**: cada evento (trabajo recibido,
completado, fallado) se le envía por la queue y este proceso persiste el historial en
**SQLite** y calcula métricas agregadas (trabajos por usuario, tiempos promedio por
operación). Esto justifica el requisito de IPC con un rol real: desacoplar la escritura
a disco/DB del camino crítico de red.

Los resultados quedan disponibles para descarga: el cliente puede hacer
`--action status --job-id X` o `--action download --job-id X`.

**Despliegue**: todo el stack (servidor, Redis, workers, opcionalmente Flower para
monitorear la cola) se levanta con **Docker Compose**.

## 2. Gráfico de la arquitectura

```
                                   ┌──────────────────────────────────────────────┐
 ┌────────────┐   socket TCP      │                SERVIDOR (host)               │
 │ Cliente 1  │◄─────────────────►│  ┌────────────────────────────────────────┐  │
 │  (CLI)     │    (asyncio       │  │   Proceso principal (asyncio)          │  │
 └────────────┘     streams)      │  │   - acepta N clientes concurrentes     │  │
 ┌────────────┐                   │  │   - recibe imágenes (I/O async)        │  │
 │ Cliente 2  │◄─────────────────►│  │   - publica tareas en Celery           │  │
 │  (CLI)     │                   │  │   - responde estados / resultados      │  │
 └────────────┘                   │  └───────┬──────────────────────┬─────────┘  │
 ┌────────────┐                   │          │ IPC:                 │            │
 │ Cliente N  │◄─────────────────►│          │ multiprocessing.Queue│ publish    │
 └────────────┘                   │          ▼                      │ (broker)   │
                                  │  ┌──────────────────┐           │            │
                                  │  │ Proceso auditor   │          │            │
                                  │  │ - historial       │          │            │
                                  │  │ - estadísticas    │          │            │
                                  │  │ - escribe SQLite  │          │            │
                                  │  └────────┬─────────┘           │            │
                                  └───────────┼─────────────────────┼────────────┘
                                              ▼                     ▼
                                        ┌──────────┐         ┌────────────┐
                                        │  SQLite  │         │   Redis    │
                                        │ (jobs.db)│         │  (broker + │
                                        └──────────┘         │  backend)  │
                                                             └─────┬──────┘
                                                                   │ consume
                                                    ┌──────────────┼──────────────┐
                                                    ▼              ▼              ▼
                                              ┌──────────┐  ┌──────────┐  ┌──────────┐
                                              │ Worker 1 │  │ Worker 2 │  │ Worker N │
                                              │ (Celery) │  │ (Celery) │  │ (Celery) │
                                              │ Pillow / │  │          │  │          │
                                              │ OCR      │  │          │  │          │
                                              └──────────┘  └──────────┘  └──────────┘
                                       (contenedores Docker escalables: --scale worker=N)
```

## 3. Funcionalidades por entidad

### Cliente (CLI, `argparse`)
- `--host/--port` para configurar la conexión (soporte IPv4/IPv6).
- `--action submit --file foto.jpg --op thumbnail|blur|watermark|ocr [--params ...]`:
  envía la imagen y recibe un `job_id`.
- `--action status --job-id X`: consulta el estado (pendiente / procesando / listo / error).
- `--action download --job-id X -o salida.jpg`: descarga el resultado.
- `--action history`: lista los trabajos previos del usuario.
- Modo `--wait`: espera de forma asíncrona a que el trabajo termine y descarga directo.

### Servidor (asyncio)
- Acepta múltiples clientes concurrentes con `asyncio.start_server` (streams).
- Protocolo propio de mensajes (JSON con encabezado de longitud + payload binario).
- Valida el pedido (formato de imagen, tamaño máximo, operación soportada).
- Publica la tarea en Celery (`task.delay()`) y devuelve el `job_id` al cliente.
- Consulta estado/resultado en el result backend de Redis.
- Reporta cada evento al proceso auditor por la `multiprocessing.Queue`.
- Manejo limpio de desconexiones y señales (SIGINT/SIGTERM → shutdown ordenado).

### Workers Celery
- Tareas: `make_thumbnail`, `apply_blur`, `apply_watermark`, `run_ocr`.
- Procesamiento CPU-bound en paralelo real (procesos separados, sin GIL compartido).
- Reintentos automáticos ante fallos transitorios (`max_retries`).
- Escalables horizontalmente (`docker compose up --scale worker=4`).

### Proceso auditor (IPC)
- Recibe eventos por `multiprocessing.Queue`.
- Persiste historial de trabajos en SQLite.
- Calcula estadísticas agregadas (por usuario, por operación, tiempos promedio).
- Responde las consultas de historial que le reenvía el servidor.

## 4. Mapeo contra los requisitos del final

| Requisito | Cómo se cumple |
|---|---|
| Sockets con clientes múltiples concurrentes | Servidor `asyncio.start_server`, N clientes en simultáneo |
| Mecanismos de IPC | `multiprocessing.Queue` entre servidor y proceso auditor |
| Asincronismo de I/O | asyncio en servidor (y en el cliente para `--wait`) |
| Cola de tareas distribuidas | Celery + Redis con workers escalables |
| Parseo de argumentos CLI | `argparse` en cliente y servidor |
| *(Adicional)* Docker | Docker Compose: servidor + redis + workers + flower |
| *(Adicional)* Base de datos | SQLite para historial y estadísticas |
| *(Adicional)* Celery | Es el corazón del procesamiento |

## 5. Justificación de mecanismos (resumen para discutir)

- **¿Por qué asyncio y no threads en el servidor?** La atención de clientes es
  I/O-bound (recibir/enviar bytes); asyncio escala a muchas conexiones con un solo
  proceso y sin costo de context-switch ni locks.
- **¿Por qué Celery y no `multiprocessing.Pool`?** El procesamiento es CPU-bound y
  además queremos distribuirlo: la cola permite workers en otras máquinas/contenedores,
  reintentos, y desacopla la vida del servidor de la de los workers.
- **¿Por qué un proceso auditor con Queue y no escribir a SQLite desde el servidor?**
  Evita bloquear el event loop con escrituras a disco y concentra el acceso a la DB en
  un único proceso (sin necesidad de locks sobre SQLite).
- **Alcance acotado**: sin autenticación compleja (usuario = nombre pasado por CLI),
  sin interfaz web obligatoria (Flower opcional ya da visual de la cola).
