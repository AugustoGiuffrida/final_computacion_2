# Instalación y puesta en marcha

## Requisitos previos

| | Versión | Para qué |
|---|---|---|
| **Python** | 3.14 o superior | el proyecto usa sintaxis y biblioteca estándar recientes |
| **Docker** | cualquiera reciente | únicamente para levantar Redis |
| **git** | — | clonar el repositorio |

No hace falta instalar Redis en el sistema: corre en un contenedor.

## 1. Clonar e instalar

```bash
git clone git@github.com:AugustoGiuffrida/final_computacion_2.git
cd final_computacion_2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

El entorno virtual aísla las dependencias del proyecto de las del sistema.

> **Todos los comandos de la documentación asumen el entorno activado.** Si abrís una
> terminal nueva —y el sistema usa tres a la vez— hay que activarlo en cada una:
>
> ```bash
> cd final_computacion_2
> source venv/bin/activate
> ```
>
> Se nota porque el prompt pasa a mostrar `(venv)` adelante. Para salir, `deactivate`.

Con el entorno activado, `python` y `celery` son los del proyecto:

```bash
which python     # .../final_computacion_2/venv/bin/python
```

Las cuatro dependencias:

| Paquete | Para qué |
|---|---|
| `Pillow` | abrir, verificar y transformar imágenes |
| `opencv-python-headless` | detección de caras. `headless` evita las dependencias gráficas, que un servidor no necesita |
| `celery[redis]` | la cola de tareas distribuida; `[redis]` arrastra el cliente del broker |
| `rich` | la presentación del cliente: tablas, colores, barras de progreso |

## 2. Levantar Redis

Es el intermediario entre el servidor y los workers.

```bash
docker run -d --name redis-final -p 6380:6379 redis:7-alpine
docker exec redis-final redis-cli ping     # tiene que responder PONG
```

| Parte | Qué hace |
|---|---|
| `run -d` | crea el contenedor y lo deja corriendo de fondo |
| `--name redis-final` | un nombre fijo, para poder frenarlo y arrancarlo después |
| `-p 6380:6379` | el puerto 6379 de adentro sale como 6380 afuera |

**Las veces siguientes** el contenedor ya existe, así que alcanza con arrancarlo:

```bash
docker start redis-final
```

Si `docker start` responde `No such container`, el contenedor se borró: volvé a correr el
`docker run` de arriba. Se nota porque el servidor rechaza los envíos con
`INTERNAL: no se pudo encolar el trabajo` — está diciendo que no encuentra a Redis.

Si necesitás usar otro puerto, cambialo en `CELERY_BROKER_URL` y `CELERY_RESULT_BACKEND`,
en `app/common/config.py`.

## 3. Verificar que todo funciona

```bash
python -m unittest discover -s tests -t .
```

Tienen que pasar las 172 pruebas. No necesitan Redis ni Docker: las que involucran
workers usan dobles de prueba.

## 4. Arrancar el sistema

Hacen falta **tres terminales**, todas desde la raíz del repositorio y **con el entorno
activado** (`source venv/bin/activate` en cada una).

**Terminal 1 — el servidor:**

```bash
python -m app.server --port 9876
```

Abre dos sockets de escucha (IPv4 e IPv6) y lanza su proceso hijo de ingreso.

**Terminal 2 — el worker:**

```bash
celery -A app.worker.celery_app worker --loglevel=info
```

Al arrancar lista las ocho tareas que sabe ejecutar. `celery` es un programa del paquete,
no un script del proyecto: `-A` le dice dónde está la instancia con la configuración.

**Terminal 3 — el cliente:**

```bash
python -m app.client --user ana --host 127.0.0.1 --port 9876 \
    --action submit --file img_test/grupo.jpg --op sanitize \
    --mode blur --quality 70 --max-size 900 --wait -o /tmp/saneada.jpg
```

Si eso devuelve una imagen saneada, la instalación está completa. El recorrido detallado,
paso a paso y con lo que hay que mirar en cada uno, está en
[`docs/05_demostracion.md`](docs/05_demostracion.md).

## Dónde quedan los archivos

| Ruta | Qué guarda | Compartido |
|---|---|---|
| `storage/uploads/<job_id>/` | las imágenes tal como llegaron | **sí** — los workers tienen que verlas |
| `storage/results/<job_id>/` | las imágenes procesadas | **sí** |
| `data/jobs.db` | la base de datos | **no** — disco local |

Ambas rutas se pueden cambiar: `--storage-dir` y `--database`.

La separación es deliberada y está pensada para el despliegue: `storage/` va a ser un
sistema de archivos de red, para que los workers puedan correr en otra máquina. La base
**no** puede estar ahí, porque SQLite desaconseja los sistemas de archivos de red —el
bloqueo no es confiable— y además no lo necesita: solo la tocan el servidor y su proceso
hijo, que viven siempre en la misma máquina.

## Apagar

`Ctrl-C` en la terminal 1 y en la 2. El servidor deja de aceptar conexiones, espera a que
se cierren las abiertas y le pide a su proceso hijo que termine.

El worker se apaga aparte porque **no es hijo del servidor**: un trabajo encolado
sobrevive en Redis aunque el servidor no esté, y lo toma el próximo worker que arranque.

```bash
docker stop redis-final    # si además querés bajar Redis
```
