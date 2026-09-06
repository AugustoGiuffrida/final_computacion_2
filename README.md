# Servicio de anonimización y sanitización de imágenes

Trabajo final de **Computación II** — Universidad de Mendoza.

Un sistema cliente-servidor que recibe imágenes por la red y las prepara para publicar:
les cubre las caras, les borra los metadatos y las comprime. El cliente envía por un
socket TCP, el servidor coordina, y un conjunto de workers hace el trabajo pesado en
paralelo.

**El problema que resuelve:** una foto cualquiera sacada con el celular lleva escondidas
las coordenadas de dónde se tomó, la marca del teléfono y la fecha exacta. Publicarla tal
cual es revelar bastante más de lo que uno cree, y si además hay gente en ella, sus caras.

```
   CLIENTE  ──socket TCP──►  SERVIDOR  ──pipe──►  INGRESO
   (CLI)                     (asyncio)            (verifica y registra)
                                 │
                                 ├──Redis──►  WORKERS  (procesan las imágenes)
                                 └──────────►  SQLite  (el historial)
```

## Instalación

En [`INSTALL.md`](INSTALL.md). En resumen: `venv`, `pip install -r requirements.txt` y un
contenedor de Redis.

## Uso

Todos los pedidos llevan `--user`, que identifica de quién es cada trabajo. No hay
autenticación: es quien dice ser.

### Enviar una imagen

```bash
python -m app.client --user ana --action submit --file img_test/grupo.jpg --op sanitize --wait
```

| Parámetro | Qué hace |
|---|---|
| `--action submit` | enviar una imagen. Es el único pedido que lleva archivo |
| `--file` | la imagen (JPEG o PNG) |
| `--op` | qué hacerle: ver la tabla de operaciones |
| `--wait` | esperar a que termine y descargar el resultado. Sin esto vuelve enseguida con el identificador |
| `-o RUTA` | dónde guardar lo que vuelva |
| `--host`, `--port` | dónde está el servidor (por defecto `localhost:9000`) |

### Las seis operaciones

| Operación | Qué hace | Parámetros |
|---|---|---|
| `inspect` | **audita sin modificar**: qué revela la imagen | — |
| `clean` | borra los metadatos, sin tocar un píxel | — |
| `anonymize` | cubre las caras que detecte | `--mode`, `--strength` |
| `compress` | recomprime y, si se pide, achica | `--quality`, `--max-size` |
| `convert` | cambia el formato del archivo | `--format`, `--quality` |
| `sanitize` | **las tres primeras juntas**: limpia, cubre y comprime | todos los de arriba |

| Parámetro | Valores |
|---|---|
| `--mode` | `blur` (difumina), `pixelate` (cuadricula), `box` (tapa con negro) |
| `--strength` | 1 a 100. Es relativa al tamaño de cada cara, no en píxeles |
| `--quality` | 1 a 95. Calidad de la recompresión JPEG o WebP |
| `--max-size` | lado máximo en píxeles; achica respetando la proporción |
| `--format` | `webp`, `jpeg`, `png` |

### Consultar y descargar

```bash
python -m app.client --user ana --action status   --job-id <ID>
python -m app.client --user ana --action download --job-id <ID> -o resultado.jpg
python -m app.client --user ana --action history  --limit 20
```

Un trabajo pasa por `QUEUED` → `PROCESSING` → `DONE` (o `ERROR`). La descarga solo
funciona en `DONE`; antes responde `NOT_READY`, e `inspect` responde `NO_OUTPUT` porque no
genera archivo.

Cada usuario ve **solo sus trabajos**: pedir el de otro responde `FORBIDDEN`.

### Ejemplos

```bash
# ¿Qué revela esta foto?
python -m app.client --user ana --action submit --file img_test/grupo.jpg --op inspect --wait

# Sacarle los metadatos, sin tocar la imagen
python -m app.client --user ana --action submit --file img_test/grupo.jpg --op clean --wait -o limpia.jpg

# Dejarla lista para publicar: caras cubiertas, sin metadatos y liviana
python -m app.client --user ana --action submit --file img_test/grupo.jpg --op sanitize \
    --mode blur --quality 70 --max-size 1200 --wait -o publicable.jpg

# Convertir a WebP
python -m app.client --user ana --action submit --file img_test/grupo.jpg --op convert \
    --format webp --wait -o foto.webp
```

Ambos programas tienen `--help` con la lista completa y más ejemplos.

## Cliente visual

Además del cliente de línea de comandos hay uno **visual**, en la terminal, que hace lo
mismo: elegir una imagen, aplicarle una operación y ver los trabajos avanzar.

```bash
python -m app.tui --user ana
```

```
┌─ Imágenes ────────┬─ Operación ───────────┬─ Trabajos ──────────────┐
│ [img_test______]  │  ( ) anonymize        │ ✓ DONE   sanitize       │
│ 📁 img_test/      │  ( ) clean            │ ◐ PROC   anonymize      │
│   grupo.jpg       │  (•) sanitize         │ ◷ QUEUED clean          │
│   paisaje.jpg     │                       │                         │
│                   │                       ├─ Resultado ─────────────┤
│                   │  Cómo cubrir  [blur▾] │ Caras detectadas: 12    │
│                   │  Calidad      [70   ] │ Tamaño final: 169.7 KB  │
│                   │  [   Enviar   ]       │                         │
└───────────────────┴───────────────────────┴─────────────────────────┘
```

| Tecla | Qué hace |
|---|---|
| flechas | moverse dentro del panel |
| `Enter` | en el árbol, elegir la imagen; en la lista, la operación |
| `Enter` en el campo de arriba | cambiar de carpeta: se escribe la ruta, y `~` vale |
| `Tab` | pasar al panel siguiente |
| `Enter` sobre una fila de trabajos | descargar su resultado |
| `d` | lo mismo, salvo mientras se escribe en un campo |
| `r` | refrescar ahora, sin esperar |
| `q` | salir |

La tabla se actualiza sola cada segundo: un trabajo enviado desde **otra** terminal aparece
acá igual, y se lo ve pasar de `QUEUED` a `PROCESSING` a `DONE`.

| Argumento | Para qué |
|---|---|
| `--user` | con qué nombre presentarse (obligatorio) |
| `--host` `--port` | dónde está el servidor |
| `--dir` | con qué carpeta arranca el árbol; después se cambia desde la pantalla |
| `--out` | dónde guardar lo que se descargue |

Los parámetros de cada operación **no** van por la línea de comandos: se eligen en la
pantalla, y cambian según la operación. Un campo en blanco significa "el valor por defecto
del servidor", igual que omitir la bandera en el otro cliente.

## Ejecutar el servidor

```bash
python -m app.server                         # puerto 9000, todas las interfaces
python -m app.server --port 9876             # otro puerto
python -m app.server --host 127.0.0.1        # solo conexiones locales
python -m app.server --storage-dir /mnt/img  # el volumen compartido, que ven los workers
python -m app.server --database /var/db.sqlite
```

Omitir `--host` hace que escuche en **todas** las interfaces, IPv4 e IPv6 a la vez.

## Ejecutar un worker

```bash
celery -A app.worker.celery_app worker --loglevel=info
```

Se pueden levantar varios, incluso en otras máquinas, mientras compartan el Redis y el
volumen de imágenes.

## Documentación

| Documento | Contenido |
|---|---|
| [`INSTALL.md`](INSTALL.md) | instalación y puesta en marcha |
| [`INFO.md`](INFO.md) | **las decisiones de diseño y su justificación** |
| [`TODO.md`](TODO.md) | lo que queda para versiones futuras |
| [`docs/01_aplicacion.md`](docs/01_aplicacion.md) | el problema, las operaciones y el alcance |
| [`docs/02_arquitectura.md`](docs/02_arquitectura.md) | componentes, canales, modelo de datos y flujos |
| [`docs/03_tecnologias.md`](docs/03_tecnologias.md) | qué es cada tecnología y por qué se eligió |
| [`docs/04_protocolo.md`](docs/04_protocolo.md) | la especificación del protocolo cliente-servidor |
| [`docs/05_demostracion.md`](docs/05_demostracion.md) | **el guion para mostrar el sistema funcionando** |
| [`docs/06_chuleta.md`](docs/06_chuleta.md) | las ideas del proyecto y sus llamadas, para la defensa |

## Pruebas

```bash
python -m unittest discover -s tests -t .
```

216 pruebas. Levantan servidores reales en puertos libres, lanzan procesos hijos de verdad
y usan bases SQLite temporales. No necesitan Redis: la cola se sustituye por un doble.
