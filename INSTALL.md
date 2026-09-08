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
docker run -d --name redis-final -p 6380:6379 redis:7.4-alpine
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

Tienen que pasar las 225 pruebas. No necesitan Redis ni Docker: las que involucran
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

## Probar el cliente visual

Con el servidor y el worker levantados —terminales 1 y 2—, en una tercera:

```bash
python -m app.tui --user ana --port 9876
```

Ocupa la terminal entera hasta que salgas con `q`. El campo de arriba a la izquierda cambia
la carpeta que se explora —se escribe la ruta y se confirma con Enter; `~` vale—, así que no
hace falta reiniciar para ir a otro lado. `--dir` solo elige con cuál arranca:

```bash
python -m app.tui --user ana --port 9876 --dir img_test --output /tmp
```

Si el servidor no está levantado, arranca igual y avisa que no pudo conectarse, en vez de
cerrarse: así se ve el error en pantalla y no en un volcado de pila.

Lo más vistoso es abrirlo **y usar el cliente de terminal al mismo tiempo** desde otra
ventana: los trabajos que mandes por línea de comandos aparecen solos en la tabla, y se los
ve pasar de `QUEUED` a `PROCESSING` a `DONE`. Son dos clientes distintos hablando el mismo
protocolo contra el mismo servidor.

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

---

## Alternativa: todo en contenedores

En lugar de instalar Python y las dependencias a mano, se puede levantar el sistema
completo con Docker. Redis, el servidor y los workers quedan aislados; **el cliente sigue
corriendo fuera**, porque es de quien usa el servicio, no parte del despliegue.

Se arranca en **dos pasos**, y el orden importa —el porqué está en [El sistema de archivos
compartido](#el-sistema-de-archivos-compartido):

```bash
docker compose up -d nfs && sleep 5 && docker compose up -d
```

La primera vez construye la imagen —unos minutos— y las siguientes arranca en segundos.

| Servicio | Qué es |
|---|---|
| `nfs` | sirve el volumen compartido; **tiene que arrancar primero** |
| `redis` | el broker y el result backend |
| `servidor` | atiende a los clientes en el puerto 9000, con su proceso de ingreso |
| `worker` | procesa las imágenes |

Ver qué está corriendo y seguir lo que hacen:

```bash
docker compose ps
docker compose logs -f servidor
docker compose logs -f worker      # el registro de todos los workers, mezclado
```

El cliente se usa igual que siempre, contra el puerto publicado:

```bash
source venv/bin/activate
python -m app.client --user ana --host 127.0.0.1 --port 9000 \
    --action submit --file img_test/grupo.jpg --op sanitize --wait -o /tmp/saneada.jpg
```

### Más o menos workers, en caliente

```bash
docker compose up -d --scale worker=3
```

Crea o elimina contenedores hasta llegar al número pedido. **El servidor no se reinicia**:
los workers van a buscar trabajo a Redis, así que el servidor no sabe cuántos hay ni le
importa.

Un worker que se baja a mitad de una imagen no pierde el trabajo: con `task_acks_late`, el
mensaje se confirma recién al terminar, así que el broker se lo vuelve a dar a otro.

### Bajar todo

```bash
docker compose down        # frena y elimina los contenedores
docker compose down -v     # además borra las imágenes guardadas y la base
```

### Dónde quedan los archivos en los contenedores

Dentro de los contenedores, en dos volúmenes de naturaleza distinta:

| Volumen | Montado en | Qué es | Quién lo usa |
|---|---|---|---|
| `imagenes` | `/mnt/imagenes` | **NFS**, servido por el contenedor `nfs` | el servidor **y** los workers |
| `base` | `/var/lib/final` | volumen local de Docker | solo el servidor |

Los dos son distintos a propósito. El compartido tiene que ser de red para que los workers
puedan estar en otra máquina; la base **no puede** serlo, porque SQLite desaconseja los
sistemas de archivos de red.

Que los workers **no** monten la base también es deliberado: no la necesitan y no deben
tocarla. La escribe únicamente el proceso de ingreso.

Para inspeccionarlos:

```bash
docker compose exec servidor ls -R /mnt/imagenes
docker compose exec servidor ls -l /var/lib/final
```

### El sistema de archivos compartido

El volumen de imágenes **es un sistema de archivos de red (NFS)**, no un volumen local de
Docker. Es un requisito del diseño, no una optimización: por la cola viajan **rutas, no
imágenes**, así que el servidor y los workers tienen que ver el mismo sistema de archivos.
Mientras corran en la misma máquina un volumen local alcanzaría; en cuanto un worker se
mude a otra, deja de alcanzar.

El compose trae **su propio servidor NFS**, en el servicio `nfs`. No hay nada que instalar
ni exportar: los archivos viven en el volumen `datos` de ese contenedor, y los demás lo
montan por red.

#### Arrancar: son dos pasos

```bash
docker compose up -d nfs
```

```bash
docker compose up -d
```

**El orden importa y no se puede evitar con `depends_on`.** Un volumen se monta cuando el
contenedor se **crea**, y Compose crea todos los contenedores antes de arrancar ninguno: si
se levanta todo junto, el servidor y el worker intentan montar un NFS que todavía no
escucha, y fallan con `connection refused`. Por eso el servidor NFS se levanta solo primero.

Para no acordarse cada vez:

```bash
docker compose up -d nfs && sleep 5 && docker compose up -d
```

#### Por qué el servidor NFS es un contenedor

La alternativa era usar el servidor NFS del sistema anfitrión —macOS trae `nfsd`— y montar
contra él. **Se probó y no resultó viable**: el montaje entre la máquina virtual de Docker y
el `nfsd` de macOS se rompía cada pocos minutos, con `Input/output error`, sin que el
servidor NFS ni la red tuvieran problema. Tres caídas en veinte minutos.

Con el servidor adentro, el tráfico NFS no sale de la red de contenedores y desaparece ese
cruce. Sigue siendo NFS de verdad: el montaje figura como `type nfs4` y los archivos viajan
por el protocolo.

El costo es real y conviene decirlo: el contenedor corre con **`privileged: true`**, porque
exportar por NFS necesita capacidades del núcleo que un contenedor normal no tiene.

#### Dónde quedan las imágenes

En el volumen `datos`, el del contenedor `nfs`. Para mirarlas:

```bash
docker compose exec nfs find /data -type f
```

Los otros dos volúmenes tienen papeles distintos:

| Volumen | Qué es | Quién lo usa |
|---|---|---|
| `datos` | donde el servidor NFS guarda de verdad | solo el contenedor `nfs` |
| `imagenes` | el **montaje** que hace visible lo anterior | servidor y workers |
| `base` | volumen normal de Docker, con SQLite | solo el servidor |

`imagenes` no guarda nada: es la cañería, no el depósito.

#### Usar un servidor NFS de la red

Para correr workers en **otra máquina** hace falta un NFS que las dos vean, y ahí el
contenedor no sirve. Se apunta el volumen a un servidor real con dos variables en `.env`:

```bash
NFS_SERVIDOR=192.168.1.100
NFS_EXPORT=/exports/imagenes
```

Sin esas variables se usan `127.0.0.1` y `/`, que son el contenedor `nfs`.

**Exportar el directorio en Linux:**

```bash
sudo mkdir -p /srv/imagenes && sudo chown $(id -u):$(id -g) /srv/imagenes
echo "/srv/imagenes 192.168.1.0/24(rw,sync,no_subtree_check,all_squash,anonuid=$(id -u),anongid=$(id -g))" | sudo tee /etc/exports
sudo exportfs -ra && sudo systemctl enable --now nfs-server
```

**Exportar el directorio en macOS:**

```bash
mkdir -p /Users/Shared/imagenes_compartidas
```

```bash
echo "/Users/Shared/imagenes_compartidas -mapall=$(id -u):$(id -g) -network 192.168.1.0 -mask 255.255.255.0" | sudo tee /etc/exports
```

```bash
echo "nfs.server.mount.require_resv_port = 0" | sudo tee -a /etc/nfs.conf
```

```bash
sudo nfsd enable && sudo nfsd restart
```

Cuatro cosas que costaron y conviene no repetir:

- **`addr` tiene que ser una IP.** El montaje lo hace el demonio de Docker, que no está en
  la red de compose y no resuelve nombres de servicio. Averiguala con
  `ipconfig getifaddr en0` en macOS, `hostname -I | awk '{print $1}'` en Linux. **No sirve
  `127.0.0.1`** contra un anfitrión: el demonio corre en una máquina virtual con su propio
  loopback.
- **La carpeta no puede estar en `~/Documents`, `~/Desktop` ni `~/Downloads`** en macOS: el
  sistema las protege y `nfsd` no puede leerlas. El export se ignora en silencio y
  `showmount -e localhost` devuelve una lista vacía.
- **`require_resv_port = 0`** en macOS: por defecto solo acepta montajes desde puertos
  menores a 1024, y el de Docker llega con uno alto.
- **`nfsd` no relee `/etc/exports` solo**: hay que reiniciarlo con `sudo nfsd restart`.

#### Cambiar las opciones del montaje

Docker guarda las opciones del volumen **cuando lo crea**, así que editar
`docker-compose.yml` no alcanza: un `down` normal no borra el volumen y el montaje sigue
con las opciones viejas. Hay que recrearlo:

```bash
docker compose down && docker volume rm final_comp2_imagenes && docker compose up -d nfs && docker compose up -d
```

Para confirmar qué opciones quedaron activas:

```bash
docker compose exec servidor mount | grep imagenes
```

El montaje usa **`soft,timeo=50,retrans=3`** en lugar del `hard` por defecto. Con `hard`, un
servidor NFS que deja de responder no produce errores: las escrituras **esperan para
siempre**. El servidor se congela atendiendo un envío, sin error ni registro, y hasta el
demonio de Docker se traba al intentar desmontar. Con `soft` la operación falla pasado el
tiempo de espera y el sistema sigue funcionando.

#### Presentar el proyecto en otra computadora

El `docker-compose.yml` es **idéntico en todas las máquinas**. Mudarse son tres pasos, y
ninguno toca el código:

1. Exportar un directorio en la máquina que vaya a servir el NFS, con los comandos de
   arriba según su sistema.
2. `cp .env.example .env` y poner ahí la IP y el directorio nuevos.
3. `docker compose up -d`.

Los workers pueden correr en una máquina y el NFS en otra: es justamente para eso. Lo único
que **no** puede mudarse es la base de datos, que va en disco local del servidor.

#### Si no tenés permisos de administrador

Montar un servidor NFS necesita `sudo`. Si la máquina donde vas a presentar no te lo
permite, la salida es dejar el NFS en una máquina tuya de la misma red y apuntar `.env`
ahí: los contenedores corren donde sea y montan por la red, que es el escenario real del
sistema.


## Apagar

`Ctrl-C` en la terminal 1 y en la 2. El servidor deja de aceptar conexiones, espera a que
se cierren las abiertas y le pide a su proceso hijo que termine.

El worker se apaga aparte porque **no es hijo del servidor**: un trabajo encolado
sobrevive en Redis aunque el servidor no esté, y lo toma el próximo worker que arranque.

```bash
docker stop redis-final    # si además querés bajar Redis
```
