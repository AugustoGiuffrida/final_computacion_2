# Guion de demostración

Recorrido corto para mostrar el sistema funcionando: **un solo envío de imagen**, siguiendo
qué hace cada parte. Al final hay un apéndice con lo que se puede mostrar si hay preguntas.

Hacen falta **dos terminales**: una con el servidor a la vista —su registro es la mitad de
la demostración— y otra para los comandos. Todo se ejecuta desde la raíz del repositorio.

---

## Quién es quién

Tres piezas, cada una en su proceso:

```
   TERMINAL 3                      TERMINAL 1
   ┌──────────┐   socket TCP    ┌──────────────┐      pipe      ┌──────────────┐
   │ CLIENTE  │ ──────────────► │   SERVIDOR   │ ─────────────► │  INGRESO     │
   │          │ ◄────────────── │  (principal) │ ◄───────────── │  (hijo)      │
   └──────────┘                 └──────────────┘                └──────────────┘
    otra máquina                        │  ▲                      revisa la imagen
    en principio                        │  │ Redis                sin bloquear a nadie
                                        ▼  │
                                 ┌──────────────┐   TERMINAL 2
                                 │   WORKER     │   procesa la imagen; puede estar
                                 │  (Celery)    │   en otra máquina
                                 └──────────────┘
```

| Pieza | Qué hace | Qué **no** hace |
|---|---|---|
| **Cliente** | arma el pedido, manda la imagen, muestra la respuesta | nada de lógica del servicio |
| **Servidor** | atiende N clientes a la vez, valida, guarda el archivo, encola y responde | **nunca abre una imagen** |
| **Ingreso** | verifica que sea una imagen, calcula su SHA-256, busca duplicados | no habla con los clientes |
| **Worker** | procesa la imagen: cubre caras, borra metadatos, comprime | no conoce el protocolo ni a los clientes |

La regla que explica el reparto: **el que atiende a los clientes nunca toca contenido no
confiable.** Abrir una imagen que mandó cualquiera puede tumbar el proceso, y si eso pasara
en el servidor se caerían todas las conexiones abiertas. Por eso se hace en procesos
aparte —el ingreso y los workers—, que pueden morirse sin arrastrar a nadie.

El **ingreso** y el **worker** se diferencian en algo más que la tarea: el ingreso es hijo
del servidor y le contesta por un pipe, en la misma máquina y en milisegundos. El worker
está del otro lado de Redis, puede vivir en otro contenedor u otra máquina, y tarda
segundos. Por eso uno responde una pregunta y el otro deja su resultado en un lugar donde
alguien lo va a ir a buscar.

---

## Preparación

Cuatro cosas, en orden. Todas se pueden repetir sin romper nada.

**1. Las dependencias** (solo la primera vez, o si cambió `requirements.txt`):

```bash
./venv/bin/pip install -r requirements.txt
```

**2. Redis**, que es el intermediario entre el servidor y los workers:

```bash
docker run -d --name redis-final -p 6380:6379 redis:7-alpine   # solo la primera vez
docker start redis-final                                        # las siguientes veces
docker exec redis-final redis-cli ping                          # tiene que decir PONG
```

| Parte | Qué hace |
|---|---|
| `run -d` | crea el contenedor y lo deja corriendo de fondo |
| `--name redis-final` | un nombre fijo, para poder frenarlo y arrancarlo por nombre |
| `-p 6380:6379` | el 6379 de adentro sale como 6380 afuera (el 6379 de esta máquina está ocupado) |
| `exec … redis-cli ping` | ejecuta el cliente de Redis adentro del contenedor: la prueba de vida |

**3. Las imágenes de prueba** ya están en el repositorio, en `img_test/`. No hay que
generar nada:

```bash
ls img_test/
```

| Archivo | Qué tiene | Para qué |
|---|---|---|
| `grupo.jpg` | una foto grupal con 12 caras y 2 metadatos EXIF | el envío principal |
| `grupo_copia.jpg` | copia byte a byte del anterior | la deduplicación: mismo contenido, otro nombre |
| `paisaje.jpg` | un paisaje, sin personas | que no haya caras es un resultado válido |
| `rota.jpg` | `grupo.jpg` sin sus últimos 5 bytes | el rechazo por imagen corrupta |

Los 2 metadatos de `grupo.jpg` son los que deja una cámara de verdad: marca y software.
Es una foto de grupo a propósito: se ve mejor el detector encontrando **doce** caras y
cubriéndolas todas. Si tenés una foto propia con caras, usala en lugar de esta.

**4. Empezar de cero**, para que lo que aparezca sea solo lo de hoy:

```bash
find storage/uploads storage/results -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
rm -f data/jobs.db data/jobs.db-shm data/jobs.db-wal
```

La primera línea vacía el volumen de imágenes conservando los `.gitkeep` (los archivos
con los que git mantiene las carpetas vacías); la segunda borra la base y los dos
archivos auxiliares del modo WAL. Sin comodines a propósito: `rm storage/uploads/*`
falla en zsh cuando la carpeta ya está vacía.

---

## El flujo completo, en resumen

Los comandos en orden, ya explicados en detalle en los pasos siguientes. Esta secuencia
está verificada de punta a punta:

```
terminal 1   ./venv/bin/python -m app.server --port 9876
terminal 2   ./venv/bin/celery -A app.worker.celery_app worker --loglevel=info
terminal 3   ./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
                 --action submit --file img_test/grupo.jpg --op sanitize \
                 --mode blur --quality 70 --max-size 900 --wait -o /tmp/saneada.jpg
terminal 3   (verificar: el bloque de python del paso 3)
terminal 3   ./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 --action history
             Ctrl-C en la terminal 1 y en la 2
```

---

## Paso 1 — Arrancar el servidor y el worker

En la **terminal 1**, el servidor:

```bash
./venv/bin/python -m app.server --port 9876
```

| Parámetro | Qué hace |
|---|---|
| `--port 9876` | puerto de escucha (por defecto 9000; se usa otro para no chocar con nada) |
| `--host` | *se omite a propósito*: sin él escucha en **todas** las interfaces, IPv4 e IPv6 |
| `--storage-dir` | *también se omite*: las imágenes van a `storage/` dentro del repositorio, que es lo cómodo para mostrarlas |

En la **terminal 2**, el worker:

```bash
./venv/bin/celery -A app.worker.celery_app worker --loglevel=info
```

| Parte | Qué hace |
|---|---|
| `-A app.worker.celery_app` | dónde está la instancia de Celery con su configuración |
| `worker` | este proceso ejecuta tareas (Celery tiene otros modos) |
| `--loglevel=info` | para ver cada tarea entrando y saliendo |

**Lo que aparece en la terminal 1:**

```
escuchando en 0.0.0.0:9876 (AF_INET)
escuchando en :::9876 (AF_INET6)
proceso de ingreso lanzado (pid 59616)
[ingreso] proceso de ingreso en marcha
```

**Y en la terminal 2**, el worker lista lo que sabe hacer:

```
[tasks]
  . app.worker.tasks.anonymize
  . app.worker.tasks.clean
  . app.worker.tasks.compress
  . app.worker.tasks.convert
  . app.worker.tasks.inspect
  . app.worker.tasks.sanitize_cover
  . app.worker.tasks.sanitize_shrink
  . app.worker.tasks.sanitize_strip
celery@… ready.
```

Las tres `sanitize_*` son las etapas de la cadena, no operaciones que el cliente pueda
pedir sueltas.

Cuatro líneas y ya están las tres cosas que importan: **dos sockets** —uno por familia de
direcciones, mismo puerto—, y **el proceso hijo**, que se lanza al arrancar y no cuando
llega el primer cliente.

Para verlos desde afuera, en la **terminal 2**:

```bash
SERVIDOR=$(pgrep -f "app.server --port 9876")
lsof -nP -p $SERVIDOR -a -iTCP -sTCP:LISTEN
ps -o pid,ppid,args -p $SERVIDOR $(pgrep -P $SERVIDOR) | sed 's|[^ ]*/Python ||'
```

```
Python  59613 augusto  7u  IPv6  …  TCP *:9876 (LISTEN)
Python  59613 augusto  8u  IPv4  …  TCP *:9876 (LISTEN)

  PID  PPID ARGS
59613 59610 -m app.server --port 9876
59615 59613 -c from multiprocessing.resource_tracker import main;main(11)
59616 59613 -c from multiprocessing.spawn import spawn_main; spawn_main(…)
```

Un solo proceso con **dos descriptores de escucha**, y dos hijos: el de ingreso —cuyo PID
es el que anunció el registro— y un `resource_tracker` que `multiprocessing` crea por su
cuenta para llevar la cuenta de los recursos compartidos.

---

## Paso 2 — Enviar la imagen

En la **terminal 3**:

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file img_test/grupo.jpg --op sanitize --mode blur --quality 70 \
    --max-size 900 --wait -o /tmp/saneada.jpg
```

| Parámetro | Qué hace |
|---|---|
| `--user ana` | con qué usuario se declara el trabajo (no hay autenticación) |
| `--host 127.0.0.1 --port 9876` | a qué servidor conectarse |
| `--action submit` | enviar una imagen: el único pedido que lleva archivo |
| `--op sanitize` | la operación estrella: cubre caras, borra metadatos y comprime |
| `--mode blur --quality 70 --max-size 900` | parámetros de las etapas: cómo cubrir, con cuánta calidad recomprimir y a qué lado máximo reducir |
| `--wait` | esperar a que termine y descargar el resultado, en vez de volver enseguida |
| `-o /tmp/saneada.jpg` | dónde guardar lo que vuelva |

**Lo que ve el cliente:**

```
╭──────────────── Imagen aceptada ────────────────╮
│   Trabajo  cf4739e2-d3aa-4fa7-b930-68d7b88bd3f1 │
│    Estado  ◷ QUEUED                             │
│ Operación  sanitize                             │
│    Imagen  grupo.jpg (312.2 KB)               │
╰─────────────────────────────────────────────────╯

╭──────── Resultado ─────────╮
│ Metadata removed  2        │
│ Caras detectadas  12       │
│             Mode  blur     │
│  Tamaño original  312.2 KB │
│     Tamaño final  78.3 KB  │
│    Saved percent  75       │
╰────────────────────────────╯

✓ Resultado guardado en /tmp/saneada.jpg (78.3 KB)
```

El informe junta lo que devolvió **cada etapa de la cadena**: los metadatos que borró la
primera, las caras que cubrió la segunda, y el ahorro que logró la tercera.

**Y en la terminal 1, el recorrido completo:**

```
conectado 127.0.0.1:54968 (1 en total)                  ← 1. el servidor acepta
pedido 'submit' (511102 bytes de payload)               ← 2. lee el header
[ingreso] cf4739e2-… revisado: JPEG, 8f3a1c9e2b04       ← 3. el HIJO revisa
trabajo cf4739e2-… encolado (tarea 0698706e-…)          ← 4. va a la cola
trabajo cf4739e2-… aceptado: 'sanitize' sobre 'grupo.jpg'
trabajo cf4739e2-… en proceso                           ← 5. un worker lo tomó
trabajo cf4739e2-… terminado                            ← 6. la cadena completó
descarga de cf4739e2-…: out.jpg                         ← 7. el cliente lo baja
```

Línea por línea:

1. **El servidor acepta la conexión.** El contador entre paréntesis lleva cuántas hay
   abiertas al mismo tiempo; con un solo cliente, una.
2. **Lee el header y sabe cuánto payload viene.** Cada mensaje empieza con su longitud,
   porque TCP entrega un flujo continuo de bytes sin marcas de dónde termina uno y empieza
   el siguiente. El servidor lee esos bytes **de a bloques**, escribiéndolos en disco a
   medida que llegan: nunca tiene la imagen entera en memoria.
3. **El hijo la revisa.** El servidor le manda por el pipe el identificador y **la ruta del
   archivo**, no los bytes: los dos procesos ven el mismo disco, copiar la imagen por un
   pipe sería trabajo al pedo. El hijo la abre con Pillow, comprueba que sea una imagen
   íntegra de un formato soportado, calcula su SHA-256, busca duplicados y registra el
   trabajo en la base. Es el único lugar del servidor donde se abre contenido de un cliente.
4. **El servidor encola la tarea** en Redis y le responde al cliente. Acá termina su
   trabajo con esta imagen: no la procesa ni espera a que se procese.
5. **Un worker la tomó.** El servidor no se enteró porque el worker le avisara —puede
   estar en otra máquina— sino porque su **monitor** consulta el estado cada medio
   segundo y lo vio pasar a `STARTED`.
6. **La cadena completó** sus tres etapas y el monitor trajo el resultado.
7. El cliente, que estaba esperando por `--wait`, pide la descarga.

**Dónde quedó cada cosa:**

```bash
find storage -type f ! -name .gitkeep
```

```
storage/uploads/cf4739e2-…/grupo.jpg    ← el original, tal como llegó
storage/results/cf4739e2-…/out.jpg        ← el resultado
```

Los archivos intermedios de la cadena —uno por etapa— vivieron en esa misma carpeta y los
borró la última etapa al terminar.

Un directorio por trabajo, nombrado con su identificador. El nombre original se conserva
adentro, pero lo que identifica al archivo es el **UUID**, que el servidor generó y que no
es adivinable ni enumerable.

---

## Paso 3 — Verificar el resultado

El `--wait` del paso 2 ya descargó el archivo. Vale la pena mirarlo, porque es donde se ve
que el sistema hizo lo que promete:

```bash
./venv/bin/python -c "
from PIL import Image
from pathlib import Path
for nombre, ruta in [('enviado', 'img_test/grupo.jpg'), ('saneado', '/tmp/saneada.jpg')]:
    with Image.open(ruta) as imagen:
        print(f'  {nombre:<9} {len(imagen.getexif())} metadatos   {imagen.size}   {Path(ruta).stat().st_size // 1024} KB')
"
```

```
  enviado   2 metadatos   (1600, 1600)   499 KB
  saneado   0 metadatos   (900, 900)      70 KB
```

Y abriendo las dos imágenes se ve la cara cubierta. Esas tres diferencias son las tres
etapas de la cadena: metadatos borrados, caras difuminadas, tamaño reducido.

**El historial también lo refleja:**

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 --action history
```

Los trabajos figuran `✓ DONE` con su hora de terminación, no `QUEUED` como antes de que
existieran los workers.

---

## Paso 4 — Apagar

En la **terminal 1**, `Ctrl-C`.

```
[ingreso] el proceso principal pidió terminar
proceso de ingreso terminado
servidor detenido
```

El apagado no es matar procesos: el servidor deja de aceptar conexiones, espera a que se
cierren las abiertas, y le **pide** al hijo que termine mandándole un mensaje por el pipe.
El hijo sale por las suyas. Solo si no obedeciera en cinco segundos se lo cortaría por la
fuerza.

El worker se apaga aparte, con `Ctrl-C` en la terminal 2, y a propósito: **no es hijo del
servidor**. Un trabajo encolado sigue en Redis aunque el servidor no esté, y lo toma el
próximo worker que arranque. Es la diferencia con un `multiprocessing.Pool`, cuyos
procesos mueren con quien los creó.

---

# Apéndice: si hay preguntas

Cada punto es independiente y se muestra en menos de un minuto.

### «¿Atiende a varios clientes a la vez?»

```bash
for numero in 1 2 3 4 5 6; do
    ./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
        --action submit --file img_test/grupo.jpg --op clean &
done
```

El `&` los lanza a todos juntos. En el registro se ve el contador subir —`(6 en total)`— y
los trabajos aceptándose **entremezclados**, no en el orden en que salieron: es asyncio
alternando entre corrutinas en cada `await`, sobre un solo hilo.

### «¿Soporta IPv6?»

El mismo comando cambiando la dirección:

```bash
./venv/bin/python -m app.client --user ana --host ::1 --port 9876 \
    --action submit --file img_test/grupo.jpg --op clean
```

En el registro la conexión figura como `::1:puerto` en vez de `127.0.0.1:puerto`. Es el
mismo servidor y el mismo puerto: los dos sockets del paso 1.

### «¿Detecta imágenes repetidas?»

Sí, comparando el contenido y no el nombre — con una salvedad honesta: **hoy no dispara
sola**, y el motivo es interesante.

La búsqueda de duplicados solo reutiliza trabajos en `DONE`, y consulta **la base de
datos**. Pero el monitor, al detectar que una tarea terminó, actualiza el índice **en
memoria** y no la base: persistir los cambios de estado es la parte que falta. Así que la
base sigue viendo todo `QUEUED` y la consulta nunca encuentra nada.

Se puede mostrar marcando el trabajo a mano —que es exactamente lo que hará esa pieza
cuando exista— y reenviando `grupo_copia.jpg`, que es copia byte a byte de
`grupo.jpg` con otro nombre:

```bash
./venv/bin/python -c "
import sqlite3
base = sqlite3.connect('data/jobs.db')
base.execute(\"UPDATE jobs SET status = 'DONE'\")
base.commit()
"
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file img_test/grupo_copia.jpg --op sanitize \
    --mode blur --quality 70 --max-size 900
```

```
╭────────────── Imagen ya procesada ──────────────╮
│   Trabajo  856bd1e4-bed1-4765-a591-b54c7a71944d │
│    Estado  ✓ DONE                               │
╰─────────────────────────────────────────────────╯
Esta imagen ya había sido procesada con esta misma operación.
```

Devolvió el identificador del trabajo **anterior**, y `find storage/uploads` muestra una
sola copia: la segunda se borró.

**Los parámetros tienen que ser los mismos que los del paso 2**, `--max-size 900`
incluido: la identidad de un trabajo son cuatro campos —usuario, contenido, operación y
parámetros—, no solo el hash. Si cambiás `--mode pixelate` o quitás el `--max-size`, el
mismo archivo vuelve a ser un trabajo nuevo, porque el resultado sería otro.

### «¿Se pierde todo si se reinicia el servidor?»

No: los trabajos van a SQLite en el momento en que se aceptan.

```bash
# apagar el servidor con Ctrl-C, volver a arrancarlo, y consultar un trabajo de antes
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action status --job-id EL-ID-DE-ANTES
```

El servidor mantiene un índice en memoria de los trabajos de esta sesión y cae a la base
cuando el trabajo es más viejo. La regla de propiedad se aplica igual: pedirlo con otro
usuario sigue dando `FORBIDDEN`.

Quien escribe la base es **únicamente el proceso de ingreso**; el principal la abre en modo
solo lectura y SQLite le rechaza cualquier escritura. Es lo que hace viable usar SQLite
acá, que admite muchos lectores y un solo escritor.

### «¿Y si mando algo que no es una imagen?»

El cliente comprueba la extensión, que es lo único que puede ver desde afuera. El que
mira el contenido es el proceso de ingreso:

`img_test/rota.jpg` es `grupo.jpg` sin sus últimos cinco bytes:

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file img_test/rota.jpg --op clean
```

```
╭─────────────── El servidor rechazó el pedido (INVALID_IMAGE) ────────────────╮
│ no se pudo abrir como imagen: image file is truncated (81 bytes not          │
│ processed)                                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

Cinco bytes de menos y el archivo deja de servir. El ingreso lo abre **dos veces**: una
para revisar la estructura y otra para decodificar los píxeles, porque un truncado así
pasa la primera y solo falla en la segunda.

En el registro del servidor se ve el rechazo, y `find storage/uploads` confirma que **el
archivo se borró**: sin un trabajo que lo use, es basura.

### «¿Un usuario puede ver los trabajos de otro?»

```bash
./venv/bin/python -m app.client --user otro --host 127.0.0.1 --port 9876 \
    --action status --job-id EL-ID-DE-ANA
```

```
╭─ El servidor rechazó el pedido (FORBIDDEN) ─╮
│ ese trabajo pertenece a otro usuario        │
╰─────────────────────────────────────────────╯
```

El servidor distingue "no existe" de "no es tuyo", y responde códigos distintos.

### «¿Qué pasa si un pedido está mal formado?»

Se responde el error y **la conexión sigue sirviendo**. El cliente valida antes de enviar,
así que hay que hablarle al servidor directamente:

```bash
./venv/bin/python - <<'FIN'
import asyncio
from app.common import protocol

async def main():
    lector, escritor = await asyncio.open_connection("127.0.0.1", 9876)

    await protocol.send_message(escritor, {"type": "borrar_todo", "user": "ana"})
    print("tipo inexistente →", (await protocol.receive_header(lector))["message"])

    await protocol.send_message(escritor, {"type": "history", "user": "ana", "limit": 3})
    respuesta = await protocol.receive_header(lector)
    print("y sigue viva     →", len(respuesta["jobs"]), "trabajos")

    escritor.close()
    await escritor.wait_closed()

asyncio.run(main())
FIN
```

**Qué aparece:**

```
tipo inexistente → tipo de pedido desconocido: 'borrar_todo'
y sigue viva     → 1 trabajos
```

El número depende de cuántos envíos lleve `ana` hasta ese momento; lo que importa es que
haya respuesta, o sea que la conexión siguió sirviendo después de dos rechazos.

Un pedido rechazado es una respuesta más, no una caída. La única excepción es un payload
demasiado grande: ahí sí se corta, porque quedarían bytes sin leer en el socket y el
mensaje siguiente se leería corrido.

### «¿Y si el proceso de ingreso se muere?»

```bash
SERVIDOR=$(pgrep -f "app.server --port 9876")
kill -9 $(pgrep -P $SERVIDOR -f spawn_main)
```

La primera línea vuelve a buscar el PID del servidor, para que este bloque funcione suelto
aunque no se haya corrido el del paso 1.

`kill -9` manda **SIGKILL**, la única señal que un proceso no puede atrapar. Simula una
muerte violenta, como la que provocaría una imagen preparada para tumbar a la biblioteca
que la abre.

```
ERROR  el proceso de ingreso murió con código -9; se relanza
INFO   proceso de ingreso lanzado (pid 59701)
```

El servidor **siguió atendiendo** —de eso se trata que el ingreso sea un proceso aparte— y
lo reemplazó solo. El código `-9` es la señal que lo mató, en negativo: así se distingue un
proceso que terminó (código 0) de uno al que terminaron.

Un envío nuevo se atiende normalmente.

---

## Lo que todavía no está

- **Los eventos del ciclo de vida no se persisten**, y arrastra dos cosas: la
  deduplicación no dispara sola —consulta la base, que sigue viendo todo `QUEUED`— y el
  historial de sesiones anteriores mostraría estados desactualizados. El monitor ya
  detecta los cambios; falta que se los cuente al proceso de ingreso para que los escriba.
- **El historial se arma solo con la memoria** de la sesión actual. Consultar un trabajo
  viejo por su identificador sí funciona —cae a la base—, pero listarlos todos no.
- **Un reinicio del servidor pierde de vista los trabajos en vuelo.** El worker termina la
  tarea igual y el resultado queda en Redis, pero nadie actualiza el índice.
- **Falta el despliegue en contenedores.** El volumen compartido está pensado para NFS,
  para que los workers puedan correr en otra máquina.
