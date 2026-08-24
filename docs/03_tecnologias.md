# Tecnologías utilizadas

Este documento explica **qué es** cada tecnología del proyecto y **por qué se eligió**
frente a sus alternativas. Cómo se combinan entre sí está en
[02_arquitectura.md](02_arquitectura.md); la especificación del protocolo, en
[04_protocolo.md](04_protocolo.md).

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

### IPv4, IPv6 y cómo atender a las dos

Conviven hoy **dos familias de direcciones**. IPv4 usa 32 bits —los clásicos
`192.168.0.10`, unos 4.300 millones de direcciones, hace años agotadas— e IPv6 usa 128
bits, escritos en hexadecimal y separados por dos puntos
(`2001:db8::1`), con un espacio que en la práctica no se agota.

Para un socket no son intercambiables: son **familias distintas**, `AF_INET` y
`AF_INET6`. Un servidor que abre un socket IPv4 simplemente no existe para un cliente que
llega por IPv6.

Hay **dos maneras** de atender a las dos familias, y conviene saber cuál usamos.

La primera es el **socket dual-stack**: se abre un único socket `AF_INET6` en la
dirección `::` y se deja *desactivada* la opción `IPV6_V6ONLY`. Con eso el sistema
operativo acepta también conexiones IPv4 sobre ese mismo socket, presentándolas como
direcciones IPv6 mapeadas (`::ffff:192.168.0.10`). Un socket, las dos familias.

La segunda es abrir **un socket por familia**: uno `AF_INET` en `0.0.0.0` y otro
`AF_INET6` en `::`, los dos sobre el mismo puerto. No chocan entre sí porque pertenecen a
familias distintas.

**Nuestro servidor usa la segunda**, porque es lo que hace `asyncio.start_server` cuando
se le pasan el host y el puerto. Y asyncio lo hace a propósito: **activa** `IPV6_V6ONLY`
en el socket IPv6 para desactivar su modo dual. El motivo es la portabilidad — el valor
por defecto de esa opción varía según el sistema (en Linux suele venir en modo dual, en
BSD y macOS no), y con un socket por familia el resultado es idéntico en todas partes.

Se puede verificar leyendo `server.sockets`: sin host hay dos entradas, una `AF_INET` y
otra `AF_INET6`. Con una dirección concreta queda una sola, la de esa familia.

Un detalle del que conviene acordarse: si además se pide el puerto 0 —"elegí uno libre"—
**cada socket recibe un puerto distinto**. Con un puerto fijo, que es el uso normal, los
dos comparten el mismo.

Del lado del cliente no hay nada que decidir: `asyncio.open_connection` resuelve el
nombre o la dirección y elige la familia que corresponda.

Sostener IPv6 no cuesta trabajo adicional y evita que el servicio quede inaccesible en
redes que ya operan sobre esa familia.

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
(ver [04_protocolo.md](04_protocolo.md)).

### Por qué TCP y no UDP

UDP envía datagramas sueltos, sin conexión previa y sin garantías: pueden perderse,
llegar desordenados o duplicados, y nadie se entera. Para este sistema TCP es la única
opción razonable:

- **Una imagen no tolera pérdidas.** Si falta un fragmento, el archivo queda corrupto e
  inservible. Muy distinto de una videollamada, donde perder un cuadro se nota apenas y
  no vale la pena retransmitirlo — ese es el terreno de UDP.
- **Una imagen no entra en un datagrama.** El máximo teórico de un datagrama UDP es de
  **65.507 bytes** de contenido (los 65.535 del campo de longitud menos las cabeceras
  UDP e IP), y en la práctica conviene quedarse bastante por debajo para evitar la
  fragmentación IP. Una foto de 3 MB habría que partirla a mano, numerar cada parte,
  detectar faltantes, volver a pedirlas y reensamblarlas en orden. Eso es, literalmente,
  lo que TCP ya hace y bien probado.

### Por qué sockets directos y no HTTP con un framework

HTTP no es una alternativa *a* los sockets: HTTP **corre sobre** sockets TCP. Un
framework web no eliminaría esta capa, solo la escondería.

Trabajar directamente con sockets nos da dos cosas: definimos un protocolo ajustado a lo
que necesitamos, y es el contenido central de la materia. Un framework resolvería la
transferencia sin que escribiéramos una línea, pero entonces estaríamos demostrando el
manejo de una biblioteca y no el de la API de sockets, que es lo que la materia evalúa.

Conviene además reconocer el parentesco, porque es la mejor forma de explicar nuestro
protocolo: **es un HTTP en miniatura**. La cabecera `Content-Length` de HTTP cumple
exactamente la función de nuestro prefijo de longitud —anunciar cuántos bytes de cuerpo
vienen después— y por el mismo motivo: TCP entrega un flujo continuo y alguien tiene que
declarar dónde termina cada mensaje. No inventamos un mecanismo raro; reimplementamos, en
unas sesenta líneas, el que usa HTTP.

Sobre el tamaño de lo transmitido no hay diferencia apreciable: `multipart/form-data`
transporta los bytes **tal cual**, separados por una marca de frontera, con un sobrecosto
de unos pocos cientos de bytes. El +33% es de **base64**, que haría falta solo si la
imagen viajara dentro de un JSON.

El costo real es la interoperabilidad: solo nuestro cliente puede hablar con nuestro
servidor, un navegador no, y no se puede probar con `curl`. Como el cliente también es
parte del proyecto, lo asumimos.

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

### La válvula de escape: `asyncio.to_thread`

Si el event loop no tolera nada bloqueante, ¿qué se hace cuando una biblioteca **no
tiene versión asíncrona**? Es el caso de varias que usamos: encolar en Celery implica una
llamada de red a Redis que bloquea, y consultar SQLite implica leer del disco.

`asyncio.to_thread(func, *args)` resuelve exactamente eso: ejecuta la función bloqueante
**en un hilo aparte**, tomado de un pool que asyncio administra, y devuelve el control al
loop mientras tanto. Desde la corrutina se usa como cualquier otra espera:

```python
resultado = await asyncio.to_thread(tarea.delay, job_id, ruta, params)
```

Mientras ese hilo espera a Redis, el loop sigue atendiendo a los demás clientes; cuando
termina, la corrutina se reanuda con el resultado.

Vale aclarar por qué acá los hilos **sí** sirven, después de haberlos descartado antes. El
problema del GIL es que impide ejecutar **código Python** en paralelo. Pero una llamada
bloqueante de red o de disco **libera el GIL mientras espera**, porque la espera ocurre
dentro del sistema operativo, no en el intérprete. Para trabajo de espera los hilos
funcionan perfectamente; lo que no dan es paralelismo de cálculo.

De ahí la regla completa del proyecto: **espera con biblioteca asíncrona** → `await`
directo; **espera con biblioteca bloqueante** → `to_thread`; **cálculo pesado** → otro
proceso, es decir, los workers.

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

### `Pipe` y `Queue`: cuál usar

Los dos sirven para lo mismo —pasarse objetos entre procesos— y los dos **serializan con
`pickle`**: cuando enviás un objeto, se convierte a bytes de un lado y se reconstruye del
otro. Por eso se puede mandar un diccionario y recibir un diccionario en vez de armar y
parsear bytes a mano, y por eso **solo se pueden enviar objetos serializables**: un
diccionario o una lista van bien; un socket abierto o una conexión a base de datos, no.

La diferencia está en para cuántos procesos son:

| | `Pipe` | `Queue` |
|---|---|---|
| Extremos | dos, y nada más | los que haga falta |
| Bidireccional | sí, un solo objeto | no: una cola por sentido |
| Por dentro | el pipe del sistema operativo | un pipe **más** un candado y un hilo auxiliar |
| Cuándo conviene | dos procesos que se hablan | varios productores o varios consumidores |

`Queue` está construida encima de un `Pipe`: le agrega el candado para que varios
procesos puedan escribir sin entremezclar mensajes, y un hilo interno que se encarga de
escribir en el pipe para que `put()` nunca bloquee. Todo eso es útil cuando hay varios de
cada lado, y es peso muerto cuando hay uno solo.

En este proyecto el servidor y el proceso de ingreso son **exactamente dos procesos
hablando en las dos direcciones**, así que se usa un `Pipe`. Si algún día hubiera varios
procesos de ingreso leyendo del mismo canal, ahí la cola pasaría a ser la opción correcta.

### Esperar un pipe sin frenar el event loop

`recv()` bloquea hasta que llega algo, y bloquear dentro del event loop congela a todos
los clientes conectados a la vez. `Connection` ofrece la salida: **`poll()` contesta al
instante si hay datos**, sin esperar por ellos. Con eso, el que recibe puede ser una
corrutina común:

```python
while True:
    if not conexion.poll():             # ¿hay algo?
        await asyncio.sleep(0.01)       # no: le devuelvo el control al event loop
        continue
    mensaje = conexion.recv()           # sí: recv() no va a bloquear
```

Es una espera activa, y conviene reconocerlo: el servidor mira cien veces por segundo
aunque no pase nada. La alternativa sería un hilo que llame a `recv()` bloqueante, pero
eso trae consigo el problema de cómo frenarlo —un hilo no se puede matar en Python— y la
necesidad de cruzar los resultados del hilo al event loop. Cien comprobaciones por segundo
salen más baratas que ese conjunto de complicaciones.

### Proceso o hilo: qué da uno que el otro no

Para sacar trabajo del event loop hay dos caminos, y conviene tener clarísimo en qué se
diferencian, porque es una pregunta habitual.

**Un hilo alcanza** cuando el trabajo es *esperar*: leer un archivo, consultar una base,
hablar por red. Esas operaciones liberan el GIL mientras esperan, así que el hilo no
estorba y es mucho más barato que un proceso — comparte memoria, arranca en
microsegundos y no requiere serializar nada para comunicarse.

**Un proceso hace falta** por dos motivos, y solo por esos dos:

1. **Paralelismo de cálculo.** Dos hilos no pueden ejecutar código Python a la vez por
   el GIL; dos procesos sí, porque cada uno tiene su propio intérprete.
2. **Aislamiento ante fallas.** Un hilo comparte el espacio de memoria del proceso: si
   algo lo corrompe o lo hace caer, se lleva todo puesto. Un proceso puede morir solo.

Ese segundo punto es el que suele pasarse por alto y el que decide el diseño de esta
aplicación. Bibliotecas como Pillow y OpenCV son, por dentro, **código nativo en C**.
Frente a un archivo malformado —accidental o deliberado— pueden provocar una caída del
intérprete: no una excepción de Python que se pueda atrapar, sino la muerte del proceso.

Por eso, cuando un programa tiene que **procesar contenido que viene de afuera**, hacerlo
en un proceso separado no es un lujo: es la única forma de que un archivo malicioso no
derribe todo el servicio. Y el aislamiento solo sirve si alguien **relanza** lo que se
cayó, así que el proceso principal tiene que supervisar a sus hijos.

### Por qué `Queue` y no un FIFO

Ambos son válidos y ambos se vieron en clase. La Queue gana en comodidad cuando los dos
extremos son procesos Python emparentados: serializa sola y es segura ante múltiples
escritores. Un FIFO sería la elección si el otro extremo fuera un programa externo,
escrito en otro lenguaje o arrancado por separado.

**El límite de la Queue**: funciona porque el proceso hijo **recibe el objeto al
crearse**, y con él los descriptores del pipe subyacente. Según el sistema, eso ocurre
por herencia directa (`fork`, el método por defecto en Linux) o transfiriéndolo al
arrancar el hijo (`spawn`, el de macOS y Windows); en ambos casos la condición es la
misma: **el canal se entrega en el momento de crear el proceso**. Un proceso ajeno, que
no fue creado por este padre, no tiene forma de obtenerlo. Por eso la Queue comunica
procesos dentro de una máquina, pero no sirve para hablar con procesos que corren en
otro contenedor u otra máquina.

---

## 4. Celery y Redis

### Qué es Redis

Un **almacén de datos clave-valor que vive en memoria**. A diferencia de una base de
datos tradicional, que guarda en disco y lee de ahí, Redis mantiene todo en RAM: por eso
sus operaciones se miden en microsegundos. Se ejecuta como un servicio aparte, al que los
programas se conectan por red.

Además de valores simples maneja estructuras de datos —listas, conjuntos, hashes— y es
justamente la **lista** lo que permite usarlo como cola: un proceso agrega mensajes por un
extremo y otro los retira por el otro, de forma atómica.

Puede persistir su contenido en disco periódicamente, pero su naturaleza es la de un
almacén **volátil y rápido**, pensado para datos que tienen sentido *ahora*. Eso lo hace
ideal para lo que le pedimos —tareas pendientes y estados en curso— y explica por qué el
historial permanente va a SQLite y no a Redis.

En el proyecto cumple **dos roles distintos** que conviene no confundir: es el *broker*
(donde esperan las tareas pendientes) y el *result backend* (donde queda el estado y el
resultado de cada tarea). Son dos usos del mismo servicio, en bases separadas.

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

### Seguimiento de estados (`task_track_started`)

Por defecto Celery **no informa el estado `STARTED`**: una tarea pasa directamente de
`PENDING` a `SUCCESS`, de modo que nunca se puede saber si un worker ya la tomó o sigue
esperando en la cola. Con `task_track_started = True` el worker reporta el estado al
empezar a ejecutar, que es lo que permite distinguir "encolado" de "procesando".

Hay además una ambigüedad que conviene conocer: **`PENDING` significa tanto "encolado y
sin tomar" como "no conozco esta tarea"**. Celery no guarda nada en el backend hasta que
la tarea empieza, así que un identificador inventado devuelve `PENDING` igual que uno
real en cola. Por eso quien consulta debe verificar por otro medio que la tarea exista.

### Confirmación de tareas (`acks_late`)

Por defecto Celery confirma el mensaje al broker **apenas lo reserva**, antes de
ejecutarlo: si el worker muere en el medio, la tarea se pierde.

Con `task_acks_late = True` la confirmación se posterga hasta que la tarea termina, de
modo que si el worker desaparece el broker se la entrega a otro. El costo es que una
tarea podría ejecutarse dos veces (si el worker muere justo después de terminar pero
antes de confirmar); es aceptable cuando las operaciones son **idempotentes**.

### Expiración de resultados (`result_expires`)

Los resultados que Celery guarda en el backend **no son permanentes**: por defecto se
borran a las **24 horas** (`result_expires = 86400`). Es una decisión sensata de Celery,
porque el backend es un almacén en memoria y sin expiración crecería sin límite.

La consecuencia para el diseño es concreta: **el estado de un trabajo de la semana pasada
ya no existe en Redis**. Si la descarga de un resultado dependiera de consultar el
backend, dejaría de funcionar al día siguiente aunque el archivo siguiera en el disco.

Por eso el sistema trata a Redis como estado *vivo* y a SQLite como verdad *permanente*:
Redis se consulta solo mientras el trabajo está en curso, y todo lo terminado se resuelve
contra la base de datos.

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
limitación no aplica, porque un solo proceso (el proceso de ingreso) escribe en la base.

Por qué SQLite y no PostgreSQL: el volumen de datos es chico, no hay escrituras
concurrentes y no queremos sumar un servicio más al despliegue. PostgreSQL sería la
elección si hubiera varios escritores o si el historial creciera a millones de filas.

---

## 6. Pillow y OpenCV

**Pillow** es la biblioteca de facto para manipular imágenes en Python — no forma parte
de la biblioteca estándar, se instala aparte (`pip install pillow`). Permite abrir,
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
corre igual en cualquier máquina. Cada contenedor es un proceso aislado: tiene su propio
sistema de archivos y su propia vista de la red, y por defecto **no comparte nada** con
los demás. **Docker Compose** describe un conjunto de servicios y los levanta con un solo
comando.

### Volúmenes: cómo se comparten archivos entre contenedores

Ese aislamiento plantea un problema para nuestro diseño: si el servidor guarda una imagen
en su sistema de archivos, los workers —que corren en otros contenedores— no la ven. Y la
arquitectura depende de que la vean, porque por la cola viajan rutas y no imágenes.

Un **volumen** es la solución: un directorio que Docker monta dentro de varios
contenedores a la vez, de modo que todos ven **el mismo sistema de archivos** en esa ruta.
Lo que el servidor escribe en `/storage/uploads/`, el worker lo lee en la misma ruta,
aunque sean procesos aislados en contenedores distintos.

El volumen es entonces una pieza estructural, no un detalle de despliegue: es lo que hace
que una ruta signifique lo mismo en los dos lados. Es también el límite de escalabilidad
del diseño — para repartir workers en máquinas que no comparten ese volumen habría que
pasar a almacenamiento en red (NFS) o de objetos (S3).

### Por qué contenedores

Dos razones:

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
| **multiprocessing.Pipe** | Pasarle pedidos y eventos al proceso de ingreso, y recibir sus veredictos |
| **Celery** | Encolar y distribuir trabajo pesado entre procesos independientes |
| **Redis** | Almacenar la cola de tareas pendientes y el estado de cada una |
| **SQLite** | Guardar el historial permanente sin administrar un servidor de BD |
| **Pillow / OpenCV** | Procesar las imágenes (la lógica de la aplicación) |
| **Docker Compose** | Desplegar y escalar todo el sistema con un comando |
| **argparse** | Configurar cliente y servidor desde la línea de comandos |

## Las tres formas de esperar, y cuándo usar cada una

Buena parte de las decisiones del proyecto se reducen a una sola pregunta: *¿esto espera
o calcula?*

| Situación | Herramienta | Dónde aparece |
|---|---|---|
| Espera con biblioteca asíncrona | `await` directo | leer y escribir en los sockets |
| Espera con biblioteca bloqueante | `asyncio.to_thread` | encolar en Celery, leer SQLite |
| Cálculo pesado | otro proceso | los workers |

Usar la herramienta equivocada en cualquiera de las tres filas rompe el sistema: un
`await` sobre algo que en realidad calcula congela a todos los clientes, y un proceso por
cliente conectado sería un derroche para trabajo que es pura espera.
