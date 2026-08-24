# Guion de demostración

Recorrido para mostrar el sistema funcionando, paso a paso. Cada paso dice **qué
demuestra**, el comando y qué mirar en la salida.

Hacen falta **dos terminales**: una con el servidor corriendo a la vista —su registro es
la mitad de la demostración— y otra para los comandos.

Todos los comandos se ejecutan desde la raíz del repositorio.

---

## Preparación

Cuatro archivos de prueba, más una copia exacta de uno de ellos:

```bash
./venv/bin/python - <<'FIN'
from pathlib import Path
for numero in range(4):
    Path(f"/tmp/prueba_{numero}.jpg").write_bytes(
        b"\xff\xd8\xff\xe0" + bytes([numero]) * 400_000
    )
FIN
cp /tmp/prueba_0.jpg /tmp/copia.jpg
```

Cada archivo pesa 400 KB, lo bastante para que la transferencia ocurra en varios bloques.
`copia.jpg` tiene el contenido idéntico a `prueba_0.jpg` con otro nombre: sirve para el
paso 6.

---

## 1. Arrancar el servidor

**Qué demuestra:** que al arrancar lanza su proceso hijo y abre sus sockets de escucha.

En la **terminal 1**:

```bash
./venv/bin/python -m app.server --port 9876 --storage-dir /tmp/demo
```

| Parámetro | Qué hace |
|---|---|
| `--port 9876` | puerto de escucha. Por defecto es 9000; se usa otro para no chocar con nada |
| `--storage-dir /tmp/demo` | dónde guardar las imágenes recibidas, en vez del `storage/` del repositorio |
| `--host` | *no se usa acá*: al omitirlo escucha en **todas** las interfaces, que es lo que hace que abra IPv4 e IPv6 |
| `--verbose` | agrega el detalle de cada mensaje al registro |

**Qué mirar:**

```
escuchando en 0.0.0.0:9876 (AF_INET)
escuchando en :::9876 (AF_INET6)
proceso de ingreso lanzado (pid 54504)
[ingreso] proceso de ingreso en marcha
```

Dos líneas de escucha, una por familia de direcciones. Y el proceso de ingreso, que se
lanza solo, antes de que llegue ningún cliente.

---

## 2. Los procesos

**Qué demuestra:** que hay dos procesos, y que el de ingreso es hijo del principal.

En la **terminal 2**:

```bash
SERVIDOR=$(pgrep -f "app.server --port 9876")
ps -o pid,ppid,args -p $SERVIDOR $(pgrep -P $SERVIDOR) | sed 's|[^ ]*/Python ||'
```

| Parte | Qué hace |
|---|---|
| `pgrep -f "…"` | busca el PID del proceso cuya línea de comando coincide |
| `pgrep -P $SERVIDOR` | lista los PID de sus **hijos** (`-P` = *parent*) |
| `ps -o pid,ppid,args` | muestra el PID, el del padre y la línea de comando |
| `sed 's\|[^ ]*/Python \|\|'` | recorta la ruta del intérprete, que ocupa media pantalla |

**Qué mirar:**

```
  PID  PPID ARGS
54501 54498 -m app.server --port 9876 --storage-dir /tmp/demo
54503 54501 -c from multiprocessing.resource_tracker import main;main(11)
54504 54501 -c from multiprocessing.spawn import spawn_main; spawn_main(…)
```

Los dos últimos tienen `PPID 54501`: son hijos del servidor. El `resource_tracker` lo crea
`multiprocessing` por su cuenta para llevar la cuenta de los recursos compartidos; **el de
ingreso es el otro**, y su PID es el que anunció el registro en el paso 1.

---

## 3. Los sockets de escucha

**Qué demuestra:** IPv4 e IPv6 a la vez, en un solo proceso y un solo puerto.

```bash
lsof -nP -p $SERVIDOR -a -iTCP -sTCP:LISTEN
```

| Parámetro | Qué hace |
|---|---|
| `-p $SERVIDOR` | solo los archivos abiertos por ese proceso |
| `-iTCP` | solo sockets de red TCP |
| `-sTCP:LISTEN` | solo los que están escuchando, no las conexiones establecidas |
| `-a` | combina las condiciones con **y** en lugar de con **o**, que es el defecto de `lsof` |
| `-n -P` | no traduce direcciones a nombres ni puertos a servicios: se ven los números |

**Qué mirar:**

```
Python  54501 augusto  7u  IPv6  …  TCP *:9876 (LISTEN)
Python  54501 augusto  8u  IPv4  …  TCP *:9876 (LISTEN)
```

El mismo PID, el mismo puerto, **dos descriptores** (7 y 8) y dos familias. `asyncio`
abre un socket por familia cuando no se le da un host concreto.

---

## 4. Enviar una imagen

**Qué demuestra:** el camino completo de un envío, incluida la revisión del proceso hijo.

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file /tmp/prueba_0.jpg --op anonymize --mode blur
```

| Parámetro | Qué hace |
|---|---|
| `--user ana` | con qué usuario se declara el trabajo. No hay autenticación: es quien dice ser |
| `--host 127.0.0.1` | dirección del servidor, **por IPv4** |
| `--action submit` | enviar una imagen. Es el único pedido que lleva payload |
| `--file` | la imagen a enviar |
| `--op anonymize` | la operación a aplicar |
| `--mode blur` | parámetro de `anonymize`: cómo cubrir las caras |

**Qué mirar en la terminal 1:**

```
conectado 127.0.0.1:54299 (1 en total)
pedido 'submit' (400004 bytes de payload)
[ingreso] a1b2c3d4-… revisado: 49fe9a7f59d7
trabajo a1b2c3d4-… aceptado: 'anonymize' sobre 'prueba_0.jpg' (400004 bytes)
```

El orden importa: **primero revisa el hijo, después el principal acepta**. Si el hijo
rechazara la imagen, no habría línea de "aceptado" y el archivo se borraría.

Anotá el identificador del trabajo: hace falta en el paso 9.

---

## 5. Varios clientes a la vez, por las dos familias

**Qué demuestra:** que el servidor atiende N clientes concurrentes sobre un solo hilo, y
que responde por IPv4 y por IPv6 indistintamente.

```bash
for numero in 1 2 3; do
    ./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
        --action submit --file /tmp/prueba_$numero.jpg --op clean &
done
for numero in 1 2 3; do
    ./venv/bin/python -m app.client --user luis --host ::1 --port 9876 \
        --action submit --file /tmp/prueba_$numero.jpg --op inspect &
done
```

El `&` del final manda cada cliente al fondo, así los seis arrancan a la vez en lugar de
uno después del otro. Tres van por IPv4 (`127.0.0.1`) y tres por IPv6 (`::1`) — **la misma
dirección de escucha, las dos familias**.

**Qué mirar en la terminal 1:**

```
conectado 127.0.0.1:54301 (1 en total)
conectado ::1:54302 (2 en total)
conectado 127.0.0.1:54303 (3 en total)
…
trabajo … aceptado: 'inspect' sobre 'prueba_3.jpg'
trabajo … aceptado: 'clean'   sobre 'prueba_1.jpg'
trabajo … aceptado: 'inspect' sobre 'prueba_2.jpg'
```

Dos cosas. El contador de conexiones sube: hubo **seis abiertas al mismo tiempo**. Y los
trabajos se aceptan **entremezclados**, no en el orden en que se lanzaron: es asyncio
alternando entre las corrutinas cada vez que una espera en un `await`.

---

## 6. La misma imagen con otro nombre

**Qué demuestra:** que el proceso de ingreso identifica el contenido, no el nombre. Es el
cimiento de la deduplicación.

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file /tmp/copia.jpg --op clean
```

**Qué mirar en la terminal 1:** comparar el hash de esta línea con el de `prueba_0.jpg`
del paso 4.

```
[ingreso] a1b2c3d4-… revisado: 49fe9a7f59d7    ← prueba_0.jpg
[ingreso] e5f6a7b8-… revisado: 49fe9a7f59d7    ← copia.jpg, mismo hash
```

Dos archivos con nombres distintos y el mismo SHA-256. Cuando exista la base de datos, el
segundo envío no generará un trabajo nuevo: devolverá el identificador del primero.

Para ver que un contenido distinto da otro hash, cualquiera de los `prueba_N.jpg` del paso
5 sirve de contraste.

---

## 7. El historial

**Qué demuestra:** el pedido `history`, que lista los trabajos del usuario del más
reciente al más antiguo.

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action history --limit 5
```

`--limit 5` acota cuántos devolver. El servidor lo recorta a 100 aunque se pida más: un
límite disparatado no se rechaza, se ajusta.

**Qué mirar:** todos los trabajos figuran en `QUEUED`, y la columna de terminación está
vacía. Es lo esperado en esta etapa: no hay nada que los procese todavía.

---

## 8. Cada usuario ve solo lo suyo

**Qué demuestra:** la regla de propiedad, que se aplica en cada pedido.

Primero, el historial de `luis`, para copiar uno de sus identificadores:

```bash
./venv/bin/python -m app.client --user luis --host ::1 --port 9876 --action history
```

Y ahora `ana` pide un trabajo de `luis` (reemplazá el identificador):

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action status --job-id EL-ID-DE-LUIS
```

**Qué mirar:**

```
╭─ El servidor rechazó el pedido (FORBIDDEN) ─╮
│ ese trabajo pertenece a otro usuario        │
╰─────────────────────────────────────────────╯
```

El servidor distingue "no existe" de "no es tuyo", y responde códigos distintos:
`JOB_NOT_FOUND` y `FORBIDDEN`.

---

## 9. Consultar y descargar

**Qué demuestra:** los pedidos `status` y `download`, y el estado actual del sistema.

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action status --job-id EL-ID-DEL-PASO-4
```

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action download --job-id EL-ID-DEL-PASO-4 -o /tmp/resultado.jpg
```

`-o` indica dónde guardar; si se omite se usa el nombre que sugiera el servidor.

**Qué mirar:** la consulta responde `QUEUED`, y la descarga responde:

```
╭────────── El servidor rechazó el pedido (NOT_READY) ──────────╮
│ el trabajo todavía está en QUEUED: no hay nada para descargar │
╰───────────────────────────────────────────────────────────────╯
```

Es **el comportamiento correcto** para lo que hay implementado: sin workers, ningún
trabajo llega a `DONE`. El camino completo de la descarga está escrito y probado; lo que
falta es quien mueva el trabajo hasta ahí.

---

## 10. Un pedido rechazado no rompe la conexión

**Qué demuestra:** que un rechazo es una respuesta más, y el cliente puede seguir usando la
misma conexión.

Esto no se puede hacer con el cliente, que valida antes de enviar: hay que hablarle al
servidor directamente.

```bash
./venv/bin/python - <<'FIN'
import asyncio
from app.common import protocol

async def main():
    lector, escritor = await asyncio.open_connection("127.0.0.1", 9876)

    await protocol.send_message(escritor, {"type": "borrar_todo", "user": "ana"})
    print("tipo inexistente  →", (await protocol.receive_header(lector))["message"])

    await protocol.send_message(escritor, {"type": "history"})
    print("sin usuario       →", (await protocol.receive_header(lector))["message"])

    await protocol.send_message(escritor, {"type": "history", "user": "ana", "limit": 3})
    respuesta = await protocol.receive_header(lector)
    print("y sigue viva      →", len(respuesta["jobs"]), "trabajos en el historial")

    escritor.close()
    await escritor.wait_closed()

asyncio.run(main())
FIN
```

**Qué mirar:**

```
tipo inexistente  → tipo de pedido desconocido: 'borrar_todo'
sin usuario       → falta el campo 'user' o está vacío
y sigue viva      → 3 trabajos en el historial
```

Dos pedidos rechazados y el tercero funciona **por la misma conexión**. El servidor
distingue un pedido inválido de una conexión rota. La única excepción es un payload
demasiado grande: ahí sí corta, porque quedarían bytes sin leer en el socket y el mensaje
siguiente se leería corrido.

---

## 11. Si el proceso de ingreso muere

**Qué demuestra:** el aislamiento entre procesos, y que el sistema se recupera solo.

```bash
INGRESO=$(pgrep -P $SERVIDOR -f spawn_main)
kill -9 $INGRESO
```

`kill -9` manda **SIGKILL**, la única señal que un proceso no puede atrapar ni ignorar. Es
la forma de simular una muerte violenta, como la que provocaría una imagen preparada para
tumbar a la biblioteca que la abre.

**Qué mirar en la terminal 1:**

```
ERROR  el proceso de ingreso murió con código -9; se relanza
INFO   proceso de ingreso lanzado (pid 54601)
[ingreso] proceso de ingreso en marcha
```

El código `-9` es la señal que lo mató, en negativo: así se distingue un proceso que
terminó (código 0) de uno al que terminaron. **El servidor siguió atendiendo** — de eso se
trata que el ingreso sea un proceso aparte y no un hilo.

Y un envío nuevo se atiende normalmente:

```bash
./venv/bin/python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file /tmp/prueba_2.jpg --op clean
```

---

## 12. Apagado ordenado

**Qué demuestra:** que el apagado no es matar procesos, sino pedirles que terminen.

En la **terminal 1**, `Ctrl-C`. O desde la terminal 2:

```bash
kill -TERM $SERVIDOR
```

`SIGTERM` es la señal de "terminá cuando puedas", y el servidor la atiende: deja de aceptar
conexiones, espera a que se cierren las abiertas, y recién entonces le pide al hijo que
salga.

**Qué mirar:**

```
INFO   desconectado 127.0.0.1:54655
[ingreso] el proceso principal pidió terminar
INFO   proceso de ingreso terminado
INFO   servidor detenido
```

El hijo sale **por las suyas**, porque el pedido le llega por el pipe como un mensaje más.
Solo si no obedeciera en cinco segundos se lo cortaría por la fuerza.

---

## Lo que todavía no está

Conviene decirlo antes de que lo pregunten:

- **Los trabajos se quedan en `QUEUED` para siempre.** Falta la cola de tareas (Celery +
  Redis) y los workers, que son los que abren la imagen y la transforman.
- **`deduplicated` siempre responde falso.** El hash ya se calcula, pero buscar un envío
  anterior con el mismo hash necesita la base de datos.
- **El proceso de ingreso todavía no verifica** que el archivo sea una imagen válida: para
  eso hace falta Pillow. Hoy calcula el hash y acepta.
- **El historial se pierde al reiniciar**, porque vive en memoria. SQLite es lo que lo hace
  permanente.

Lo que sí funciona de punta a punta: el cliente, el proceso principal con sus sockets IPv4
e IPv6, el protocolo completo con sus cuatro pedidos, y el proceso de ingreso con su
comunicación por pipe.
