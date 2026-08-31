# Chuleta para la defensa: las ideas y sus llamadas

El proyecto usa 43 llamadas de biblioteca distintas, pero salen de **ocho ideas**. Cada
sección tiene la idea en una frase, las llamadas que genera y dónde encontrarlas. Si se
entiende la frase, las llamadas son consecuencias.

---

## 1. El event loop no puede bloquearse

Un solo hilo atiende N clientes: las corrutinas se turnan en cada `await`. Si una llama a
algo bloqueante, se frena el hilo entero y con él todos los clientes.

| Llamada | Qué hace | Dónde |
|---|---|---|
| `asyncio.run(...)` | arranca el event loop y ejecuta la corrutina principal | `main/cli.py` — `main` |
| `asyncio.start_server(handler, host, port)` | abre los sockets de escucha; crea una corrutina por cliente | `image_server.py` — `start` |
| `await asyncio.sleep(t)` | duerme **esta** corrutina y cede el control | receptor y esperas de `intake_channel.py` |
| `asyncio.create_task(corr())` | pone una corrutina a correr "al lado", sin esperarla | `intake_channel.py` — `start` |
| `asyncio.wait_for(x, t)` | espera con tope; al vencer cancela y levanta `TimeoutError` | `intake_channel.py` — `review` |
| `asyncio.get_running_loop()` | devuelve el loop actual (para `create_future`) | `intake_channel.py` — `review` |

**Si preguntan** por qué no hilos: la consigna pide asincronismo de I/O; y con corrutinas
el estado compartido no necesita candados, porque solo se cede el control en los `await`.

---

## 2. TCP es un flujo sin marcas: hay que delimitar mensajes

TCP entrega bytes corridos, sin decir dónde termina un mensaje y empieza el otro. Por eso
cada mensaje viaja con su longitud adelante (framing).

| Llamada | Qué hace | Dónde |
|---|---|---|
| `reader.readexactly(n)` | lee exactamente n bytes o levanta `IncompleteReadError` | `protocol.py` — `receive_header` |
| `writer.write(bytes)` + `await writer.drain()` | encola bytes y frena al emisor si el receptor no da abasto | `protocol.py` — `send_message` |
| `asyncio.open_connection(host, port)` | el lado cliente: conecta y devuelve reader/writer | `client/session.py` — `connect` |

**Si preguntan** qué pasa sin framing: el mensaje siguiente se leería corrido, empezando
en el medio del anterior, sin forma de detectarlo.

---

## 3. Esperar un valor que llega "desde afuera": el Future

La respuesta del hijo no viene de llamar una función: aparece después, sacada del pipe.
Un `Future` es una caja: la corrutina espera la caja, otro se la llena.

| Llamada | Qué hace | Dónde |
|---|---|---|
| `loop.create_future()` | crea la caja vacía | `intake_channel.py` — `review` |
| `future.set_result(x)` | la llena y programa despertar al que espera | `intake_channel.py` — `_deliver` |
| el diccionario `_pending` | correlaciona: muchas cajas en vuelo, la respuesta trae el `job_id` que dice cuál es la suya | `intake_channel.py` |

**Si preguntan** por qué hay correlación si el hijo atiende de a uno: los pedidos quedan
encolados en el pipe; la correlación permite tener varios en vuelo sin esperar turno para
*enviar*.

---

## 4. Dos procesos emparentados, un pipe

El hijo abre contenido no confiable; si muere, no arrastra al servidor. Se comunican por
un pipe: descriptor del sistema operativo, dos extremos, `pickle` por dentro.

| Llamada | Qué hace | Dónde |
|---|---|---|
| `multiprocessing.get_context("spawn")` | el hijo arranca limpio, sin heredar loop ni sockets | `intake_channel.py` — `__init__` |
| `context.Pipe()` | crea los dos extremos conectados | `intake_channel.py` — `_open_channel` |
| `context.Process(target=..., args=..., daemon=True)` | lanza el hijo; `daemon` evita huérfanos | `intake_channel.py` — `_launch_process` |
| `connection.send(x)` / `connection.recv()` | serializa con pickle y cruza / bloquea hasta que haya algo | ambos lados |
| `connection.poll()` | ¿hay algo? contesta al instante — es lo que permite esperar sin hilo | `intake_channel.py` — `_receive_loop` |
| `EOFError` en `recv()` | el otro extremo cerró: el proceso de enfrente murió | ambos lados |

**Si preguntan** por qué pipe y no `mp.Queue`: son exactamente dos procesos; la Queue es
un pipe + candado + hilo alimentador, para el caso de muchos productores que no tenemos.
(Material de la cátedra, Clase 8.)

---

## 5. Las señales avisan; matar es otra cosa

| Llamada | Qué hace | Dónde |
|---|---|---|
| `loop.add_signal_handler(SIGTERM, ...)` | ante la señal, encender un `asyncio.Event` para apagar ordenado | `main/cli.py` — `install_shutdown_handlers` |
| `signal.signal(SIGINT, SIG_IGN)` | el hijo ignora Ctrl-C: la señal va a todo el grupo, y él debe terminar solo cuando se lo pidan por el pipe | `intake/process.py` — `run_intake` |
| `process.terminate()` | SIGTERM: pedido enérgico, se puede atender | `intake_channel.py` — `stop` |
| `kill -9` (SIGKILL) | no se puede atrapar; código de salida **-9** = "lo mataron con la 9" | demo, paso de recuperación |

**Regla:** código 0 = terminó solo; negativo = lo terminaron (la señal, en negativo).

---

## 6. SQLite: un escritor, muchos lectores

| Llamada | Qué hace | Dónde |
|---|---|---|
| `sqlite3.connect(path)` | abre (y crea) la base | `database.py` — `JobWriter` |
| `PRAGMA journal_mode = WAL` | el servidor lee mientras el hijo escribe, sin esperarse | `database.py` — `JobWriter` |
| `connect("file:...?mode=ro", uri=True)` | solo lectura: SQLite **rechaza** escrituras del servidor | `database.py` — `JobReader` |
| `executescript(schema)` | crea las tablas desde `schema.sql`; idempotente | `database.py` — `JobWriter` |
| `row_factory = sqlite3.Row` | filas accesibles por nombre de columna | `database.py` — `JobReader` |

**Si preguntan** por qué no MySQL/Postgres: un único escritor por diseño; un servidor de
BD resolvería un problema (escritores concurrentes) que este sistema no tiene.

---

## 7. Lo bloqueante va a un hilo del pool

Cuando una biblioteca no tiene versión asíncrona, su llamada bloqueante se manda a un
hilo con `asyncio.to_thread(funcion, args)`: devuelve una corrutina esperable y el event
loop sigue atendiendo mientras el hilo espera.

| Llamada | Qué espera | Dónde |
|---|---|---|
| `to_thread(task.delay, …)` | publicar en Redis | `jobs.py` — `enqueue` |
| `to_thread(self._poll_backend)` | consultar el estado en Redis | `jobs.py` — `_watch` |
| `to_thread(self._process.join, t)` | que el proceso hijo termine | `intake_channel.py` — `_wait_for_child` |

**Si preguntan** por qué acá sí hay hilos y en el resto no: son del pool de asyncio, se
usan solo para envolver llamadas bloqueantes ajenas (el cliente de Redis, `join`), y no
comparten estado — no hay candados propios en todo el proyecto.

La fecha real, cuando hace falta (`created_at`), es `datetime.now(timezone.utc)` —
`registry.py` y `database.py`.

---

## 8. No confiar en contenido ajeno

| Llamada | Qué hace | Dónde |
|---|---|---|
| `Image.open` + `verify()` **y de nuevo** + `load()` | estructura y píxeles: a un JPEG le faltan 5 bytes y `verify()` no lo ve, `load()` sí | `intake/process.py` — `verify_image` |
| `Image.DecompressionBombError` | archivo chico que se infla a imagen gigante (agota memoria) | `intake/process.py` — `verify_image` |
| `hashlib.sha256` + lectura por bloques | identidad del contenido, memoria acotada | `intake/process.py` — `hash_of` |
| `Path(nombre).name` | defensa contra `../../etc/passwd` como nombre de archivo | `incoming.py` — `safe_filename` |

**La regla madre:** el proceso principal **nunca abre una imagen**. Todo esto corre en el
hijo, que puede morir sin arrastrar a nadie.

---

## El camino de un `submit`, llamada por llamada

Para practicar en voz alta — es el recorrido de la demostración:

```
 1. readexactly(4) + readexactly(n)      el servidor lee el header       [protocol]
 2. require_user / safe_filename / …     valida sin tocar el payload     [incoming]
 3. stream_payload → write por bloques   guarda la imagen sin cargarla   [incoming.save_upload]
 4. connection.send(ReviewRequest)       el pedido cruza al hijo         [intake_channel.review]
 5. create_future + _pending[job_id]     la caja queda esperando         [intake_channel.review]
 6. connection.recv()                    el hijo lo recibe               [process.run_intake]
 7. Image.open ×2 (verify, load)         ¿es una imagen de verdad?       [process.verify_image]
 8. sha256 por bloques                   la identidad del contenido      [process.hash_of]
 9. SELECT … / INSERT …                  ¿duplicado? si no, persiste     [database.JobWriter]
10. connection.send(ReviewResponse)      el veredicto vuelve             [process]
11. poll() → recv() → set_result()       la caja se llena                [intake_channel._receive_loop]
12. wait_for despierta con el veredicto  review() retorna                [intake_channel.review]
13. send_message({ok, job_id, …})        la respuesta viaja al cliente   [image_server.handle_submit]
```
