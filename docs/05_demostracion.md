# Guion de demostración

Recorrido corto para mostrar el sistema funcionando: **un solo envío de imagen**, siguiendo
qué hace cada parte. Al final hay un apéndice con lo que se puede mostrar si hay preguntas.

Hacen falta **dos terminales**: una con el servidor a la vista —su registro es la mitad de
la demostración— y otra para los comandos. Todo se ejecuta desde la raíz del repositorio.

---

## Quién es quién

Tres piezas, cada una en su proceso:

```
   TERMINAL 2                      TERMINAL 1
   ┌──────────┐   socket TCP    ┌──────────────┐      pipe      ┌──────────────┐
   │ CLIENTE  │ ──────────────► │   SERVIDOR   │ ─────────────► │  INGRESO     │
   │          │ ◄────────────── │  (principal) │ ◄───────────── │  (hijo)      │
   └──────────┘                 └──────────────┘                └──────────────┘
    otra máquina                  atiende a todos                 revisa la imagen
    en principio                  los clientes a la vez           sin bloquear a nadie
```

| Pieza | Qué hace | Qué **no** hace |
|---|---|---|
| **Cliente** | arma el pedido, manda la imagen, muestra la respuesta | nada de lógica del servicio |
| **Servidor** | atiende N clientes a la vez, valida, guarda el archivo, responde | **nunca abre una imagen** |
| **Ingreso** | abre el contenido y lo revisa: hoy calcula su SHA-256 | no habla con los clientes |

La regla que explica el reparto: **el que atiende a los clientes nunca toca contenido no
confiable.** Abrir una imagen que mandó cualquiera puede tumbar el proceso, y si eso pasara
en el servidor se caerían todas las conexiones abiertas. Por eso se hace en un proceso
aparte, que puede morirse sin arrastrar a nadie.

---

## Preparación

Una imagen de prueba:

```bash
./venv/bin/python -c "from pathlib import Path; Path('/tmp/foto.jpg').write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 300_000)"
```

Sirve cualquier `.jpg` o `.png` real; esta pesa 300 KB, lo bastante para que la
transferencia ocurra en varios bloques.

Y conviene empezar con el almacenamiento vacío, para que lo que aparezca durante la
demostración sea solo lo de hoy:

```bash
find storage/uploads -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
```

Borra todo lo que haya adentro salvo el `.gitkeep`, que es el archivo con el que git
conserva la carpeta vacía en el repositorio. Con `rm -rf storage/uploads/*` alcanzaría,
pero falla si la carpeta ya está vacía.

---

## Paso 1 — Arrancar el servidor

En la **terminal 1**:

```bash
./venv/bin/python -m app.server --port 9876
```

| Parámetro | Qué hace |
|---|---|
| `--port 9876` | puerto de escucha (por defecto 9000; se usa otro para no chocar con nada) |
| `--host` | *se omite a propósito*: sin él escucha en **todas** las interfaces, IPv4 e IPv6 |
| `--storage-dir` | *también se omite*: las imágenes van a `storage/` dentro del repositorio, que es lo cómodo para mostrarlas |

**Lo que aparece:**

```
escuchando en 0.0.0.0:9876 (AF_INET)
escuchando en :::9876 (AF_INET6)
proceso de ingreso lanzado (pid 59616)
[ingreso] proceso de ingreso en marcha
```

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

En la **terminal 2**:

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file /tmp/foto.jpg --op anonymize --mode blur
```

| Parámetro | Qué hace |
|---|---|
| `--user ana` | con qué usuario se declara el trabajo (no hay autenticación) |
| `--host 127.0.0.1 --port 9876` | a qué servidor conectarse |
| `--action submit` | enviar una imagen: el único pedido que lleva archivo |
| `--op anonymize` | qué hacerle |
| `--mode blur` | parámetro de `anonymize`: cómo cubrir las caras |

**Lo que ve el cliente:**

```
╭──────────────── Imagen aceptada ────────────────╮
│   Trabajo  7cbb592e-45c0-4841-a6bc-029c535550b4 │
│    Estado  ◷ QUEUED                             │
│ Operación  anonymize                            │
│    Imagen  foto.jpg (293.0 KB)                  │
╰─────────────────────────────────────────────────╯
```

**Y en la terminal 1, el recorrido completo:**

```
conectado 127.0.0.1:54968 (1 en total)                     ← 1. el servidor acepta
pedido 'submit' (300004 bytes de payload)                  ← 2. lee el header
[ingreso] 7cbb592e-… revisado: 49fe9a7f59d7                ← 3. el HIJO revisa
trabajo 7cbb592e-… aceptado: 'anonymize' sobre 'foto.jpg'  ← 4. el servidor acepta
desconectado 127.0.0.1:54968                               ← 5. el cliente cierra
```

Línea por línea:

1. **El servidor acepta la conexión.** El contador entre paréntesis lleva cuántas hay
   abiertas al mismo tiempo; con un solo cliente, una.
2. **Lee el header y sabe cuánto payload viene.** Cada mensaje empieza con su longitud,
   porque TCP entrega un flujo continuo de bytes sin marcas de dónde termina uno y empieza
   el siguiente. El servidor lee esos 300.004 bytes **de a bloques**, escribiéndolos en
   disco a medida que llegan: nunca tiene la imagen entera en memoria.
3. **El hijo la revisa.** El servidor le manda por el pipe el identificador y **la ruta del
   archivo**, no los bytes: los dos procesos ven el mismo disco, copiar la imagen por un
   pipe sería trabajo al pedo. El hijo la abre, calcula su SHA-256 y devuelve el veredicto.
4. **Recién ahora el servidor acepta el trabajo** y le responde al cliente. El orden
   importa: si el hijo la hubiera rechazado, no habría línea de "aceptado", el archivo se
   borraría y el cliente recibiría un error.
5. El cliente cierra la conexión y termina.

**Dónde quedó la imagen:**

```bash
find storage/uploads -type f ! -name .gitkeep
```

```
storage/uploads/7cbb592e-45c0-4841-a6bc-029c535550b4/foto.jpg
```

Un directorio por trabajo, nombrado con su identificador. El nombre original se conserva
adentro, pero lo que identifica al archivo es el **UUID**, que el servidor generó y que no
es adivinable ni enumerable.

---

## Paso 3 — Consultar el estado

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action status --job-id 7cbb592e-45c0-4841-a6bc-029c535550b4
```

Reemplazá el identificador por el que devolvió el paso 2.

```
╭───────────────────────────────────────────────╮
│ Trabajo  7cbb592e-45c0-4841-a6bc-029c535550b4 │
│  Estado  ◷ QUEUED                             │
╰───────────────────────────────────────────────╯
```

Sigue en `QUEUED`, y va a seguir así: **no hay nada que procese los trabajos todavía**. Esa
es la parte que falta —los workers— y conviene decirlo antes de que lo pregunten.

Si se pide la descarga, el servidor lo explica en lugar de fallar:

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action download --job-id 7cbb592e-45c0-4841-a6bc-029c535550b4
```

```
╭────────── El servidor rechazó el pedido (NOT_READY) ──────────╮
│ el trabajo todavía está en QUEUED: no hay nada para descargar │
╰───────────────────────────────────────────────────────────────╯
```

El camino completo de la descarga está escrito y probado; lo que falta es quien lleve el
trabajo hasta `DONE`.

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

---

# Apéndice: si hay preguntas

Cada punto es independiente y se muestra en menos de un minuto.

### «¿Atiende a varios clientes a la vez?»

```bash
for numero in 1 2 3 4 5 6; do
    ./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
        --action submit --file /tmp/foto.jpg --op clean &
done
```

El `&` los lanza a todos juntos. En el registro se ve el contador subir —`(6 en total)`— y
los trabajos aceptándose **entremezclados**, no en el orden en que salieron: es asyncio
alternando entre corrutinas en cada `await`, sobre un solo hilo.

### «¿Soporta IPv6?»

El mismo comando cambiando la dirección:

```bash
./venv/bin/python -m app.client --user ana --host ::1 --port 9876 \
    --action submit --file /tmp/foto.jpg --op clean
```

En el registro la conexión figura como `::1:puerto` en vez de `127.0.0.1:puerto`. Es el
mismo servidor y el mismo puerto: los dos sockets del paso 1.

### «¿Detecta imágenes repetidas?»

Todavía no, pero el hash ya se calcula. Una copia del mismo archivo con otro nombre:

```bash
cp /tmp/foto.jpg /tmp/copia.jpg
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file /tmp/copia.jpg --op clean
```

En el registro, la línea `revisado:` termina con el **mismo hash** que la de `foto.jpg`.
Cuando exista la base de datos, ese es el dato con el que se buscará el envío anterior.

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

- **Los trabajos se quedan en `QUEUED`.** Falta la cola de tareas (Celery + Redis) y los
  workers, que son los que abren la imagen y la transforman.
- **No se detectan duplicados.** El hash ya se calcula; falta la base de datos donde
  buscarlo.
- **El ingreso todavía no verifica** que el archivo sea una imagen válida: para eso hace
  falta Pillow.
- **El historial se pierde al reiniciar**, porque vive en memoria. SQLite es lo que lo hace
  permanente.
