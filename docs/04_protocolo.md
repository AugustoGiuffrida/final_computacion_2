# Protocolo de comunicación cliente-servidor

Especificación del diálogo entre el cliente y el servidor: cómo se identifican los
trabajos y sus archivos, qué formato tienen los mensajes y cómo se intercambian.

---

## 1. Posición en el modelo de capas

El protocolo que definimos es un **protocolo de capa de aplicación**, construido sobre
TCP. Ocupa el mismo lugar que HTTP, FTP o SMTP: todos ellos son protocolos de
aplicación que usan TCP como transporte.

```
┌───────────────────────────────────────────────┐
│ Aplicación   │  NUESTRO PROTOCOLO             │  ← lo que definimos nosotros
│              │  (equivalente a HTTP, FTP…)    │
├───────────────────────────────────────────────┤
│ Transporte   │  TCP                           │  ← lo provee el sistema operativo
├───────────────────────────────────────────────┤
│ Red          │  IP                            │
├───────────────────────────────────────────────┤
│ Enlace       │  Ethernet / Wi-Fi              │
└───────────────────────────────────────────────┘
```

El reparto de responsabilidades es el siguiente. **TCP** garantiza que los datos lleguen
completos, en orden y sin duplicados, pero entrega un flujo continuo sin ninguna noción
de "mensaje". **Nuestro protocolo** define qué se puede pedir, cómo se codifica cada
pedido, cómo se delimita un mensaje del siguiente y qué respuestas existen.

Es decir: TCP resuelve el transporte, nosotros resolvemos el significado.

---

## 2. Protocolo propio, no HTTP

**Decisión: se implementa un protocolo propio sobre TCP.**

### Comparación

| | Protocolo propio | HTTP |
|---|---|---|
| Transporte | TCP | TCP |
| Envío de imágenes | binario crudo | `multipart/form-data` o base64 (+33% de tamaño) |
| Mensajes definidos | 4 tipos, exactamente los que se usan | método + URL + cabeceras + códigos de estado |
| Interoperabilidad | solo nuestro cliente | cualquier navegador o herramienta |
| Trabajo de implementación | hay que escribir el framing | resuelto por la biblioteca |
| Visibilidad de la capa de sockets | explícita | oculta |

### Justificación

**El objetivo de la materia es la capa de sockets.** Un framework HTTP la ocultaría por
completo, y con ella el manejo de conexiones concurrentes, el framing y el control del
flujo de bytes — que es justamente lo que el trabajo debe demostrar.

**El conjunto de operaciones es cerrado y chico.** Son cuatro pedidos: enviar, consultar,
descargar, listar. HTTP aporta un modelo general (métodos, rutas, cabeceras,
negociación de contenido, códigos de estado) que acá quedaría casi todo sin usar.

**La transferencia binaria es más directa.** La imagen viaja tal cual, sin
transformaciones. Por HTTP habría que envolverla en `multipart/form-data` o codificarla
en base64, que la agranda un tercio sin ningún beneficio.

**Costo asumido**: se pierde interoperabilidad — un navegador no puede hablar con
nuestro servidor. Como el cliente es parte del proyecto, no nos afecta. Si en el futuro
se quisiera una interfaz web, la vía natural sería agregar un adaptador HTTP delante,
sin tocar el núcleo.

---

## 3. Modelo de identidad

Esta sección responde: **cómo se identifica una imagen y cómo un cliente la descarga.**

### 3.1 El identificador de trabajo (`job_id`)

Cuando el servidor acepta un `submit`, genera un **`job_id`: un UUID versión 4**
(aleatorio de 122 bits, por ejemplo `a3f7b2c1-9e4d-4b8a-b3c7-1f2e5d8a9c40`). Lo devuelve
en la respuesta, y es **la única referencia que el cliente necesita conservar**: con él
consulta el estado, descarga los resultados y aparece en el historial.

El `job_id` organiza también el almacenamiento, de forma que cada trabajo tiene su
espacio propio y no hay colisiones de nombres entre usuarios:

```
storage/
├── uploads/
│   └── a3f7b2c1-…/
│       └── foto.jpg              ← original, con su nombre tal como llegó
└── results/
    └── a3f7b2c1-…/
        └── out.jpg               ← resultado
```

**Por qué UUID y no un número secuencial.** Hay dos razones, y conviene tener las dos.

La primera es de **seguridad**: un contador secuencial es adivinable. Si mi trabajo es el
número 41, puedo pedir el 40 y el 42 y ver los de otros. Un UUID v4 es aleatorio y no se
puede enumerar.

La segunda es de **arquitectura distribuida**: un contador secuencial necesita un punto
central que lo lleve, y por lo tanto coordinación —una consulta a la base de datos o un
lock— en el camino crítico de cada `submit`. Un UUID se genera localmente, sin
coordinación con nadie, y sigue siendo único aunque mañana haya varios servidores. Es
coherente con el resto del diseño, donde ningún componente necesita un coordinador
central.

### 3.2 Un trabajo, un archivo

**Cada trabajo produce a lo sumo un archivo de salida**, y el `job_id` alcanza para
identificarlo. No hace falta un segundo nivel de direccionamiento.

| Operación | Archivo de salida | Datos que devuelve |
|---|---|---|
| `inspect` | **ninguno** | el informe de privacidad completo |
| `anonymize` | la imagen procesada | cuántas caras detectó |
| `clean` | la imagen procesada | qué metadatos eliminó |
| `convert` | la imagen convertida | — |
| `compress` | la imagen comprimida | tamaño original y final |
| `sanitize` | la imagen saneada | resumen de las tres etapas |

Los **datos** que devuelve cada operación —cuántas caras, qué metadatos se borraron, el
informe de `inspect`— son unos pocos cientos de bytes y viajan en el campo `result` de la
respuesta de `status`. No son archivos y no se descargan.

Por eso `inspect` es un caso particular: **no genera nada para descargar**. Su resultado
completo llega en la consulta de estado y el cliente lo muestra por pantalla.

**Si en el futuro una operación produjera varios archivos** —por ejemplo, miniaturas en
tres tamaños—, bastaría con agregar un campo `artifact` opcional al `download`. Como el
header es JSON, ese agregado no rompería ningún cliente existente. No se incluye ahora
porque ninguna operación de la v1 lo necesita.

### 3.3 Propiedad: quién puede descargar qué

Cada trabajo queda asociado al **usuario que lo creó**, dato que el auditor persiste en
la tabla `jobs`. El servidor aplica una regla simple en `status`, `download` e
`history`: **solo sirve trabajos cuyo `user` coincida con el del pedido**. Si no
coincide, responde `FORBIDDEN`.

**Limitación conocida y asumida.** El usuario se **declara** por línea de comandos
(`--user augusto`), no se autentica: nada impide que alguien pase el nombre de otro. Por
lo tanto esta regla delimita responsabilidades y evita accesos accidentales, pero **no
es un control de seguridad real**.

Lo que sí aporta una barrera efectiva es que el `job_id` sea un UUID aleatorio: aun
declarando el nombre de otro usuario, haría falta conocer el identificador exacto de un
trabajo ajeno, que no es enumerable.

Para un control real haría falta autenticación con credenciales y un token por sesión;
está fuera del alcance de esta versión y documentado en `TODO.md`.

---

## 4. Formato de los mensajes

### 4.1 El problema a resolver

TCP entrega un **flujo continuo de datos, sin marcas de separación**: lo que el emisor
envía en tres operaciones puede llegar en una sola lectura, o al revés. Si el cliente
manda un pedido seguido de una imagen, el servidor recibe todo pegado y no tiene forma
natural de saber dónde termina uno y empieza la otra, ni cuándo dejar de leer.

Todo protocolo sobre TCP tiene que resolver esto de alguna manera. HTTP lo hace con
líneas de texto y la cabecera `Content-Length`. Nosotros usamos **prefijo de longitud**
(*length-prefixed framing*), que es más simple y más directo para datos binarios.

### 4.2 Estructura del frame

Todos los mensajes, en ambas direcciones, tienen la misma estructura:

```
 0        4                    4+H                  4+H+P
 ├────────┼─────────────────────┼────────────────────┤
 │ 4 bytes│  header JSON (UTF-8)│  payload binario   │
 │  u32   │      H bytes        │      P bytes       │
 │  BE    │                     │    (opcional)      │
 └────────┴─────────────────────┴────────────────────┘
     ↑              ↑                     ↑
  longitud     qué se pide          la imagen, cruda
  del header   o se responde        (P = payload_size)
```

1. **Prefijo (4 bytes)**: entero sin signo, big-endian (orden de red). Indica cuántos
   bytes mide el header. Es de tamaño fijo, y por eso se puede leer siempre primero sin
   ambigüedad.
2. **Header (JSON, UTF-8)**: los datos del mensaje. Siempre contiene `type` y
   `payload_size`; los pedidos incluyen además `user`.
3. **Payload (binario, opcional)**: el contenido del archivo, tal cual. Mide exactamente
   lo que declara `payload_size` — que vale `0` cuando no hay payload.

**Por qué JSON en el header y binario en el payload.** El header transporta datos
estructurados y variables: JSON se lee sin herramientas, se extiende agregando campos
sin romper compatibilidad, y Python lo serializa en una línea. El payload va crudo
porque es lo más eficiente posible: cualquier codificación textual lo agrandaría sin
beneficio alguno. Cada parte usa el formato que le conviene.

### 4.3 Ejemplo de mensaje completo, byte a byte

Un `submit` con una imagen de 3.145.728 bytes:

```
bytes 0-3      00 00 00 8E                       ← el header mide 142 bytes
bytes 4-145    {"type":"submit","user":"augusto",
                "op":"anonymize",
                "params":{"mode":"blur","strength":15},
                "filename":"foto.jpg",
                "payload_size":3145728}          ← 142 bytes de JSON
bytes 146-…    FF D8 FF E0 00 10 4A 46 …         ← 3.145.728 bytes de JPEG
```

Total transmitido: 4 + 142 + 3.145.728 bytes. El receptor sabe exactamente dónde termina
cada parte antes de empezar a leerla.

### 4.4 Algoritmo de lectura

Es el mismo en los dos extremos, y es la clave de todo el protocolo:

```python
async def read_frame(reader):
    # 1. Leer el prefijo: siempre 4 bytes
    raw = await reader.readexactly(4)
    header_len = int.from_bytes(raw, 'big')
    if header_len > MAX_HEADER:              # protección contra valores absurdos
        raise ProtocolError('header demasiado grande')

    # 2. Leer el header: exactamente los bytes que anunció el prefijo
    header = json.loads(await reader.readexactly(header_len))

    # 3. Leer el payload: exactamente lo que declara payload_size
    n = header.get('payload_size', 0)
    if n > MAX_PAYLOAD:
        raise ProtocolError('payload demasiado grande')
    payload = await reader.readexactly(n) if n else b''

    return header, payload
```

La pieza esencial es **`readexactly(n)`**: sigue leyendo hasta juntar exactamente `n`
bytes, sin importar en cuántos pedazos lleguen. Es lo que resuelve el problema del flujo
continuo. Si la conexión se corta antes de completarlos, lanza una excepción, que es la
forma en que el servidor detecta una desconexión a mitad de un envío.

**Payloads grandes.** Para el `submit`, el servidor no acumula la imagen entera en
memoria: la lee en bloques de 64 KB y los escribe directamente en el archivo de destino.
Así el uso de memoria queda acotado sin importar el tamaño de la imagen, y el `await` de
cada bloque le da al event loop oportunidades frecuentes de atender a otros clientes.

---

## 5. Catálogo de mensajes

### 5.1 `submit` — enviar una imagen a procesar

**Pedido** (con payload):

```json
{"type": "submit", "user": "augusto", "op": "anonymize",
 "params": {"mode": "blur", "strength": 15},
 "filename": "foto.jpg", "payload_size": 3145728}
```

**Respuesta** (sin payload):

```json
{"type": "ok", "job_id": "a3f7b2c1-9e4d-4b8a-b3c7-1f2e5d8a9c40",
 "status": "QUEUED", "payload_size": 0}
```

El servidor valida, guarda la imagen, encola la tarea y responde **de inmediato**: no
espera al resultado. Esa respuesta rápida es lo que le permite seguir atendiendo a los
demás clientes.

### 5.2 `status` — consultar el estado de un trabajo

**Pedido**:

```json
{"type": "status", "user": "augusto", "job_id": "a3f7b2c1-…", "payload_size": 0}
```

**Respuesta** (trabajo terminado, con archivo para descargar):

```json
{"type": "ok", "job_id": "a3f7b2c1-…", "status": "DONE", "has_output": true,
 "result": {"faces_detected": 3, "bytes": 284915, "content_type": "image/jpeg"},
 "payload_size": 0}
```

**Respuesta** (`inspect`, que no genera archivo):

```json
{"type": "ok", "job_id": "b8e1d4f2-…", "status": "DONE", "has_output": false,
 "result": {"faces_detected": 2, "gps": {"lat": -32.889, "lon": -68.845},
            "taken_at": "2026-07-04T18:22:10", "camera": "iPhone 14",
            "bytes": 3145728},
 "payload_size": 0}
```

**Respuesta** (todavía procesando):

```json
{"type": "ok", "job_id": "a3f7b2c1-…", "status": "PROCESSING", "payload_size": 0}
```

**Respuesta** (falló):

```json
{"type": "ok", "job_id": "a3f7b2c1-…", "status": "ERROR",
 "error": "formato de imagen no soportado", "payload_size": 0}
```

Estados posibles: `QUEUED`, `PROCESSING`, `DONE`, `ERROR`. Los campos `has_output` y
`result` aparecen solo cuando el estado es `DONE`: el primero le dice al cliente si tiene
sentido pedir la descarga, el segundo trae los datos que produjo la operación.

### 5.3 `download` — descargar el resultado

**Pedido**:

```json
{"type": "download", "user": "augusto", "job_id": "a3f7b2c1-…", "payload_size": 0}
```

El `job_id` alcanza: cada trabajo tiene a lo sumo un archivo de salida.

**Respuesta** (con payload):

```json
{"type": "ok", "job_id": "a3f7b2c1-…", "filename": "foto_anonymized.jpg",
 "content_type": "image/jpeg", "payload_size": 284915}
```

seguida de los 284.915 bytes del archivo. El campo `filename` es un nombre sugerido; el
cliente puede guardarlo donde indique `-o`.

Es el pedido inverso al `submit`: sin payload de ida, con payload de vuelta.

### 5.4 `history` — listar los trabajos del usuario

**Pedido**:

```json
{"type": "history", "user": "augusto", "limit": 10, "payload_size": 0}
```

**Respuesta**:

```json
{"type": "ok", "jobs": [
   {"job_id": "a3f7b2c1-…", "op": "anonymize", "status": "DONE",
    "filename": "foto.jpg", "created_at": "2026-08-13T10:22:41",
    "finished_at": "2026-08-13T10:22:48"},
   {"job_id": "b8e1d4f2-…", "op": "compress", "status": "ERROR",
    "filename": "captura.png", "created_at": "2026-08-13T09:15:03",
    "error": "imagen corrupta"}
 ], "payload_size": 0}
```

Es el único pedido que se resuelve contra SQLite. El servidor **lee la base
directamente, en modo solo lectura**, con `asyncio.to_thread(...)` para no bloquear el
event loop. No le pregunta al auditor: la `mp.Queue` es de una sola dirección
(servidor → auditor) y agregar un canal de vuelta obligaría a numerar pedidos y
respuestas sin ninguna ganancia.

Esto no contradice que el auditor sea el único escritor. **SQLite admite muchos lectores
simultáneos**; lo que no admite son escritores concurrentes. El diseño respeta
exactamente ese modelo: un solo escritor (el auditor) y los lectores que hagan falta.

La consulta filtra por el usuario del pedido, así que cada cliente ve solo sus propios
trabajos, ordenados del más reciente al más antiguo.

---

## 6. Cómo funciona cada operación, paso a paso

### 6.1 De dónde saca el servidor la información de un trabajo

Las tres consultas (`status`, `download`, `history`) empiezan por lo mismo: resolver el
`job_id`. El servidor tiene **tres fuentes**, y cada una cumple un papel distinto.

| Fuente | Qué contiene | Para qué se usa |
|---|---|---|
| **Índice en memoria** | los trabajos aceptados desde que el servidor arrancó: usuario, operación, estado y ruta de salida | resolver el caso común, sin tocar el disco |
| **SQLite** (solo lectura) | el historial completo y permanente | trabajos de ejecuciones anteriores del servidor, y el `history` |
| **Redis** (result backend) | el estado vivo de las tareas en curso | distinguir `QUEUED` de `PROCESSING` |

**Orden de resolución**: primero el índice en memoria; si no está ahí, SQLite; si tampoco,
`JOB_NOT_FOUND`. Recién con el trabajo resuelto se verifica que **pertenezca al usuario**
que pregunta.

#### Por qué existe el índice en memoria

No es una optimización: **resuelve una condición de carrera concreta**.

Cuando el servidor acepta un `submit`, deposita el evento en la cola del auditor y
responde de inmediato, **sin esperar** a que el auditor escriba en SQLite — esa es
justamente la razón de ser del IPC asíncrono. Pero con `--wait` el cliente consulta el
estado un segundo después. Si el servidor buscara solo en SQLite, es perfectamente
posible que la fila todavía no esté escrita y respondiera `JOB_NOT_FOUND` **sobre un
trabajo que él mismo acaba de aceptar**.

El índice cierra esa ventana: el servidor conoce sus propios trabajos sin depender de que
el auditor ya los haya persistido. Las entradas de trabajos terminados se descartan unos
minutos después, cuando su escritura en SQLite ya está garantizada, de modo que el índice
queda acotado al trabajo reciente y no crece indefinidamente.

#### Cuándo se consulta Redis (y cuándo no)

Solo se consulta **si el trabajo está en curso**. Un trabajo terminado —`DONE` o
`ERROR`— se responde desde el índice o desde SQLite, sin tocar Redis.

No es solo economía de consultas. Los resultados de Celery **expiran** en el result
backend: `result_expires` vale 24 horas por defecto. Un trabajo de la semana pasada ya no
existe en Redis, pero sigue en SQLite y su archivo sigue en el volumen. Si la descarga
dependiera de Redis, **dejaría de funcionar al día siguiente**.

De ahí la división: Redis es el estado *vivo* y efímero; SQLite es la verdad *permanente*.

#### Consecuencia para la descarga

**Para un `download`, Redis no se consulta nunca.** Solo se descargan trabajos terminados,
y tanto su estado como la ruta del archivo salen del índice en memoria o de SQLite. El
servidor resuelve la descarga por completo con esas dos fuentes más el volumen compartido.

### 6.2 Envío de una imagen (`submit`)

**En el cliente:**

1. Verifica que el archivo exista, que la extensión esté soportada y que no supere el
   tamaño máximo. Falla localmente si algo no cierra, sin molestar al servidor.
2. Arma el header con la operación y sus parámetros, y mide el archivo con
   `os.path.getsize()` para completar `payload_size`.
3. Envía el header y luego el archivo **en bloques de 64 KB**, con
   `await writer.drain()` después de cada bloque.

**En el servidor:**

4. Lee el prefijo y el header (algoritmo de la sección 4.4) y valida los campos.
5. Genera el `job_id` (UUID v4) y crea el directorio `storage/uploads/<job_id>/`.
6. Lee el payload **en bloques de 64 KB y los escribe directamente en el archivo**,
   sin acumular la imagen en memoria. Si la conexión se corta antes de completar
   `payload_size`, `readexactly` lanza excepción y el directorio se descarta.
7. Verifica que el archivo sea realmente una imagen abriéndolo con Pillow. Si no lo es,
   responde `INVALID_IMAGE` y borra lo recibido.
8. Encola la tarea en Celery con `asyncio.to_thread(...)`, para que la llamada
   bloqueante a Redis no frene el event loop.
9. Envía los eventos `received` y `queued` al auditor por la `mp.Queue`, y agrega el
   trabajo a su índice en memoria de trabajos en curso.
10. Responde `{job_id, status: "QUEUED"}`.

Los pasos 4 a 10 llevan milisegundos: el servidor **nunca espera** a que la imagen se
procese.

### 6.3 Consulta de estado (`status`)

#### De dónde sale el estado

El estado vive en el **result backend de Celery** (Redis). El servidor lo obtiene con
`AsyncResult(task_id).state`, ejecutado con `asyncio.to_thread(...)` porque implica una
consulta de red a Redis.

Celery maneja sus propios nombres de estado, que el servidor traduce a los cuatro del
protocolo:

| Estado de Celery | Estado del protocolo | Significado |
|---|---|---|
| `PENDING` | `QUEUED` | está en la cola, ningún worker lo tomó |
| `STARTED` | `PROCESSING` | un worker lo está ejecutando |
| `RETRY` | `PROCESSING` | falló de forma transitoria y se está reintentando |
| `SUCCESS` | `DONE` | terminó bien |
| `FAILURE` | `ERROR` | falló definitivamente |

**Dos detalles de configuración que hay que conocer**, porque sin ellos el estado no
funciona como uno esperaría:

- Por defecto Celery **no informa `STARTED`**: pasa directo de `PENDING` a `SUCCESS`, y
  el cliente nunca vería `PROCESSING`. Hace falta `task_track_started = True`.
- `PENDING` en Celery es **ambiguo**: significa tanto "encolado y sin tomar" como
  "no conozco esta tarea". Si un cliente inventara un `job_id`, Redis respondería
  `PENDING` y el servidor informaría `QUEUED` sobre un trabajo inexistente. Por eso el
  servidor **primero verifica que el trabajo exista** —en su índice en memoria o
  consultando SQLite— y solo entonces consulta Redis. Si no existe, responde
  `JOB_NOT_FOUND`.

#### El proceso completo

1. El cliente envía `status` con su `job_id`, **por la misma conexión ya abierta**: no
   se reconecta en cada consulta.
2. El servidor resuelve el `job_id` según la sección 6.1 (índice en memoria → SQLite) y
   verifica que **pertenezca al usuario** que pregunta; si no, responde `JOB_NOT_FOUND`
   o `FORBIDDEN`.
3. Consulta el estado en el result backend y lo traduce según la tabla.
4. Si el estado es `DONE`, agrega `has_output` y el `result` de la operación (caras
   detectadas, metadatos eliminados, informe de `inspect`). Si es `ERROR`, agrega el
   motivo.
5. Responde. Cada consulta cuesta una lectura a Redis, del orden del milisegundo.

#### La política de consulta del cliente

Sin `--wait`, el cliente hace **una sola consulta** y termina: muestra el estado y sale.

Con `--wait`, consulta cada **1 segundo** hasta que el estado sea `DONE` o `ERROR`, con
un límite configurable (`--timeout`, por defecto 300 s). La espera se hace con
`await asyncio.sleep(1)`, sin bloquear.

Si se agota el tiempo, el cliente avisa y **muestra el `job_id`** para consultarlo más
tarde. Rendirse a esperar **no cancela nada**: el trabajo sigue su curso en el worker y
el resultado queda disponible.

#### Por qué consulta periódica y no notificación

Sería posible que el servidor avisara al cliente apenas el trabajo termina, pero eso
obligaría a que el servidor hable sin que le pregunten, y entonces el cliente tendría que
estar escuchando en todo momento mientras además envía pedidos — dos flujos simultáneos
sobre la misma conexión, que habría que distinguir con identificadores.

La consulta periódica mantiene **un único sentido de iniciativa** y hace el protocolo
mucho más simple. El costo es una lectura a Redis por segundo por cliente en espera, que
es despreciable.

#### Una garantía importante del orden

Cuando el estado es `DONE`, **el archivo ya está escrito en el disco**. No es casualidad:
Celery marca la tarea como `SUCCESS` recién cuando la función del worker **retorna**, y la
función escribe el archivo antes de retornar. Por eso no existe la ventana en que el
cliente vea `DONE`, pida la descarga y el archivo todavía no exista.

### 6.4 Descarga del resultado (`download`)

**En el servidor, al recibir el pedido:**

1. Resuelve el `job_id` según la sección 6.1 —índice en memoria, y si no está ahí,
   SQLite— y verifica que **pertenezca al usuario**; si no, `JOB_NOT_FOUND` o
   `FORBIDDEN`. **Redis no interviene** en ningún paso de la descarga.
2. Verifica que el estado sea `DONE`. Si sigue en curso responde `NOT_READY`; si falló,
   `ERROR` con el motivo.
3. Verifica que el trabajo **genere archivo**. `inspect` no genera ninguno: responde
   `NO_OUTPUT` e indica que el resultado está en la consulta de estado.
4. Resuelve la ruta `storage/results/<job_id>/out.<ext>` y obtiene el tamaño exacto con
   `os.path.getsize()`. **Ese número es el `payload_size`** del header: por eso el
   servidor puede anunciar cuánto va a enviar antes de empezar a leer el archivo.
5. Envía el header con `payload_size`, `filename` y `content_type`.
6. Abre el archivo y lo envía **en bloques de 64 KB**. Después de cada bloque hace
   `await writer.drain()`.

**En el cliente:**

7. Lee el prefijo y el header, y de ahí obtiene `payload_size`.
8. Lee exactamente esa cantidad de bytes, **escribiéndolos en disco a medida que
   llegan**, sin acumular el archivo en memoria.
9. Guarda el resultado en la ruta que indique `-o`, o con el `filename` sugerido.

#### Por qué se envía en bloques con `drain()`

Es el punto más fino de la descarga. `writer.write()` **no envía**: copia los datos a un
buffer interno y retorna de inmediato. Si el servidor escribiera un archivo de 20 MB de
una sola vez, ese buffer crecería hasta contenerlo entero, porque el cliente lo consume
mucho más lento de lo que el servidor lo produce. Con muchas descargas simultáneas, la
memoria del servidor se dispararía.

`await writer.drain()` es el freno: suspende la corrutina hasta que el buffer se vacía lo
suficiente. Así el servidor **envía al ritmo que el cliente puede recibir**, la memoria
queda acotada, y —al ser un `await`— cada bloque le devuelve el control al event loop
para atender a los demás clientes.

Es el mismo mecanismo de control de flujo que TCP aplica a nivel de red, pero acá a nivel
de aplicación.

#### Cómo sabe el cliente que llegó todo

Por el `payload_size` del header. `readexactly(payload_size)` no retorna hasta juntar
exactamente esa cantidad de bytes, y si la conexión se corta antes lanza una excepción:
el cliente descarta el archivo parcial e informa el error. **No hace falta un checksum**,
porque TCP ya garantiza la integridad de lo que entrega y el prefijo de longitud detecta
cualquier truncamiento.

#### Casos borde

- **El cliente se desconecta a mitad del envío**: la escritura falla con
  `ConnectionResetError`. El servidor cierra el archivo, libera la conexión y sigue
  atendiendo al resto. El resultado permanece en disco.
- **El archivo no está** (fue borrado por la limpieza periódica): responde `INTERNAL`
  indicando que el resultado ya no está disponible.
- **Dos clientes descargan el mismo trabajo a la vez**: no hay problema, son lecturas
  independientes y cada corrutina abre el archivo por su cuenta.

## 7. Errores

Ante cualquier problema el servidor responde con un mensaje de tipo `error`:

```json
{"type": "error", "code": "JOB_NOT_FOUND",
 "message": "no existe un trabajo con ese identificador", "payload_size": 0}
```

| Código | Cuándo ocurre |
|---|---|
| `BAD_REQUEST` | header mal formado o campos obligatorios ausentes |
| `UNKNOWN_OP` | la operación pedida no existe |
| `INVALID_IMAGE` | el archivo no es una imagen válida o el formato no está soportado |
| `TOO_LARGE` | la imagen excede el tamaño máximo configurado |
| `JOB_NOT_FOUND` | no existe un trabajo con ese `job_id` |
| `FORBIDDEN` | el trabajo pertenece a otro usuario |
| `NOT_READY` | se pidió descargar un trabajo que aún no terminó |
| `NO_OUTPUT` | se pidió descargar un trabajo que no genera archivo (`inspect`) |
| `INTERNAL` | error inesperado del servidor |

**Un error nunca cierra la conexión**: es una respuesta como cualquier otra, y el cliente
puede seguir enviando pedidos. Cerrar sería más simple de programar, pero le impediría
distinguir entre un pedido rechazado y una caída del servidor.

---

## 8. Traza completa de una sesión

`submit --wait`, que ejercita el protocolo entero sobre una única conexión:

```
Cliente                                             Servidor
   │                                                   │
   │═══ conexión TCP a 192.168.0.10:9000 ═════════════►│  accept() → socket dedicado
   │                                                   │
   │──► [4B: 142] [header submit] [3 MB de JPEG] ─────►│  lee en bloques de 64 KB
   │                                                   │  valida la imagen
   │                                                   │  guarda uploads/a3f7…/foto.jpg
   │                                                   │  genera job_id (UUID v4)
   │                                                   │  encola la tarea en Celery
   │                                                   │  avisa al auditor (mp.Queue)
   │◄── [4B: 78] [{"type":"ok","job_id":"a3f7…",       │
   │             "status":"QUEUED"}] ──────────────────│  (todo esto: milisegundos)
   │                                                   │
   │──► [status a3f7…] ───────────────────────────────►│  consulta el result backend
   │◄── [{"status":"PROCESSING"}] ─────────────────────│
   │   await asyncio.sleep(1)                          │
   │──► [status a3f7…] ───────────────────────────────►│
   │◄── [{"status":"DONE","has_output":true,           │
   │      "result":{"faces_detected":3,…}}] ───────────│
   │                                                   │
   │──► [download a3f7…] ─────────────────────────────►│  abre results/a3f7…/out.jpg
   │◄── [4B: 96] [header] [284.915 B de JPEG] ─────────│  lo envía en bloques
   │                                                   │
   │   escribe el archivo local                        │
   │═══ cierra la conexión ═══════════════════════════►│  EOF → cierra y libera
```

Mientras esta conversación transcurre, el servidor atiende a los demás clientes: cada
`await` de lectura o escritura le devuelve el control al event loop.

---

## 9. Reglas del diálogo, límites y casos borde

**Un pedido por vez.** El cliente envía un pedido y espera la respuesta completa antes de
enviar el siguiente. Como consecuencia, **no hacen falta identificadores de correlación**
en los mensajes: cada respuesta corresponde necesariamente al último pedido. Si se
quisieran pedidos simultáneos sobre una misma conexión habría que numerarlos y que el
servidor devolviera ese número en cada respuesta; no lo necesitamos, porque el
paralelismo del sistema está en los workers, no en la conexión.

**El cliente siempre inicia.** El servidor nunca envía nada por su cuenta, solo responde.
Por eso el cliente consulta el estado periódicamente en vez de esperar un aviso: mantener
un único sentido de iniciativa simplifica ambos extremos.

**Una conexión por ejecución.** Se abre al arrancar el cliente y se cierra al terminar.
Durante ese lapso puede transportar varios pedidos.

**Límites configurables**: tamaño máximo de header (64 KB) y de payload (por defecto
25 MB). Ambos se verifican **antes** de leer, para que un valor absurdo en el prefijo no
haga que el servidor intente reservar memoria de más.

**Desconexión a mitad de un envío**: `readexactly` lanza excepción, la corrutina de ese
cliente cierra su socket y libera los recursos. No afecta a las demás conexiones.

**Desconexión con un trabajo en curso**: el trabajo **sigue su curso**. El worker no sabe
ni le importa si quien lo pidió continúa conectado, y el resultado queda disponible para
cuando el cliente vuelva a consultarlo con su `job_id`.

**Trabajo terminado, cliente que nunca vuelve**: los resultados quedan en el volumen. La
limpieza periódica de resultados viejos está prevista como tarea programada de Celery
Beat y documentada en `TODO.md`.
