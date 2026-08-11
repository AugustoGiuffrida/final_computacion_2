# Arquitectura

## 1. Vista general

La idea central de la arquitectura es simple: **el sistema tiene dos mitades que
trabajan a ritmos distintos**.

- Una mitad **atiende**: recibe las imágenes de los clientes, responde consultas,
  entrega resultados. Tiene que ser rápida siempre, aunque haya muchos clientes.
- La otra mitad **procesa**: detecta caras, difumina, recomprime. Es lenta por
  naturaleza (segundos por imagen) y consume mucho CPU.

Si las dos cosas las hiciera un mismo programa, mientras procesa una imagen no
podría atender a nadie más. Por eso están separadas en procesos distintos, unidas
por una **cola de tareas**: la mitad que atiende deja "pedidos" en la cola y sigue
atendiendo; la mitad que procesa los va tomando a su ritmo.

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

**Cómo leer el diagrama**: el cliente habla con el servidor por un socket TCP (flecha
de arriba). El servidor guarda la imagen en el volumen compartido, deja la tarea en
Redis y le avisa al auditor. Un worker toma la tarea de Redis, lee la imagen del
volumen, la procesa y guarda el resultado en el mismo volumen. El cliente después
pregunta por el estado y descarga el resultado, siempre a través del servidor.

Notar que **el cliente solo conoce al servidor**: no sabe que existen Redis, los
workers ni el auditor. Toda la complejidad interna queda oculta detrás de una única
dirección y un único puerto, y eso es lo que permite cambiar la mitad de atrás
—agregar workers, cambiar el broker— sin tocar ni el cliente ni el protocolo.

## 2. Componentes: qué hace cada uno y por qué

### 2.1 Sockets TCP — la comunicación entre cliente y servidor

#### Por qué necesitamos sockets

El cliente y el servidor son **dos programas independientes que corren en máquinas
distintas**: el usuario en su computadora, el servicio en un servidor. Esa separación
es la que descarta todos los demás mecanismos de comunicación que vimos en la materia.
Dos procesos en la misma máquina pueden comunicarse por pipes, FIFOs, memoria
compartida o archivos, porque comparten el mismo sistema operativo y el mismo sistema
de archivos. En máquinas distintas no comparten nada: no hay memoria común, no hay
sistema de archivos común, no hay un kernel que los conozca a los dos.

Lo único que las une es la red. Y la interfaz que el sistema operativo ofrece a los
programas para usar la red es, precisamente, el **socket**.

#### Qué es un socket

Un socket es un **punto de conexión**: la representación, dentro de un programa, de un
extremo de una comunicación. El sistema operativo lo entrega como un descriptor, igual
que un archivo abierto, y el programa lo usa de forma parecida — escribe en él para
enviar información y lee de él para recibirla. Todo el trabajo sucio (partir la
información en paquetes, enviarlos por la placa de red, reordenarlos al llegar,
reclamar los que se perdieron) queda del lado del kernel.

La analogía útil es el teléfono: el servidor deja una línea abierta esperando llamadas,
el cliente marca el número, y una vez establecida la comunicación cualquiera de los dos
puede hablar y escuchar.

#### Direcciones y puertos: cómo se ubica un servicio

Para que un cliente encuentre al servidor hacen falta **dos datos, no uno**:

- La **dirección IP** identifica a la *máquina* dentro de la red.
- El **puerto** identifica al *programa* dentro de esa máquina.

El puerto existe porque una misma computadora corre muchos servicios de red al mismo
tiempo: un servidor web, una base de datos, SSH, y nuestro servidor de imágenes. Todos
comparten la misma IP. Cuando llega información desde afuera, el sistema operativo
necesita saber **a cuál de todos entregársela**, y el número de puerto es exactamente
ese dato de ruteo interno. Siguiendo la analogía telefónica: la IP es el número de la
empresa, el puerto es el número de interno.

Un puerto es un número de 16 bits, o sea del 0 al 65535. Los menores a 1024 están
reservados para servicios estándar (80 para HTTP, 443 HTTPS, 22 SSH) y en Linux
requieren privilegios de root para usarse. Nuestro servidor usa por defecto un puerto
alto — el **9000**, configurable con `--port` — justamente para no necesitar
privilegios especiales.

El cliente **también** ocupa un puerto, pero no lo elige: cuando se conecta, el sistema
operativo le asigna automáticamente uno libre del rango alto (puerto *efímero*). El
cliente no necesita una dirección conocida porque nadie lo llama a él; solo necesita
una dirección de retorno para que las respuestas encuentren el camino de vuelta.

#### Cómo un solo puerto atiende a muchos clientes

Acá aparece la duda natural: si todos los clientes se conectan al puerto 9000, ¿no se
mezclan las conversaciones?

No, y la razón es que **una conexión TCP no se identifica por un puerto sino por cuatro
datos**: IP de origen, puerto de origen, IP de destino y puerto de destino. Como cada
cliente recibe un puerto efímero distinto, cada conexión forma una combinación única,
aunque todas compartan la IP y el puerto del servidor. El kernel usa esa cuádrupla para
saber a qué conversación pertenece cada paquete que llega.

En el código eso se refleja en dos tipos de socket distintos, y conviene tenerlo claro
porque es una confusión frecuente:

1. El servidor crea un **socket de escucha**, le asigna la dirección y el puerto
   (`bind`) y lo pone a esperar (`listen`). Este socket **nunca transporta datos de la
   aplicación**: su único trabajo es recibir solicitudes de conexión.
2. Cada vez que llega una, `accept` devuelve un **socket nuevo y distinto**, dedicado
   exclusivamente a ese cliente. Por ahí sí viaja la información. Mientras tanto, el
   socket de escucha queda libre para aceptar la conexión siguiente.

Con `asyncio.start_server` todo esto está encapsulado: el framework mantiene el socket
de escucha y, por cada cliente que llega, ejecuta nuestra corrutina entregándole el
`reader` y el `writer` de la conexión ya establecida.

#### Por qué TCP y no UDP

TCP es un protocolo **orientado a conexión y confiable**. Antes de transmitir, los dos
extremos establecen la conexión, y a partir de ahí TCP garantiza que la información
llegue **completa, en el mismo orden en que se envió y sin duplicados**: numera lo que
manda, espera confirmaciones, retransmite lo que se perdió y regula la velocidad si el
receptor no da abasto.

UDP no ofrece nada de eso. Envía datagramas sueltos, sin conexión previa y sin
garantías: pueden perderse, llegar desordenados o duplicados, y nadie se entera.

Para este sistema TCP es la única opción razonable, por dos motivos:

- **Una imagen no tolera pérdidas.** Si falta un fragmento en el medio, el archivo
  queda corrupto e inservible. Es muy distinto de una videollamada, donde perder un
  cuadro se nota apenas y no vale la pena retransmitirlo — ese es el terreno de UDP.
- **Una imagen no entra en un datagrama.** UDP tiene un tamaño máximo por datagrama de
  unas decenas de kilobytes (y en la práctica conviene mantenerse muy por debajo). Una
  foto de 3 MB habría que partirla a mano, numerar cada parte, detectar cuáles faltan,
  volver a pedirlas y reensamblarlas en orden al llegar. Todo eso es, literalmente, lo
  que TCP ya hace y bien probado. Reimplementarlo sería reinventar la rueda, con la
  garantía casi segura de hacerlo peor.

#### Por qué sockets directos y no HTTP con un framework

HTTP no es una alternativa *a* los sockets: HTTP **corre sobre** sockets TCP. Usar un
framework web no eliminaría esta capa, solo la escondería detrás de una biblioteca.

Trabajar directamente con sockets nos da tres cosas. Primero, definimos nuestro propio
protocolo (sección 3), ajustado exactamente a lo que necesitamos: cuatro tipos de
mensaje y nada más. Segundo, la imagen viaja **tal cual es**, sin transformaciones
intermedias — enviarla por HTTP obligaría a usar `multipart/form-data` o a codificarla
en base64, que la infla un 33% de puro relleno. Y tercero, es el contenido central de
la materia: la consigna pide demostrar el manejo de sockets, no el uso de un framework
que los oculte.

El costo de esta decisión, para decirlo con honestidad, es la interoperabilidad: solo
nuestro cliente puede hablar con nuestro servidor, un navegador no. Como el cliente
también es parte del proyecto, no nos afecta.

### 2.2 El servidor — asyncio

#### El problema

El servidor tiene que atender **muchos clientes al mismo tiempo**. Un servidor que
atendiera de a uno sería inaceptable: mientras recibe la foto de un cliente —lo que
puede llevar varios segundos si la conexión es lenta o la imagen grande— el resto
quedaría en espera, sin siquiera poder conectarse.

Y hay que notar de qué está hecha esa espera: el servidor **no está calculando nada**
durante esos segundos. Está esperando que la red le entregue la información, que es
lo mismo que decir que está sin hacer nada. Esa es la clave que define la solución.

#### Las opciones y por qué las descartamos

La solución tradicional es **un hilo (o un proceso) por cliente**. Funciona, pero
paga un precio: cada hilo reserva su propia memoria de pila y obliga al sistema
operativo a alternar entre todos ellos, un costo que crece con cada cliente conectado.
Además, si varios hilos tocan datos compartidos hay que sincronizarlos con locks, con
todos los problemas que eso trae (condiciones de carrera, interbloqueos).

En Python hay un motivo extra: el **GIL** (Global Interpreter Lock) es un candado del
intérprete que impide que dos hilos ejecuten código Python simultáneamente. Es decir
que, para trabajo de CPU, los hilos ni siquiera dan el paralelismo que uno esperaría.
Pagamos el costo de administrarlos sin obtener el beneficio.

#### La solución: asyncio

`asyncio` parte de la observación de arriba: si el servidor pasa casi todo el tiempo
esperando, no necesita más hilos — necesita **aprovechar las esperas**.

El mecanismo es el **event loop**: un único hilo que va alternando entre muchas tareas
(*corrutinas*). Cuando una corrutina llega a un `await` —"acá me quedo esperando que
llegue la imagen"— en lugar de bloquear el programa entero le devuelve el control al
loop, que aprovecha para avanzar con otro cliente. Cuando la información finalmente
llega, el loop retoma esa corrutina exactamente donde había quedado.

El resultado es que un solo proceso, con un solo hilo, sostiene cientos de conexiones
simultáneas. Sin hilos que administrar y, sobre todo, **sin locks**: como hay un único
hilo de ejecución, dos corrutinas nunca pueden estar modificando la misma estructura
en el mismo instante. Desaparece toda una categoría de errores.

#### La condición que hace que esto funcione

Todo el esquema se apoya en un supuesto: **el trabajo del servidor es de espera, no de
cálculo**. Recibir una imagen, guardarla, responder — todo es esperar a la red o al
disco.

Si el servidor además procesara las imágenes, el esquema se derrumbaría: mientras
detecta caras (varios segundos de CPU pura, sin ningún `await` de por medio) el event
loop no puede alternar a otra tarea, y **todos** los clientes conectados quedarían
congelados. Esta es exactamente la razón por la que el procesamiento vive en los
workers y no acá. La arquitectura entera se sigue de este único hecho.

Hay un caso más chico del mismo problema: encolar una tarea en Celery implica
comunicarse con Redis, y esa operación bloquea. Son milisegundos, pero milisegundos
en los que el loop no atiende a nadie. Se resuelve con `asyncio.to_thread(...)`, que
la ejecuta en un hilo aparte y devuelve el control al loop mientras tanto.

### 2.3 La cola de tareas — Celery + Redis

#### La idea

El servidor no procesa la imagen: deja anotado el pedido —"hay que aplicar `anonymize`
al trabajo a3f…"— y responde al cliente enseguida. Ese pedido queda en una **cola**, y
del otro lado hay procesos independientes esperando trabajo que lo van a tomar.

La cola es lo que permite que las dos mitades del sistema convivan a ritmos distintos:
el servidor produce pedidos en milisegundos, los workers los consumen en segundos, y
la diferencia se absorbe en la cola en vez de trabar a nadie.

#### Las tres piezas de Celery

Conviene distinguirlas porque cumplen roles diferentes:

- **El broker (Redis)** es el buzón de tareas pendientes. Guarda los pedidos hasta que
  algún worker los reclame. Si en ese momento no hay ningún worker vivo, los pedidos
  simplemente esperan; cuando uno arranca, se los lleva. Nada se pierde por el camino.
- **Los workers** son procesos Python (`celery -A app worker`) que se conectan al
  broker, toman tareas, las ejecutan e informan el resultado.
- **El result backend (también Redis)** es el archivador: registra en qué estado está
  cada tarea —pendiente, ejecutando, terminada, fallada— y qué devolvió. Es lo que
  hace posible que el cliente pregunte por su trabajo y el servidor sepa contestar,
  aunque el worker que lo ejecutó ya no exista.

#### Por qué una cola distribuida y no un pool de procesos

`multiprocessing.Pool` resolvería el paralelismo, pero los procesos del pool son
**hijos del servidor**, y de ahí salen sus tres limitaciones. Si el servidor se
reinicia, se lleva puesto todo lo que estaba en curso. Los procesos tienen que vivir
en la misma máquina, así que la única forma de crecer es conseguir una computadora más
grande. Y si una tarea falla, nadie la reintenta.

Con la cola, servidor y workers son programas **independientes que ni se conocen**: lo
único que comparten es el buzón. Se puede reiniciar el servidor mientras los workers
siguen trabajando, sumar workers en otras máquinas, y Celery reintenta por su cuenta
lo que falla. Crecer es levantar más procesos, sin tocar una línea de código.

Además, es lo que pide la consigna: una **cola de tareas distribuidas**, no un pool
local.

#### Por qué Redis y no RabbitMQ

RabbitMQ es más potente para mensajería compleja (ruteos elaborados, prioridades), pero
acá no necesitamos esa potencia: tenemos un solo tipo de flujo y ninguna regla de ruteo.
Lo que sí valoramos es la simplicidad, y ahí Redis gana: cubre los dos roles —broker y
result backend— con un único servicio y prácticamente sin configuración.

#### Qué viaja por la cola (y qué no)

Por la cola viajan **rutas de archivo, nunca imágenes**. El broker está diseñado para
mensajes de control de unos pocos bytes; hacerle transportar 3 MB por tarea lo
convertiría en el cuello de botella del sistema y consumiría memoria de más.

Las imágenes viajan por el volumen compartido (sección 2.6) y el mensaje solo indica
*dónde* están. Es la misma idea que aplicamos en el resto del diseño: cada dato por el
camino que le corresponde.

### 2.4 Los workers — Celery + Pillow + OpenCV

**Qué hacen**: ejecutan las operaciones de la aplicación, una tarea por operación
(`inspect`, `anonymize`, `clean`, `convert`, `compress`). La detección de caras usa
OpenCV con cascadas de Haar; el difuminado, el borrado de metadatos y la recompresión
usan Pillow.

#### Acá está el paralelismo real

Cada worker es un **proceso independiente**, con su propio intérprete de Python y, por
lo tanto, su propio GIL. Esa es la diferencia decisiva con los hilos: cuatro workers
procesan cuatro imágenes literalmente al mismo tiempo, en cuatro núcleos distintos, sin
ningún candado compartido que los serialice.

Vale la pena comparar las dos mitades del sistema, porque resume el criterio de todo el
diseño:

- El **servidor** usa `asyncio` y obtiene **concurrencia**: muchas tareas en curso,
  turnándose en un solo hilo. Es lo correcto cuando el trabajo consiste en esperar.
- Los **workers** son procesos y obtienen **paralelismo**: varias tareas ejecutándose
  de verdad al mismo tiempo. Es lo correcto cuando el trabajo consiste en calcular.

Usar la herramienta equivocada en cualquiera de los dos lados arruinaría el sistema:
procesar imágenes dentro del event loop lo congelaría, y levantar un proceso por
cliente conectado sería un derroche de memoria para trabajo que es pura espera.

#### La operación `sanitize`

Se implementa con `chain` de Celery: una cadena anonymize → clean → compress donde
cada etapa recibe como entrada la salida de la anterior, como una línea de montaje.

Se usa `chain` y no `group` porque las etapas son **secuenciales por naturaleza**: no
tiene sentido comprimir antes de haber difuminado las caras, ya que cada paso trabaja
sobre el resultado del anterior. `group` sería lo apropiado si las subtareas fueran
independientes entre sí y pudieran ejecutarse en paralelo.

#### Manejo de errores

Si una tarea falla por una causa transitoria —por ejemplo, que el archivo todavía no
sea visible en el volumen compartido—, Celery la reintenta automáticamente una cantidad
acotada de veces. Si el fallo es definitivo, como una imagen corrupta, la tarea queda
marcada como fallada junto con el motivo, y el cliente lo ve al consultar el estado.

Que un worker se caiga a mitad de una tarea tampoco es un problema, **pero requiere
configurarlo**. Por defecto Celery confirma el mensaje al broker apenas lo reserva,
antes de ejecutarlo, de modo que si el proceso muere en el medio la tarea se pierde.
Con `task_acks_late = True` la confirmación se posterga hasta que la tarea termina:
mientras tanto el mensaje sigue pendiente y, si el worker desaparece, el broker se lo
entrega a otro.

El costo de esa elección es que una tarea podría ejecutarse dos veces (si el worker
muere justo después de terminar, pero antes de confirmar). Lo asumimos porque nuestras
operaciones son **idempotentes**: reprocesar la misma imagen produce el mismo
resultado y lo sobrescribe en la misma ruta, sin efectos acumulativos.

### 2.5 El auditor — un proceso aparte, comunicado por IPC

#### El problema

Queremos un historial permanente: quién pidió qué, cuándo, y cómo terminó. Eso implica
escribir en una base de datos, y ahí aparecen dos obstáculos.

El primero ya lo conocemos: escribir en disco es una operación de espera, y si el
servidor la hiciera dentro del event loop, cada escritura congelaría a **todos** los
clientes conectados. Por poco que dure, es exactamente lo que la arquitectura busca
evitar.

El segundo es propio de SQLite: tolera mal que varios procesos escriban al mismo
tiempo. Cuando dos intentan hacerlo, uno recibe el conocido error *database is locked*.

#### La solución

Al arrancar, el servidor crea un **proceso hijo** —el auditor— y se comunica con él
por una **`multiprocessing.Queue`**. Cada vez que ocurre algo relevante (un trabajo
recibido, encolado, terminado o fallado), el servidor deposita un evento en la cola
con `put()` y continúa atendiendo. Es una operación casi instantánea y, sobre todo,
**no espera respuesta**: el servidor no necesita saber si el evento ya se guardó.

Del otro lado, el auditor toma los eventos con `get()` y los escribe en SQLite a su
propio ritmo, sin apuro y sin molestar a nadie.

La consecuencia interesante es que este diseño resuelve los dos problemas de arriba
con una sola decisión. El primero, porque la escritura ocurre en otro proceso. Y el
segundo, porque al ser el auditor **el único que escribe** en la base, la concurrencia
sobre SQLite simplemente no existe: no hacen falta locks ni reintentos, no porque los
hayamos manejado bien, sino porque la situación nunca se produce.

#### Por qué `multiprocessing.Queue`

Es un canal de comunicación entre procesos de la misma máquina. Internamente está
construida sobre un pipe —el mismo mecanismo que vimos en clase—, pero agrega dos
comodidades que acá vienen bien: serializa los objetos de Python automáticamente (se
envía un diccionario, no una secuencia de bytes que después hay que interpretar) y es
segura aunque varios procesos escriban a la vez.

Un FIFO habría sido la elección si el auditor fuera un programa externo, escrito en
otro lenguaje o arrancado por separado. Como es un proceso hijo del servidor y ambos
son Python, la Queue le gana en comodidad sin perder nada.

#### Cómo se entera el auditor de que un trabajo terminó

Los workers no pueden avisarle directamente: viven en otros contenedores, y la Queue
solo comunica procesos de la misma máquina emparentados entre sí.

De eso se ocupa el servidor. Mantiene la lista de los trabajos en curso y una corrutina
de fondo que consulta periódicamente su estado en el result backend. Cuando detecta que
uno terminó, envía el evento correspondiente al auditor y lo saca de la lista. Es la
misma estrategia de consulta periódica que usa el cliente, aplicada del lado del
servidor.

### 2.6 El almacenamiento — tres lugares, tres roles

El sistema guarda información en tres sitios distintos, y la separación no es casual:
cada uno tiene características que lo hacen apto para un tipo de dato y malo para los
otros.

- **`storage/` (volumen compartido)** guarda los **archivos**: los originales en
  `uploads/<job_id>/` y los resultados en `results/<job_id>/`. Está montado tanto en
  el servidor como en los workers, y esa es justamente la condición que permite que
  por la cola viajen solo rutas: cuando el worker recibe una, puede abrir el archivo
  directamente.
- **Redis** guarda el estado **vivo** de las tareas: en qué anda cada una ahora mismo.
  Lo administra Celery y es información efímera, que deja de importar poco después de
  que el trabajo termina.
- **SQLite (`jobs.db`)** guarda el historial **permanente**. Se eligió porque es un
  único archivo, sin servidor de base de datos que instalar ni administrar, y porque
  su principal debilidad —los escritores concurrentes— quedó eliminada por el patrón
  de escritor único del auditor.

En resumen: archivos pesados al disco compartido, estado transitorio a Redis, historia
a SQLite. Poner todo en un mismo lugar habría significado, en cada caso, usar una
herramienta para algo que no es lo suyo.

### 2.7 El despliegue — Docker Compose

**Qué hace**: describe todos los servicios del sistema (`server`, `redis`, `worker`, y
opcionalmente `flower`, un panel web para observar la cola en vivo) y los levanta con
un solo comando, con el volumen `storage/` montado donde corresponde.

**Por qué**. La primera razón es de reproducibilidad: el sistema se comporta igual en
cualquier máquina, con OpenCV y todas sus dependencias nativas ya resueltas dentro de
la imagen del worker. Instalar eso a mano es de las tareas más propensas a fallar.

La segunda es más importante para el proyecto: **vuelve demostrable lo que de otro modo
sería una afirmación**. Que el sistema sea distribuido se puede mostrar en vivo —
levantar cuatro workers con `docker compose up --scale worker=4` y ver cómo se reparten
la carga, o matar uno a mitad de un trabajo y ver cómo la tarea se recupera en otro. Es
la diferencia entre decir que la arquitectura tolera fallas y mostrarlo funcionando.

## 3. La comunicación entre cliente y servidor

Los sockets resuelven el transporte: garantizan que lo que un extremo envía llegue
íntegro al otro. Pero no dicen **qué** enviar ni **cómo interpretarlo**. Eso lo define
el protocolo de aplicación, que es lo que diseñamos acá: el idioma en que cliente y
servidor se entienden.

### 3.1 El modelo de diálogo

La comunicación sigue el esquema **pedido → respuesta**, siempre iniciado por el
cliente. El servidor nunca habla por su cuenta: solo responde. Esto encaja
naturalmente con una herramienta de línea de comandos, donde cada ejecución tiene un
objetivo concreto y termina.

Una ejecución del cliente equivale a **una conexión**: se abre al arrancar, se usa
para todos los intercambios que haga falta, y se cierra al terminar. Dentro de esa
conexión pueden ocurrir varios pedidos, siempre **de a uno por vez**: el cliente
envía uno y espera la respuesta completa antes de enviar el siguiente.

Esa decisión —no permitir pedidos superpuestos en una misma conexión— simplifica el
protocolo de manera importante: como cada respuesta corresponde necesariamente al
último pedido enviado, no hacen falta identificadores para saber qué contesta a qué.
Si quisiéramos varios pedidos en vuelo simultáneos, habría que numerarlos y que el
servidor devolviera ese número en cada respuesta. No lo necesitamos: el paralelismo
del sistema está en los workers, no en la conexión.

El caso más completo es `submit --wait`, donde el cliente aprovecha la misma conexión
para toda la secuencia:

```
Cliente                                        Servidor
   │                                              │
   │──── conecta (TCP) ──────────────────────────►│  accept: socket dedicado
   │                                              │
   │──── submit (op + imagen) ───────────────────►│  valida, guarda, encola
   │◄─── {job_id: "a3f…", status: "QUEUED"} ──────│
   │                                              │
   │──── status (job_id) ────────────────────────►│  consulta el result backend
   │◄─── {status: "PROCESSING"} ──────────────────│
   │                          (espera ~1 s)       │
   │──── status (job_id) ────────────────────────►│
   │◄─── {status: "DONE"} ────────────────────────│
   │                                              │
   │──── download (job_id) ──────────────────────►│  lee el resultado del volumen
   │◄─── header + imagen procesada ───────────────│
   │                                              │
   │──── cierra ─────────────────────────────────►│  detecta EOF, libera recursos
```

El cliente consulta el estado periódicamente (*polling*) en lugar de quedarse
esperando un aviso del servidor. Es la opción simple y suficiente: mantiene el
protocolo con un único sentido de iniciativa —el cliente pregunta, el servidor
responde— y evita tener que manejar notificaciones no solicitadas. Como es una espera,
el cliente la hace con `await asyncio.sleep(...)`, sin bloquear.

### 3.2 El problema de delimitar los mensajes

TCP garantiza que la información llegue completa y en orden, pero **no conserva la
noción de "mensaje"**: para TCP la conexión es un flujo continuo, sin marcas que
indiquen dónde termina un envío y empieza el siguiente. Lo que el emisor manda en
tres operaciones puede llegar al receptor en una sola lectura, o al revés.

Esto tiene una consecuencia práctica que hay que resolver sí o sí: si el cliente envía
un pedido seguido de una imagen, el servidor recibe todo pegado y no tiene forma
natural de saber dónde termina el pedido y dónde empieza la foto. Peor aún: no sabe
cuándo dejar de leer, porque la conexión sigue abierta.

La solución estándar es **anunciar la longitud antes del contenido**. Cada mensaje
empieza con un campo de tamaño fijo que dice cuánto mide lo que viene a continuación.
El receptor lee primero ese campo —siempre la misma cantidad de bytes, así que no hay
ambigüedad— y con ese número sabe exactamente cuánto leer después. Se conoce como
*length-prefixed framing* y es lo que hacen, en el fondo, casi todos los protocolos
binarios.

### 3.3 El formato del mensaje

```
+----------------+----------------------+----------------------+
| 4 bytes u32 BE | header JSON (UTF-8)  | payload binario      |
| long. header   |                      | (opcional)           |
+----------------+----------------------+----------------------+
```

Cada mensaje tiene tres partes:

1. **Prefijo de longitud**: 4 bytes con un entero sin signo en *big-endian* (el orden
   de red convencional). Indica cuánto mide el header. Se lee primero y siempre mide
   lo mismo, que es lo que permite arrancar la lectura sin ambigüedad.
2. **Header**: un objeto JSON con los datos del pedido. Siempre incluye `type` (qué se
   pide), `user` (quién lo pide) y `payload_size` (cuántos bytes de contenido binario
   vienen después, 0 si no viene ninguno), más los campos propios de cada tipo.
3. **Payload**: la imagen, tal cual, sin ninguna transformación. Su longitud es la que
   anunció `payload_size`.

**Por qué esta combinación de JSON y binario.** El header es JSON porque los datos del
pedido son estructurados y variables: se lee sin herramientas, se extiende agregando
campos sin romper nada, y Python lo serializa en una línea. El payload, en cambio, va
crudo porque es lo más eficiente posible: cualquier codificación textual (base64, por
ejemplo) lo agrandaría un tercio sin ningún beneficio. Cada parte del mensaje usa el
formato que le conviene.

### 3.4 Los cuatro tipos de pedido

| Tipo (cliente → servidor) | Campos extra                | Payload | Respuesta del servidor            |
|---------------------------|-----------------------------|---------|-----------------------------------|
| `submit`                  | `op`, `params`, `filename`  | imagen  | `{job_id, status: "QUEUED"}`      |
| `status`                  | `job_id`                    | —       | `{job_id, status, error?}`        |
| `download`                | `job_id`                    | —       | header + payload con el resultado |
| `history`                 | `limit`                     | —       | `{jobs: [...]}`                   |

- **`submit`** es el único pedido del cliente que lleva payload. El servidor valida la
  operación y la imagen, la guarda en el volumen compartido, encola la tarea y
  responde de inmediato con el identificador del trabajo. **No espera al resultado**:
  esa respuesta rápida es la que le permite seguir atendiendo a todos los demás.
- **`status`** consulta el estado en el result backend de Celery. Es una operación
  liviana, pensada para ser llamada repetidamente.
- **`download`** es el pedido inverso a `submit`: sin payload de ida, con payload de
  vuelta. El servidor lee el resultado del volumen y lo envía con el mismo formato de
  mensaje.
- **`history`** no toca ni Redis ni el volumen: el servidor se lo pregunta al proceso
  auditor, que es quien tiene el historial en SQLite.

### 3.5 Errores y desconexiones

Cuando algo sale mal, el servidor responde con un mensaje de tipo `error`, con un
código y una descripción: `{type: "error", code, message}`. Los casos previstos son
operación inexistente, imagen corrupta o de formato no soportado, tamaño excedido,
trabajo inexistente, y descarga de un trabajo que todavía no terminó.

La decisión de diseño es que **un error nunca corta la conexión**: es una respuesta
como cualquier otra. El cliente la recibe, la muestra, y la conexión queda disponible.
Cortar sería más simple de programar, pero le impediría al cliente distinguir un
pedido rechazado de una caída del servidor.

Las desconexiones se detectan solas. Si el cliente cierra o se cae, la lectura en el
servidor termina con la conexión vacía (EOF): la corrutina de ese cliente lo detecta,
cierra su socket y libera los recursos, sin afectar en absoluto a las demás. Un
trabajo ya encolado **sigue su curso** aunque el cliente desaparezca — el worker no
sabe ni le importa si quien lo pidió sigue conectado, y el resultado queda disponible
para cuando vuelva a consultarlo.

## 4. Modelo de datos (SQLite)

Dos tablas: los trabajos y sus eventos. Las estadísticas no se guardan: se calculan
con consultas sobre estas tablas cuando se piden (evita datos duplicados que pueden
quedar inconsistentes).

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
importa nada del servidor (solo `common/config`). Esto garantiza que cada componente
pueda desplegarse por separado — que es la gracia de un sistema distribuido.

## 6. Flujo completo de un trabajo (resumen numerado)

1. El cliente parsea los argumentos, valida el archivo y abre el socket TCP.
2. Envía el frame `submit` (header JSON + bytes de la imagen).
3. El servidor (una corrutina por cliente) lee el frame sin bloquear el loop,
   valida, y guarda el original en `storage/uploads/<job_id>/`.
4. Encola la tarea con `asyncio.to_thread(task.delay, ...)` → queda un mensaje chico
   en Redis.
5. Notifica `received` + `queued` al auditor por la `mp.Queue` y responde el `job_id`
   al cliente. (Hasta acá, milisegundos.)
6. Un worker toma la tarea, procesa con Pillow/OpenCV, escribe el resultado en
   `storage/results/<job_id>/` y marca SUCCESS o FAILURE en el result backend.
7. La corrutina de monitoreo del servidor detecta el final y avisa al auditor
   (`done`/`failed`), que lo persiste en SQLite.
8. El cliente consulta `status` y descarga con `download` (o hace todo junto con
   `--wait`).

## 7. Tabla de cumplimiento de requisitos

| Requisito obligatorio | Dónde se cumple |
|---|---|
| Sockets, clientes múltiples concurrentes | `server.py` (asyncio.start_server, TCP) |
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
