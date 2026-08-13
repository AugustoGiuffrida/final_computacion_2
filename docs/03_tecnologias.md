# Tecnologías utilizadas

Este documento explica **qué es** cada tecnología del proyecto y **por qué se eligió**
frente a sus alternativas. La descripción de cómo se combinan entre sí está en
[02_arquitectura.md](02_arquitectura.md).

---

## 1. Sockets TCP

### Qué es un socket

Un socket es un **punto de conexión**: la representación, dentro de un programa, de un
extremo de una comunicación. El sistema operativo lo entrega como un descriptor, igual
que un archivo abierto, y el programa lo usa de forma parecida — escribe en él para
enviar información y lee de él para recibirla. Todo el trabajo sucio (partir la
información en paquetes, enviarlos por la placa de red, reordenarlos al llegar,
reclamar los que se perdieron) queda del lado del kernel.

La analogía útil es el teléfono: el servidor deja una línea abierta esperando llamadas,
el cliente marca el número, y una vez establecida la comunicación cualquiera de los dos
puede hablar y escuchar.

### Direcciones y puertos

Para que un cliente encuentre al servidor hacen falta **dos datos, no uno**:

- La **dirección IP** identifica a la *máquina* dentro de la red.
- El **puerto** identifica al *programa* dentro de esa máquina.

El puerto existe porque una misma computadora corre muchos servicios de red al mismo
tiempo: un servidor web, una base de datos, SSH. Todos comparten la misma IP. Cuando
llega información desde afuera, el sistema operativo necesita saber **a cuál de todos
entregársela**, y el número de puerto es exactamente ese dato de ruteo interno.
Siguiendo la analogía telefónica: la IP es el número de la empresa, el puerto es el
número de interno.

Un puerto es un número de 16 bits (0 a 65535). Los menores a 1024 están reservados para
servicios estándar (80 HTTP, 443 HTTPS, 22 SSH) y en Linux requieren privilegios de
root.

El cliente **también** ocupa un puerto, pero no lo elige: al conectarse, el sistema
operativo le asigna uno libre del rango alto (puerto *efímero*). No necesita una
dirección conocida porque nadie lo llama a él; solo necesita una dirección de retorno.

### Cómo un solo puerto atiende a muchos clientes

Si todos los clientes se conectan al mismo puerto, ¿no se mezclan las conversaciones?

No, porque **una conexión TCP no se identifica por un puerto sino por cuatro datos**:
IP de origen, puerto de origen, IP de destino y puerto de destino. Como cada cliente
recibe un puerto efímero distinto, cada conexión forma una combinación única aunque
todas compartan la IP y el puerto del servidor. El kernel usa esa cuádrupla para saber
a qué conversación pertenece cada paquete.

En el código eso se refleja en **dos tipos de socket distintos**:

1. El servidor crea un **socket de escucha**, le asigna dirección y puerto (`bind`) y
   lo pone a esperar (`listen`). Este socket **nunca transporta datos de la
   aplicación**: su único trabajo es recibir solicitudes de conexión.
2. Cada vez que llega una, `accept` devuelve un **socket nuevo y distinto**, dedicado a
   ese cliente. Por ahí sí viaja la información. Mientras tanto, el socket de escucha
   queda libre para la conexión siguiente.

### TCP: flujo de datos, no mensajes

TCP garantiza que la información llegue **completa, en orden y sin duplicados**: numera
lo que envía, espera confirmaciones, retransmite lo perdido y regula la velocidad si el
receptor no da abasto.

Lo que **no** hace es conservar la noción de "mensaje". Para TCP la conexión es un flujo
continuo, sin marcas que indiquen dónde termina un envío y empieza el siguiente: lo que
el emisor manda en tres operaciones puede llegar en una sola lectura, o al revés. Por
eso toda aplicación sobre TCP necesita definir su propia forma de delimitar mensajes
(ver el protocolo en [02_arquitectura.md](02_arquitectura.md)).

### Por qué TCP y no UDP

UDP envía datagramas sueltos, sin conexión previa y sin garantías: pueden perderse,
llegar desordenados o duplicados, y nadie se entera. Para este sistema TCP es la única
opción razonable:

- **Una imagen no tolera pérdidas.** Si falta un fragmento, el archivo queda corrupto e
  inservible. Muy distinto de una videollamada, donde perder un cuadro se nota apenas y
  no vale la pena retransmitirlo — ese es el terreno de UDP.
- **Una imagen no entra en un datagrama.** UDP tiene un tamaño máximo de unas decenas de
  kilobytes por datagrama. Una foto de 3 MB habría que partirla a mano, numerar cada
  parte, detectar faltantes, volver a pedirlas y reensamblarlas en orden. Eso es,
  literalmente, lo que TCP ya hace y bien probado.

### Por qué sockets directos y no HTTP con un framework

HTTP no es una alternativa *a* los sockets: HTTP **corre sobre** sockets TCP. Un
framework web no eliminaría esta capa, solo la escondería.

Trabajar directamente con sockets nos da tres cosas: definimos un protocolo ajustado a
lo que necesitamos; la imagen viaja **tal cual es**, sin transformaciones (por HTTP
habría que usar `multipart/form-data` o base64, que la infla un 33%); y es el contenido
central de la materia.

El costo es la interoperabilidad: solo nuestro cliente puede hablar con nuestro
servidor, un navegador no. Como el cliente también es parte del proyecto, no nos afecta.

---

## 2. asyncio

### El problema que resuelve

Un programa normal, al pedir datos de un socket, se **bloquea**: el sistema operativo lo
saca de ejecución hasta que los datos llegan, y durante ese tiempo no puede hacer nada
más. Con un solo hilo y diez clientes, nueve esperan a que termine el primero.

La solución tradicional es **un hilo (o proceso) por cliente**. Funciona, pero cada hilo
reserva memoria de pila y obliga al sistema operativo a alternar entre todos, un costo
que crece con cada conexión. Si además comparten datos hay que sincronizarlos con locks,
con los problemas que eso trae (condiciones de carrera, interbloqueos). Y en Python hay
un motivo extra: el **GIL** impide que dos hilos ejecuten código Python simultáneamente,
así que para trabajo de CPU los hilos ni siquiera dan el paralelismo esperado.

### Cómo funciona

asyncio parte de una observación: si el programa pasa casi todo el tiempo esperando, no
necesita más hilos — necesita **aprovechar las esperas**. Lo consigue con tres piezas:

**Las corrutinas** son funciones declaradas con `async def` que pueden **pausarse en la
mitad y retomarse después**. Se diferencian de una función normal en dos cosas.
Primero, llamarlas no las ejecuta: devuelven un objeto corrutina, que recién corre
cuando el loop lo agenda (envuelto en una `Task`). Segundo, y más importante: el estado
de una función normal —en qué línea va, cuánto valen sus variables— vive en la pila de
llamadas, que no se puede apartar y guardar; el de una corrutina vive en un objeto del
heap, y por eso puede quedar estacionada y reanudarse intacta.

**El event loop** es un bucle en un **único hilo** que mantiene la lista de corrutinas
listas para avanzar. Toma una, la ejecuta hasta que se suspende, la guarda, toma la
siguiente, y así indefinidamente.

**El selector del sistema operativo** (`epoll` en Linux, `kqueue` en macOS) es lo que lo
hace eficiente. Cuando el loop se queda sin corrutinas listas, le pregunta al kernel:
"de estos 300 sockets, ¿cuáles tienen datos disponibles?". El kernel responde y el loop
reanuda solo esas corrutinas. No hay espera activa: el proceso duerme hasta que el
kernel lo despierta con trabajo concreto.

### Qué significa exactamente `await`

`await` **no significa "pausá acá"**. Significa: *esta operación puede no estar lista;
si no lo está, devolvele el control al loop*. Si los datos ya están en el buffer, la
corrutina sigue de largo sin suspenderse.

Y el punto crucial: la suspensión es **cooperativa, no forzada**. Nadie le quita el
control a una corrutina; el loop solo puede cambiar de tarea en los puntos donde la
corrutina misma cede, es decir, en los `await`. Es lo opuesto a los hilos, donde el
sistema operativo interrumpe cuando quiere.

De ahí se sigue la regla que gobierna toda la arquitectura: **cualquier trabajo de CPU
dentro del event loop lo congela por completo**. Una corrutina que procese una imagen
durante cuatro segundos sin ningún `await` deja a todos los clientes esperando, porque
no existe mecanismo que pueda quitarle el turno.

### Concurrencia vs. paralelismo

En cada instante hay **exactamente una corrutina ejecutando código Python**. Nunca dos.
No hay simultaneidad real: hay alternancia muy rápida durante las esperas. Eso es
**concurrencia**.

El **paralelismo** —varias cosas ejecutándose de verdad al mismo tiempo— requiere varios
procesos, y en este proyecto vive en los workers.

| | asyncio | procesos |
|---|---|---|
| Qué da | concurrencia | paralelismo |
| Hilos/procesos | uno | varios |
| Sirve para | trabajo de espera (I/O) | trabajo de cálculo (CPU) |
| Cambio de tarea | cooperativo (en los `await`) | lo decide el sistema operativo |
| Sincronización | no hace falta | locks, colas |

---

## 3. IPC y multiprocessing

### Qué es IPC

Los procesos están **aislados por diseño**: cada uno tiene su propio espacio de
direcciones y su memoria no la ve nadie más. Si el proceso A tiene `x = 5`, B no puede
leerla ni sabe que existe. Ese aislamiento es deliberado y es lo que hace robusto al
sistema — un error en un proceso no puede corromper la memoria de otro.

Pero a veces necesitamos que colaboren, y ahí está la tensión: el aislamiento que
protege es también lo que impide comunicarse. **IPC (Inter-Process Communication) es el
conjunto de mecanismos que ofrece el sistema operativo para perforar ese aislamiento de
manera controlada.**

### Los mecanismos

**Por paso de mensajes** — los datos se **copian** de un proceso al otro a través del
kernel: *pipes* (canal anónimo entre procesos emparentados), *FIFOs* o named pipes (lo
mismo pero con nombre en el sistema de archivos, usable por procesos sin parentesco),
colas de mensajes y *sockets*. Como cada proceso trabaja sobre su propia copia, el
mecanismo mismo serializa el acceso.

**Por memoria compartida** — el sistema operativo mapea una misma región de memoria
física en ambos procesos. Es el más rápido, porque no hay copias, pero también el más
peligroso: si dos escriben a la vez en la misma posición el resultado queda indefinido.
Por eso **siempre** viene acompañada de primitivas de sincronización (locks, semáforos,
condiciones).

**Señales** — caso degenerado: no transportan datos, solo avisan que algo pasó
(`SIGTERM`, `SIGINT`).

Vale notar que **los sockets también son IPC**: son el único mecanismo que funciona
entre máquinas distintas.

### Qué es multiprocessing

Es el módulo estándar de Python para **crear y coordinar procesos**. Existe por una
razón concreta del lenguaje: como el GIL impide que los hilos ejecuten código Python en
paralelo, la única forma de aprovechar varios núcleos es con procesos separados, cada
uno con su propio intérprete.

Cubre tres áreas:

- **Crear procesos**: `Process(target=..., args=...)`, con `start()`, `join()`.
- **Comunicarlos (IPC)**: `Queue` y `Pipe` (paso de mensajes); `Value` y `Array`
  (memoria compartida); `Manager`.
- **Sincronizarlos**: `Lock`, `RLock`, `Semaphore`, `Event`, `Condition`, `Barrier`.

Es decir que `multiprocessing` no *es* IPC: es un módulo que permite crear procesos y
que, además, ofrece implementaciones cómodas de varios mecanismos de IPC.

### Cómo funciona `multiprocessing.Queue` por dentro

Por debajo **es un pipe**. Lo que agrega es una capa de comodidad: cuando hacés
`put(objeto)`, la Queue lo **serializa** con `pickle` y escribe los bytes en el pipe;
del otro lado `get()` los lee y **deserializa**, reconstruyendo el objeto. Por eso se
puede mandar un diccionario y recibir un diccionario, en vez de armar y parsear bytes a
mano. Además protege el pipe con locks internos, así varios procesos pueden escribir sin
que los mensajes se entremezclen.

Consecuencia práctica: **solo se pueden enviar objetos serializables**. Un diccionario o
una lista van bien; un socket abierto o una conexión a base de datos, no.

### Por qué `Queue` y no un FIFO

Ambos son válidos y ambos se vieron en clase. La Queue gana en comodidad cuando los dos
extremos son procesos Python emparentados: serializa sola y es segura ante múltiples
escritores. Un FIFO sería la elección si el otro extremo fuera un programa externo,
escrito en otro lenguaje o arrancado por separado.

**El límite de la Queue**: funciona porque el proceso hijo se crea con `fork` y hereda
el objeto, y con él los descriptores del pipe. Un proceso ajeno, que no descienda del
mismo padre, no puede acceder a ese canal. Por eso la Queue sirve para comunicar
procesos dentro de una máquina, pero no para hablar con procesos que corren en otro
contenedor u otra máquina.

---

## 4. Celery y Redis

### Qué es Celery

Una **biblioteca de Python para colas de tareas distribuidas**: permite escribir una
función normal y, en lugar de ejecutarla acá y ahora, mandarla a ejecutar a otro proceso
—posiblemente en otra máquina— y seguir trabajando.

Se define una función común con un decorador:

```python
@app.task
def anonymize(job_id, path, mode):
    ...
    return {'output': ...}
```

Y hay dos formas de llamarla:

```python
anonymize(job_id, path, 'blur')          # ejecuta acá mismo: tarda y bloquea
anonymize.delay(job_id, path, 'blur')    # NO ejecuta: encola y vuelve en ~1 ms
```

`.delay()` no ejecuta nada: **serializa la invocación** —el nombre de la función y sus
argumentos— en un mensaje y lo deja en el broker:

```json
{"task": "tasks.anonymize", "id": "c1f2…", "args": ["a3f7…", "/storage/uploads/…"],
 "kwargs": {"mode": "blur"}}
```

Del otro lado, un worker toma el mensaje, busca esa función **en su propia copia del
código**, la ejecuta y guarda el resultado.

De ahí el punto más importante: **Celery no transporta código, transporta llamadas**. El
worker debe tener el mismo `tasks.py` que el servidor. Y los argumentos deben ser
serializables — por eso se envían rutas de archivo y no objetos de imagen.

### Las piezas

- **Celery** es la biblioteca: define las tareas, las serializa, las despacha, maneja
  reintentos y resultados. **No almacena nada por sí misma.**
- **El broker** (Redis) es donde los mensajes esperan. Es un programa aparte.
- **Los workers** son los procesos que ejecutan.
- **El result backend** (también Redis) registra estado y resultado de cada tarea.

`.delay()` devuelve un `AsyncResult`, que funciona como un ticket: tiene `id`, `status`
(PENDING / STARTED / SUCCESS / FAILURE) y `result`. Esa información vive en Redis, así
que sobrevive aunque el worker que ejecutó la tarea ya no exista.

### Qué es un worker, propiamente dicho

Un **proceso de Python que corre el runtime de Celery, conectado al broker**. No es un
hilo ni una tarea: es un programa independiente.

Al levantar uno con `--concurrency=4` aparecen en realidad cinco procesos:

```
celery worker MainProcess          ← coordina: conexión al broker, reparto, confirmaciones
 ├── ForkPoolWorker-1              ← ejecuta tareas
 ├── ForkPoolWorker-2
 ├── ForkPoolWorker-3
 └── ForkPoolWorker-4
```

El principal **no ejecuta tareas**: mantiene la conexión, reclama mensajes, los reparte
entre los hijos libres, confirma al broker y publica resultados. Los del pool son
procesos separados creados con `fork` — de ahí que el paralelismo sea real, cada uno con
su intérprete y su GIL.

Por eso la palabra es ambigua: para Celery un worker es la unidad completa; en el habla
corriente "cuatro workers" suele significar cuatro ejecuciones simultáneas. Con
`--scale worker=3 --concurrency=2` hay **tres workers y seis tareas en paralelo**.

Dos propiedades importantes: es **independiente del servidor** (no es un proceso hijo,
se levanta por separado, puede estar en otra máquina — lo único que los vincula es que
apuntan al mismo Redis) y **tiene su propia copia del código**.

### Nadie reparte el trabajo

No hay ningún componente que decida "esta tarea va para el worker 2". El modelo es
**pull, no push**: cada worker libre toma el próximo mensaje disponible por su cuenta, y
el broker garantiza que se lo lleve uno solo.

De ahí la propiedad que hace escalable al sistema: un worker nuevo **no se registra en
ningún lado**. Arranca, se conecta a Redis y empieza a tomar mensajes. Si desaparece,
tampoco hay que notificar nada. No existe un punto central que lleve la cuenta.

### Por qué prefork y no hilos

Celery permite elegir el tipo de pool: `prefork` (procesos, por defecto), hilos, o
corrutinas (`gevent`, `eventlet`). Corresponde `prefork` porque nuestras tareas son
**CPU-bound**, y con hilos el GIL las serializaría. Los pools de hilos sirven para tareas
I/O-bound (mandar mails, llamar APIs).

### Confirmación de tareas (`acks_late`)

Por defecto Celery confirma el mensaje al broker **apenas lo reserva**, antes de
ejecutarlo: si el worker muere en el medio, la tarea se pierde.

Con `task_acks_late = True` la confirmación se posterga hasta que la tarea termina, de
modo que si el worker desaparece el broker se la entrega a otro. El costo es que una
tarea podría ejecutarse dos veces (si el worker muere justo después de terminar pero
antes de confirmar); es aceptable cuando las operaciones son **idempotentes**.

### Composición de tareas

- **`chain`**: encadena tareas, cada una recibe la salida de la anterior. Para etapas
  secuenciales.
- **`group`**: lanza varias en paralelo. Para subtareas independientes.
- **`chord`**: un `group` seguido de una tarea que consolida los resultados.

### Por qué una cola distribuida y no `multiprocessing.Pool`

Un Pool resolvería el paralelismo, pero sus procesos son **hijos del servidor**, y de ahí
salen sus tres limitaciones: si el servidor se reinicia se lleva puesto todo lo que
estaba en curso; los procesos deben vivir en la misma máquina; y si una tarea falla,
nadie la reintenta.

Con la cola, servidor y workers son programas **independientes que ni se conocen**: lo
único que comparten es el buzón. Se puede reiniciar el servidor mientras los workers
trabajan, sumar workers en otras máquinas, y Celery reintenta lo que falla. Además, es
lo que pide la consigna: una cola de tareas **distribuidas**, no un pool local.

### Por qué Redis y no RabbitMQ

RabbitMQ es más potente para mensajería compleja (ruteos elaborados, prioridades), pero
acá no hay reglas de ruteo ni prioridades que justifiquen esa potencia. Redis cubre los
dos roles —broker y result backend— con un único servicio y prácticamente sin
configuración.

---

## 5. SQLite

Base de datos relacional que vive en **un único archivo**, sin servidor que instalar ni
administrar: la biblioteca se enlaza al propio programa. Viene incluida en la biblioteca
estándar de Python.

Su limitación conocida es la **escritura concurrente**: cuando dos procesos intentan
escribir a la vez, uno recibe el error *database is locked*. En este proyecto esa
limitación no aplica, porque un solo proceso (el auditor) escribe en la base.

Por qué SQLite y no PostgreSQL: el volumen de datos es chico, no hay escrituras
concurrentes y no queremos sumar un servicio más al despliegue. PostgreSQL sería la
elección si hubiera varios escritores o si el historial creciera a millones de filas.

---

## 6. Pillow y OpenCV

**Pillow** es la biblioteca estándar de manipulación de imágenes en Python: abrir,
redimensionar, aplicar filtros (`GaussianBlur`), guardar con distinto formato y calidad,
y leer metadatos EXIF (incluido el bloque GPS). Se usa para el difuminado, el borrado de
metadatos, la conversión y la compresión.

**OpenCV** es una biblioteca de visión por computadora. Se usa únicamente para la
**detección de caras**, con *cascadas de Haar* (`haarcascade_frontalface`), un método
clásico que viene incluido en el paquete `opencv-python` — no requiere descargar modelos
ni usar redes neuronales.

Limitación asumida: las cascadas de Haar detectan bien caras frontales y peor las de
perfil. Se documenta como mejora futura (sumar la cascada de perfil, o migrar a un
detector basado en redes neuronales).

---

## 7. Docker y Docker Compose

**Docker** empaqueta una aplicación junto con todas sus dependencias en una imagen que
corre igual en cualquier máquina. **Docker Compose** describe un conjunto de servicios y
los levanta con un solo comando.

Dos razones para usarlo:

**Reproducibilidad.** OpenCV arrastra dependencias nativas cuya instalación manual es de
las tareas más propensas a fallar. Dentro de la imagen ya están resueltas.

**Vuelve demostrable lo que de otro modo sería una afirmación.** Que el sistema sea
distribuido se puede mostrar en vivo: levantar cuatro workers con
`docker compose up --scale worker=4` y ver cómo se reparten la carga, o matar uno a mitad
de un trabajo y ver cómo la tarea se recupera en otro.

---

## 8. argparse

Módulo estándar de Python para **parsear argumentos de línea de comandos**. Define las
opciones, valida tipos y valores obligatorios, y genera el `--help` automáticamente.

Es el requisito más simple de la consigna, pero cumple un rol de diseño real: gracias a
que nada está fijo en el código, el servidor puede escuchar en otro puerto y el cliente
apuntar a otra máquina sin recompilar nada. Es lo que hace posible la demostración
distribuida.

---

## Resumen: qué resuelve cada tecnología

| Tecnología | Problema que resuelve |
|---|---|
| **Sockets TCP** | Comunicar procesos en máquinas distintas, de forma confiable |
| **asyncio** | Atender muchos clientes a la vez sin un hilo por cliente |
| **multiprocessing.Queue** | Pasar eventos al proceso auditor sin bloquear el event loop |
| **Celery** | Encolar y distribuir trabajo pesado entre procesos independientes |
| **Redis** | Almacenar la cola de tareas pendientes y el estado de cada una |
| **SQLite** | Guardar el historial permanente sin administrar un servidor de BD |
| **Pillow / OpenCV** | Procesar las imágenes (la lógica de la aplicación) |
| **Docker Compose** | Desplegar y escalar todo el sistema con un comando |
| **argparse** | Configurar cliente y servidor desde la línea de comandos |
