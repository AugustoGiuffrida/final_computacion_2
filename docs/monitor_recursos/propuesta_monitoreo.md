# Propuesta B — «Sentinel»: Sistema de monitoreo distribuido de hosts

## 1. Descripción verbal de la aplicación

Quiero armar un sistema de monitoreo estilo "mini-Nagios": varios **agentes** corriendo
en distintas máquinas (o contenedores) recolectan métricas del sistema (CPU, memoria,
disco, procesos, uptime) y las envían periódicamente por **socket TCP** a un
**servidor central**. El servidor almacena las series de tiempo, evalúa **reglas de
alerta** (ej: CPU > 90% durante 3 muestras seguidas) y genera **reportes periódicos**.

El **servidor central** usa **asyncio** para aceptar y sostener las conexiones de N
agentes concurrentes sin bloquear: cada agente mantiene una conexión persistente y
envía una muestra cada X segundos. Además el servidor atiende, por el mismo u otro
puerto, a **clientes de consulta** (CLI) que piden el estado actual de los hosts,
historial de métricas o alertas activas.

La **evaluación de alertas y la generación de reportes** no se hace en el camino
crítico de red: el servidor publica cada lote de métricas en una **cola de tareas
distribuida (Celery + Redis)**. Los **workers Celery** evalúan las reglas contra el
historial, marcan alertas y arman reportes; **Celery Beat** dispara tareas periódicas
(reporte horario/diario, limpieza de datos viejos). Esto separa la ingesta (I/O-bound,
async) del análisis (CPU-bound/diferible, procesos paralelos).

El servidor tiene además un **proceso escritor de almacenamiento** separado, conectado
por **IPC (multiprocessing.Queue)**: el proceso principal le pasa las muestras recibidas
y este único proceso escribe en **SQLite** de forma serializada, evitando bloquear el
event loop y sin contención sobre la base.

Aquí se usa **concurrencia** con asyncio (ingesta de N agentes + consultas),
**paralelismo** con workers Celery (evaluación de reglas y reportes), e **IPC** para
desacoplar la persistencia. Las entidades se comunican de manera asincrónica: agentes
y clientes por sockets TCP con un protocolo JSON, servidor y workers a través del
broker Redis.

**Despliegue**: Docker Compose con servidor, Redis, workers, beat y varios agentes de
ejemplo, lo que permite demostrar el sistema "distribuido" en una sola máquina.
Opcional: un mini dashboard web (aiohttp) que muestra el estado de los hosts.

## 2. Gráfico de la arquitectura

```
 ┌─────────────┐                    ┌───────────────────────────────────────────────┐
 │  Agente 1   │  socket TCP       │              SERVIDOR CENTRAL                 │
 │ (host/cont.)│──────────────────►│  ┌─────────────────────────────────────────┐  │
 └─────────────┘  muestras cada Xs │  │  Proceso principal (asyncio)            │  │
 ┌─────────────┐                   │  │  - ingesta de N agentes concurrentes    │  │
 │  Agente 2   │──────────────────►│  │  - atiende clientes de consulta (CLI)   │  │
 └─────────────┘                   │  │  - publica lotes en Celery              │  │
 ┌─────────────┐                   │  └─────┬─────────────────────────┬─────────┘  │
 │  Agente N   │──────────────────►│        │ IPC: mp.Queue           │ publish    │
 └─────────────┘                   │        ▼                         │            │
                                   │  ┌───────────────┐               │            │
 ┌─────────────┐   socket TCP      │  │ Proceso       │               │            │
 │ Cliente CLI │◄─────────────────►│  │ escritor DB   │               │            │
 │ (consultas) │  estado/alertas   │  │ (único punto  │               │            │
 └─────────────┘                   │  │ de escritura) │               │            │
                                   │  └──────┬────────┘               │            │
 ┌─────────────┐   HTTP (opcional) │         │                        │            │
 │  Navegador  │◄─────────────────►│         ▼                        ▼            │
 │ (dashboard) │                   │   ┌──────────┐            ┌────────────┐      │
 └─────────────┘                   │   │  SQLite  │            │   Redis    │      │
                                   │   │(metrics, │            │ (broker +  │      │
                                   │   │ alerts)  │            │  backend)  │      │
                                   │   └──────────┘            └─────┬──────┘      │
                                   └─────────────────────────────────┼─────────────┘
                                                                     │ consume
                                                      ┌──────────────┼─────────────┐
                                                      ▼              ▼             ▼
                                                ┌──────────┐  ┌──────────┐  ┌──────────┐
                                                │ Worker 1 │  │ Worker 2 │  │  Beat    │
                                                │ evalúa   │  │ reportes │  │ (tareas  │
                                                │ reglas   │  │          │  │periódicas)│
                                                └──────────┘  └──────────┘  └──────────┘
                                            (contenedores Docker escalables)
```

## 3. Funcionalidades por entidad

### Agente (corre en cada host/contenedor)
- CLI con `argparse`: `--server-host`, `--server-port`, `--interval`, `--hostname`,
  `--metrics cpu,mem,disk` (qué recolectar).
- Recolecta métricas con `psutil` cada `interval` segundos.
- Conexión TCP persistente con reconexión automática ante caída del servidor.
- Envío asincrónico (asyncio) de muestras en JSON con timestamp.
- Manejo de señales para apagado limpio.

### Servidor central (asyncio)
- `asyncio.start_server`: acepta N agentes y clientes de consulta concurrentes.
- Protocolo JSON por líneas (registro del agente, muestras, consultas).
- Mantiene en memoria el "último estado conocido" de cada host (para respuestas rápidas).
- Publica lotes de muestras como tareas Celery para análisis.
- Reenvía cada muestra al proceso escritor vía `multiprocessing.Queue`.
- Detecta agentes caídos (sin muestras hace > N segundos → alerta "host down").

### Cliente de consulta (CLI, `argparse`)
- `--action status`: tabla de hosts con últimas métricas y estado (OK/WARN/CRIT/DOWN).
- `--action history --host X --metric cpu --last 1h`: historial de una métrica.
- `--action alerts [--active]`: alertas activas o históricas.
- `--action report --period daily`: pide el último reporte generado.

### Workers Celery + Beat
- `evaluate_rules(batch)`: compara contra umbrales configurables (YAML/JSON de reglas),
  crea/resuelve alertas con histéresis (N muestras seguidas sobre el umbral).
- `generate_report(period)`: agregados (promedios, máximos, % uptime) por host.
- `cleanup_old_data()`: tarea periódica de Beat que poda datos viejos.
- Escalables horizontalmente; reintentos ante fallos.

### Proceso escritor (IPC)
- Único punto de escritura a SQLite (muestras y alertas) — serializa el acceso.
- Batching de inserts para eficiencia.

### Dashboard web (opcional, si el tiempo alcanza)
- Página simple (aiohttp + HTML) con el estado de hosts y alertas activas.

## 4. Mapeo contra los requisitos del final

| Requisito | Cómo se cumple |
|---|---|
| Sockets con clientes múltiples concurrentes | N agentes + clientes de consulta sobre asyncio |
| Mecanismos de IPC | `multiprocessing.Queue` hacia el proceso escritor de DB |
| Asincronismo de I/O | asyncio en servidor, agentes y cliente de consulta |
| Cola de tareas distribuidas | Celery + Redis (evaluación de reglas, reportes, Beat) |
| Parseo de argumentos CLI | `argparse` en agente, servidor y cliente |
| *(Adicional)* Docker | Compose: servidor + redis + workers + beat + agentes demo |
| *(Adicional)* Base de datos | SQLite con series de tiempo y alertas |
| *(Adicional)* Celery | Análisis y tareas periódicas |
| *(Adicional)* Entorno visual | Dashboard web opcional |

## 5. Justificación de mecanismos (resumen para discutir)

- **¿Por qué asyncio para la ingesta?** Muchas conexiones persistentes de larga vida
  con tráfico esporádico: el caso ideal de un event loop; threads por agente no escala.
- **¿Por qué Celery para las alertas y no evaluarlas inline?** La evaluación consulta
  historial y puede ser costosa; hacerla en workers separados mantiene la ingesta con
  latencia mínima, y Beat da tareas periódicas (reportes, limpieza) "gratis".
- **¿Por qué un proceso escritor con Queue?** SQLite maneja mal escritores concurrentes;
  un único proceso escritor con batching elimina la contención y saca el disco del
  event loop.
- **Recorte de alcance previsto** (si el profesor lo pide): dashboard web y reportes
  son las primeras features recortables; el núcleo agente→servidor→cola→alertas ya
  cumple todos los requisitos obligatorios.

## 6. Riesgos / puntos a validar con el profesor

- Cantidad de métricas y reglas: empezar con 3 métricas y umbrales simples.
- ¿Dashboard web requerido o alcanza con el cliente CLI? (propongo CLI primero).
- Demo en la mesa: todo con Docker Compose en una sola máquina (agentes = contenedores).
