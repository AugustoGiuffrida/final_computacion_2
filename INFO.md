# Decisiones de diseño y su justificación

Las decisiones principales del sistema y por qué se tomaron. El detalle completo está en
`docs/`; acá está lo esencial de cada una y, sobre todo, **contra qué alternativa se
decidió**.

---

## 1. Por qué asyncio y no un hilo por cliente

Atender a un cliente es **espera pura**: recibir bytes, escribirlos en disco, mandar la
respuesta. Casi todo el tiempo el servidor está esperando I/O, no calculando.

Con un hilo por cliente eso funcionaría, pero el costo aparece en otro lado: **todo estado
compartido necesitaría candados**. El índice de trabajos y el contador de conexiones los
tocan todas las conexiones a la vez, y con hilos la intercalación puede ocurrir entre dos
instrucciones cualesquiera — `self.connected_clients += 1` son tres operaciones y dos
hilos pueden perder un incremento.

Con corrutinas eso no puede pasar: **una corrutina solo cede el control en un `await`**, y
esos puntos están a la vista en el código. **No hay un solo candado en todo el proyecto.**

*Lo que costó:* el canal con el proceso hijo (`intake_channel.py`) es más complejo de lo
que sería con hilos, porque hay que envolver una API bloqueante. Es un archivo; los
candados habrían estado en todos.

---

## 2. Por qué el proceso de ingreso es un proceso y no un hilo

Es la pieza que **abre las imágenes que manda el cliente**. Pillow y OpenCV son por dentro
código nativo en C: una imagen malformada no produce una excepción de Python, produce la
muerte del intérprete.

Si eso pasara dentro del servidor, se caerían **todas las conexiones abiertas**. Un hilo no
protege de nada acá: comparte el espacio de memoria y muere con el proceso.

El aislamiento ante fallas es **lo único que un proceso ofrece y un hilo no**, y es la
razón entera de esta decisión. La regla que se deriva: **el proceso principal nunca abre
una imagen.**

Y el aislamiento se completa con la recuperación: cuando el hijo muere, el servidor lo
detecta —el pipe da fin de archivo—, rehace el canal y lo relanza. Sin eso, aislar
serviría para no caerse pero el servicio quedaría roto igual.

---

## 3. Por qué un `Pipe` y no dos `mp.Queue`

Acá hay **exactamente dos procesos** hablando en las dos direcciones, que es para lo que
sirve un pipe. Una `mp.Queue` está construida encima de un pipe **más un candado y un hilo
alimentador**, y todo eso resuelve un problema que este sistema no tiene: varios
productores o consumidores compartiendo el canal.

El material de la cátedra lo plantea igual: *"Pipes: simples, rápidos, para 2 procesos.
Queues: escalables, seguras, pero con mayor overhead"*.

Hubo además una razón práctica descubierta probando: **una `mp.Queue` protege la lectura
con un semáforo compartido**, y si el hijo muere mientras lo tiene tomado, queda tomado
para siempre y la cola no sirve más. Con la primera versión, matar al hijo dejaba al
sistema en un estado del que no se recuperaba. Un pipe es un descriptor: no deja nada
tomado.

*Lo que se acepta:* un pipe es punto a punto. Si algún día hubiera varios procesos de
ingreso, ahí sí convendría una cola.

---

## 4. Por qué Celery y Redis, y no `multiprocessing.Pool`

Un `Pool` resolvería el paralelismo, pero sus procesos son **hijos del servidor**, y de ahí
salen sus tres límites:

1. Si el servidor se reinicia, se lleva puesto todo lo que estaba en curso.
2. Los procesos tienen que vivir en la misma máquina.
3. Si una tarea falla, nadie la reintenta.

Con Celery, un trabajo encolado **sobrevive en Redis** aunque el servidor no esté, y lo
toma el próximo worker que arranque — incluso en otra máquina. Con `task_acks_late`, un
worker que muere a mitad de una imagen no pierde el trabajo: el broker se lo da a otro.

Redis cumple dos papeles con bases distintas: la 0 es el **broker** (los mensajes "hay
trabajo") y la 1 el **result backend** (en qué anda cada tarea).

---

## 5. Por qué existe un monitor consultando el estado

Los workers **no pueden avisarle al servidor**: viven en otro contenedor o en otra máquina,
y no hay canal de vuelta. El result backend es el lugar común — el worker deja ahí su
estado y alguien de este lado tiene que ir a preguntar.

Por eso hay una corrutina de fondo que consulta cada medio segundo los trabajos en vuelo y
traduce los estados de Celery a los del protocolo: `STARTED` → `PROCESSING`, `SUCCESS` →
`DONE`, `FAILURE` → `ERROR`.

Como el cliente de Redis es bloqueante, esas consultas van a un hilo del pool con
`asyncio.to_thread`: un Redis caído no debe congelar el event loop.

---

## 6. Por qué SQLite y no MySQL o PostgreSQL

**Un solo escritor por diseño.** La base la escribe únicamente el proceso de ingreso; el
servidor solo lee. La limitación clásica de SQLite —un escritor a la vez— no es un
obstáculo acá porque el sistema ya está organizado así.

Un servidor de base de datos resolvería el problema de los escritores concurrentes, que
este sistema no tiene, a cambio de un servicio más que instalar, configurar y mantener.

La regla se hace cumplir sola: el servidor abre la base **en modo solo lectura**, y SQLite
rechaza cualquier escritura que intente. No es una convención, es el motor.

El modo WAL permite que el servidor lea mientras el hijo escribe, sin que ninguno espere
al otro.

---

## 7. Por qué la base está fuera del volumen compartido

El volumen de imágenes tiene que ser un **sistema de archivos de red**, para que los
workers puedan correr en otra máquina — sin eso, la cola distribuida sería distribuida
solo en el papel.

Pero SQLite **desaconseja los sistemas de archivos de red**: el bloqueo de archivos no es
confiable sobre NFS, y el modo WAL necesita memoria compartida entre procesos, que NFS no
provee.

La base no lo necesita: la escribe el proceso de ingreso y la lee el principal, que son
**padre e hijo y viven siempre en la misma máquina**. Por eso `storage/` va por red y
`data/jobs.db` en disco local.

---

## 8. Por qué el protocolo delimita los mensajes con un prefijo de longitud

TCP entrega **un flujo continuo de bytes**, sin marcas de dónde termina un mensaje y
empieza el siguiente. Es un problema inevitable, no una elección.

Cada mensaje viaja como: 4 bytes con la longitud del header, el header en JSON, y el
payload binario si lo hay. Con eso el receptor siempre sabe cuánto leer.

De ahí sale una regla que atraviesa el servidor: **un pedido se consume completo, aunque
se vaya a rechazar**. Los bytes que quedaran sin leer serían interpretados como el prefijo
de longitud del mensaje siguiente, y el diálogo quedaría desfasado sin forma de detectarlo.

*JSON y no pickle,* tanto acá como en la cola: por el canal viajan **datos, nunca objetos
ejecutables**.

---

## 9. Cómo se identifica un trabajo, y por qué UUID

Un `job_id` es un **UUID versión 4**, no un contador. Dos razones: no es enumerable —nadie
puede pedir el trabajo 41 y ver el ajeno— y no necesita coordinación para generarse.

La **deduplicación** usa una identidad distinta: cuatro campos, no solo el contenido.

| Campo | Por qué está |
|---|---|
| `sha256` del contenido | dos archivos con el mismo contenido son la misma imagen, se llamen como se llamen |
| operación | difuminar y comprimir dan resultados distintos |
| parámetros | la misma foto con `strength=15` y con `strength=30` no da lo mismo |
| **usuario** | **por privacidad**: reutilizar el trabajo de otro le revelaría que procesó esa misma imagen |

Los parámetros se comparan como texto en forma canónica —JSON con las claves ordenadas—
para que `{"mode":"blur","strength":15}` y `{"strength":15,"mode":"blur"}` sean iguales.

Solo se reutilizan trabajos en `DONE`: uno que falló no sirve, y uno en curso se procesa de
nuevo en vez de esperar al que ya está corriendo.

---

## 10. Por qué `sanitize` es una cadena de tareas y no una tarea grande

`sanitize` encadena `clean` → `anonymize` → `compress` con un `chain` de Celery, donde cada
etapa recibe el resultado de la anterior. Es la demostración de **composición de tareas**.

El trabajo de cada operación vive en una función suelta, y tanto la tarea individual como
la etapa de la cadena la llaman. Un arreglo en una función arregla las dos.

**El orden se descubrió probándolo.** La limpieza de metadatos va primera porque es la
única etapa que puede contar cuántos había: cualquier etapa que guarde la imagen antes se
los lleva puestos sin decirlo, y la limpieza informaría cero. De paso, así los archivos
intermedios nunca llevan las coordenadas GPS.

*Lo que costó:* un `chain` devuelve el asidero de la **última** tarea, que figura `PENDING`
mientras corren las anteriores. Sin tratamiento, un saneamiento se vería `QUEUED` durante
casi todo su procesamiento. El monitor camina los `.parent` hacia atrás para detectar que
la cadena ya arrancó.

---

## 11. Por qué OpenCV 4 y cascadas Haar, y no la versión 5

OpenCV 5 **eliminó `CascadeClassifier`** y sus cascadas del paquete: solo deja un detector
basado en redes neuronales que exige descargar un modelo aparte.

Se fijó la versión 4 por dos motivos. **Las cascadas vienen incluidas**: cero archivos
binarios en el repositorio y cero descargas en el despliegue. Y el método es **explicable
en dos minutos** —ventanas deslizantes sobre la imagen en escala de grises,
características de contraste, y una cascada de etapas donde cada una descarta rápido lo
que no es cara— frente a una caja negra con pesos entrenados.

La intensidad del cubrimiento se aplica **relativa al tamaño de cada cara**, no en píxeles
absolutos: así `--strength 15` tapa igual una foto de celular y una de cámara.

---

## 12. Qué se probó y cómo

176 pruebas sobre `unittest`, con una decisión de fondo: **cada pieza se prueba con sus
vecinos sustituidos, y las integraciones reales se verifican a mano**.

- Las pruebas del servidor levantan **servidores TCP reales** en puertos libres y los
  interrogan con el mismo cliente que usa el usuario.
- Las del canal lanzan **procesos hijos de verdad**, pero con un cuerpo falso: así se puede
  provocar un hijo que se muera, que tarde de más o que conteste desordenado — cosas que
  el hijo real no permite forzar.
- Las de la base usan **SQLite de verdad** en archivos temporales. Simular una base no
  ahorraría nada y taparía lo que hay que verificar.
- La cola se sustituye por un doble: las pruebas no dependen de que Redis esté corriendo.

Varias pruebas existen porque un bug apareció primero: el pipe roto que cortaba la conexión
del cliente equivocado, el mensaje corrupto que mataba al receptor en silencio, el falso
positivo del detector al reescalar.
