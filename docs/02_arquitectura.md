# Arquitectura

Este documento describe **los componentes del sistema, qué hace cada uno y cómo se
comunican entre sí**. La explicación de qué es cada tecnología y por qué se eligió está
en [03_tecnologias.md](03_tecnologias.md).

## 1. Idea central

El sistema tiene **dos mitades que trabajan a ritmos distintos**:

- Una mitad **atiende**: recibe las imágenes, responde consultas, entrega resultados.
  Su trabajo es casi todo espera de red, y tiene que responder rápido siempre, aunque
  haya muchos clientes conectados.
- La otra mitad **procesa**: detecta caras, difumina, recomprime. Su trabajo es cálculo
  puro, tarda segundos por imagen y consume CPU intensivamente.

Si un mismo programa hiciera las dos cosas, mientras procesa una imagen no podría
atender a nadie más. Por eso están **separadas en procesos distintos y unidas por una
cola de tareas**: la mitad que atiende deja pedidos en la cola y sigue atendiendo; la
mitad que procesa los toma a su ritmo.

De esa separación se derivan todas las decisiones que siguen.

## 2. Diagrama

```
 ┌────────────┐
 │ Cliente 1  │◄──────┐
 │ (CLI)      │       │ socket TCP → IP:9000 (protocolo propio:
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
                            │ encolar tarea                 ▼
                            ▼                         ┌──────────┐
                      ┌───────────┐                   │  SQLite  │
                      │   Redis   │                   │ jobs.db  │
                      │ broker +  │                   └──────────┘
                      │ backend   │
                      └─────┬─────┘
                            │ consumir tareas
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
                │                     │  storage/results/   (procesadas)
                └─────────────────────┘
```

**Cómo leerlo**: el cliente habla con el servidor por un socket TCP. El servidor guarda
la imagen en el volumen compartido, deja la tarea en Redis y le avisa al auditor. Un
worker toma la tarea de Redis, lee la imagen del volumen, la procesa y guarda el
resultado en el mismo volumen. El cliente después consulta el estado y descarga el
resultado, siempre a través del servidor.

Notar que **el cliente solo conoce al servidor**: no sabe que existen Redis, los workers
ni el auditor. Toda la complejidad interna queda detrás de una única dirección y un
único puerto, y eso permite cambiar la mitad de atrás —agregar workers, cambiar el
broker— sin tocar el cliente ni el protocolo.

## 3. Los canales de comunicación

El sistema usa **cuatro canales distintos**, cada uno con un alcance diferente. Esta
tabla es el resumen de la arquitectura:

| Canal | Une | Alcance | Qué transporta |
|---|---|---|---|
| **Socket TCP** | cliente ↔ servidor | entre máquinas | pedidos e imágenes |
| **multiprocessing.Queue** | servidor ↔ auditor | misma máquina, procesos emparentados | eventos de auditoría |
| **Redis** | servidor ↔ workers | entre máquinas, procesos sin relación | invocaciones y estados |
| **Volumen compartido** | servidor ↔ workers | mismo sistema de archivos | los archivos de imagen |

La lógica del reparto: los sockets son el único mecanismo que cruza la red hacia
clientes externos; la Queue es lo más simple y directo entre dos procesos emparentados,
pero no funciona fuera de la máquina; Redis coordina procesos que ni se conocen; y el
disco compartido lleva lo que pesa, porque ninguno de los otros canales está pensado
para megabytes.

## 4. Componentes

### 4.1 Cliente (CLI)

Programa que ejecuta el usuario desde su terminal. Configura la conexión y la identidad
por argumentos de línea de comandos, valida el archivo localmente antes de enviarlo
(que exista, que el formato sea soportado, que no exceda el tamaño máximo), abre la
conexión con el servidor y ejecuta la acción pedida: enviar una imagen, consultar un
estado, descargar un resultado o listar el historial.

Con `--wait` mantiene la conexión abierta, consulta el estado periódicamente y descarga
el resultado apenas está listo.

### 4.2 Servidor — proceso principal

Es el único punto de contacto de los clientes y **la pieza que nunca debe bloquearse**.
Atiende N conexiones concurrentes sobre un solo hilo con asyncio.

Sus responsabilidades:

- Aceptar conexiones y leer los pedidos según el protocolo (sección 5).
- Validar cada pedido: operación existente, imagen bien formada, tamaño dentro del límite.
- **Guardar la imagen original** en el volumen compartido, bajo `uploads/<job_id>/`.
- **Encolar la tarea** en Celery, pasándole la ruta y los parámetros.
- **Notificar cada evento al auditor** por la `multiprocessing.Queue`.
- Responder consultas de estado (leyendo el result backend) e historial (preguntándole
  al auditor).
- Servir las descargas, leyendo el resultado del volumen.
- Vigilar los trabajos en curso (sección 4.5).
- Apagarse ordenadamente ante SIGINT/SIGTERM: dejar de aceptar conexiones, cerrar las
  abiertas y avisar al auditor para que cierre la base.

Lo que el servidor **no** hace, y es deliberado: **no procesa imágenes**. Cualquier
trabajo de CPU dentro del event loop congelaría a todos los clientes conectados.

### 4.3 Auditor — proceso hijo

Proceso que el servidor lanza al arrancar, conectado por una `multiprocessing.Queue`.

Recibe los eventos que el servidor deposita en la cola —trabajo recibido, encolado,
terminado, fallado— y los persiste en SQLite. Es el **único proceso que escribe** en la
base, y también quien responde las consultas de historial que le reenvía el servidor.

Existe por dos razones que se resuelven con una sola decisión. Escribir en disco es una
operación de espera, y hacerla dentro del event loop congelaría a todos los clientes.
Y SQLite tolera mal varios escritores simultáneos: al haber uno solo, ese problema
directamente no se presenta.

### 4.4 Workers

Procesos independientes que ejecutan las operaciones sobre las imágenes. **No son
procesos hijos del servidor**: se levantan por separado, en sus propios contenedores, y
lo único que los vincula con el servidor es que apuntan al mismo Redis.

Cada worker toma mensajes de la cola por su cuenta, lee la imagen del volumen
compartido, la procesa con Pillow y OpenCV, escribe el resultado en `results/<job_id>/`
y reporta el estado y las rutas al result backend.

Acá está el **paralelismo real** del sistema: son procesos separados, con intérpretes y
GILs separados, así que N workers procesan N imágenes simultáneamente en N núcleos.

La operación `sanitize` se implementa como una cadena de tareas (anonymize → clean →
compress), donde cada etapa recibe la salida de la anterior.

### 4.5 Monitor de trabajos en curso

Una corrutina de fondo dentro del servidor, necesaria por una limitación concreta: **los
workers no pueden avisarle al auditor**. Viven en otros contenedores, y la
`multiprocessing.Queue` solo comunica procesos emparentados de la misma máquina.

El monitor mantiene la lista de los trabajos en vuelo y consulta periódicamente su
estado en el result backend. Cuando detecta que uno terminó, envía el evento
correspondiente al auditor y lo saca de la lista.

### 4.6 Almacenamiento

Tres lugares, tres roles:

- **Volumen compartido (`storage/`)** — los archivos: originales en `uploads/<job_id>/`,
  resultados en `results/<job_id>/`. Montado en el servidor y en todos los workers. Esa
  condición es la que permite que por la cola viajen solo rutas: cuando el worker recibe
  una, puede abrir el archivo directamente.
- **Redis** — el estado **vivo** de las tareas. Efímero: deja de importar poco después
  de que el trabajo termina.
- **SQLite (`jobs.db`)** — el historial **permanente**.

Cada dato en el lugar que le corresponde: archivos pesados al disco, estado transitorio
a Redis, historia a la base de datos.

## 5. Protocolo cliente-servidor (resumen)

Los sockets garantizan que la información llegue íntegra, pero no definen **qué** enviar
ni **cómo interpretarlo**. Eso lo define un **protocolo propio de capa de aplicación**,
construido sobre TCP — el mismo lugar que ocupa HTTP.

La especificación completa está en [04_protocolo.md](04_protocolo.md). Lo esencial:

**Identidad.** Cada `submit` genera un **`job_id`** (UUID v4) que el cliente conserva
para consultar y descargar. Cada archivo producido es un **artefacto** con nombre propio
dentro del trabajo, de modo que el par `(job_id, artifact)` direcciona cualquier archivo
del sistema. `status` devuelve la lista de artefactos disponibles, así que el cliente
nunca adivina qué puede descargar.

**Formato.** Todos los mensajes usan *length-prefixed framing*: 4 bytes con la longitud
del header, header JSON con los datos del pedido, y payload binario opcional cuyo tamaño
se declara en el header. Es necesario porque TCP entrega un flujo continuo sin marcas
que separen un mensaje del siguiente.

**Diálogo.** Pedido → respuesta, siempre iniciado por el cliente, uno por vez. Una
conexión por ejecución del cliente, que puede transportar varios pedidos. Cuatro tipos:
`submit`, `status`, `download`, `history`.

**Por qué protocolo propio y no HTTP**: HTTP corre sobre sockets, así que usarlo no
eliminaría esa capa sino que la ocultaría — y es justamente la capa que el trabajo debe
demostrar. Además el conjunto de operaciones es chico y cerrado, y la transferencia
binaria directa evita el 33% de sobrecarga que impondría base64.


## 6. Mensajes hacia los workers

Por la cola viajan **rutas y parámetros, nunca imágenes**. El broker está diseñado para
mensajes de control de unos pocos bytes; hacerle transportar megabytes lo convertiría en
el cuello de botella del sistema.

Mensaje de ida (servidor → worker):

```json
{
  "task": "tasks.anonymize",
  "id": "c1f2a9…",
  "args": ["a3f7b2…", "/storage/uploads/a3f7b2…/foto.jpg", "/storage/results/a3f7b2…"],
  "kwargs": {"mode": "blur", "strength": 15}
}
```

La operación **es el nombre de la tarea** (`tasks.anonymize`, `tasks.compress`), no un
argumento: hay una tarea por operación.

Resultado de vuelta (worker → result backend):

```json
{"output": "/storage/results/a3f7b2…/out.jpg", "faces_detected": 3, "bytes": 284915}
```

Otra vez rutas y metadatos. El cliente obtiene el archivo por el socket cuando hace
`download`, nunca a través de Redis.

**Condición que esto impone**: la ruta solo sirve si ambos lados ven el mismo sistema de
archivos, y por eso el volumen está montado en el servidor y en los workers. Es el
límite de escalabilidad conocido del diseño: para distribuir workers en máquinas sin ese
volumen habría que usar almacenamiento en red (NFS) o de objetos (S3).

## 7. Modelo de datos (SQLite)

Dos tablas: los trabajos y sus eventos. Las estadísticas no se almacenan — se calculan
con consultas cuando se piden, evitando datos duplicados que puedan quedar
inconsistentes.

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

## 8. Módulos del código

```
final_comp2/
├── docs/
├── app/
│   ├── common/
│   │   ├── protocol.py      # framing (pack/unpack de frames), tipos de mensaje,
│   │   │                    #   constantes — único lugar donde se define el protocolo
│   │   └── config.py        # rutas de storage, límites (tamaño máx.), defaults
│   ├── client/
│   │   └── client.py        # argparse + asyncio streams; una función por acción
│   ├── server/
│   │   ├── main.py          # argparse, arranque: lanza auditor, instala manejo de
│   │   │                    #   señales, inicia el event loop
│   │   ├── server.py        # asyncio.start_server + un handler por tipo de mensaje
│   │   ├── jobs.py          # puente con Celery: encolar, consultar estado,
│   │   │                    #   corrutina de monitoreo de trabajos en vuelo
│   │   └── auditor.py       # proceso hijo: bucle sobre mp.Queue, escritura SQLite,
│   │                        #   consultas de historial
│   └── worker/
│       ├── celery_app.py    # instancia y configuración de Celery
│       └── tasks.py         # tareas Pillow/OpenCV, una por operación + sanitize
├── storage/                 # volumen compartido (uploads/, results/)
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

**Regla de dependencias**: `client` y `server` solo comparten `common/`; `worker` no
importa nada del servidor (solo `common/config`). Esto garantiza que cada componente
pueda desplegarse por separado, que es la condición de un sistema distribuido.

## 9. Flujo completo de un trabajo

1. El cliente parsea los argumentos, valida el archivo y abre el socket TCP.
2. Envía el pedido `submit` (header JSON + bytes de la imagen).
3. El servidor —una corrutina por cliente— lee el pedido sin bloquear el loop, valida,
   y guarda el original en `storage/uploads/<job_id>/`.
4. Encola la tarea en Celery: queda un mensaje chico en Redis con la ruta y los
   parámetros.
5. Notifica `received` + `queued` al auditor por la `mp.Queue` y responde el `job_id`
   al cliente. **Hasta acá, milisegundos.**
6. Un worker toma la tarea, lee la imagen del volumen, la procesa, escribe el resultado
   en `storage/results/<job_id>/` y reporta al result backend.
7. El monitor del servidor detecta el final y avisa al auditor, que lo persiste en
   SQLite.
8. El cliente consulta `status` y descarga con `download` (o todo junto con `--wait`).

## 10. Cumplimiento de requisitos

| Requisito obligatorio | Dónde se cumple |
|---|---|
| Sockets, clientes múltiples concurrentes | `server.py` — `asyncio.start_server` sobre TCP |
| Mecanismos de IPC | `mp.Queue` entre servidor y `auditor.py` |
| Asincronismo de I/O | asyncio en el servidor y en el cliente (`--wait`) |
| Cola de tareas distribuidas | Celery + Redis, tareas en `tasks.py` |
| Parseo de argumentos CLI | argparse en `client.py` y `main.py` |

| Adicional | Dónde |
|---|---|
| Despliegue en contenedores | `docker-compose.yml` (server, redis, workers, flower) |
| Base de datos | SQLite, escrita por el auditor |
| Celery para tareas en paralelo | workers escalables + `sanitize` encadenado |
| Entorno visual | Flower, panel web de la cola (opcional) |
