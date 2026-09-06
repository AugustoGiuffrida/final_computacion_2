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


```bash
docker compose up -d
```

La primera vez construye la imagen —unos minutos— y las siguientes arranca en segundos.

| Servicio | Qué es |
|---|---|
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
| `imagenes` | `/mnt/imagenes` | **NFS**, servido por otra máquina o por la misma | el servidor **y** los workers |
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

Hace falta entonces **una máquina que sirva el NFS**. Puede ser la misma que corre los
contenedores o cualquier otra de la red.

#### Exportar el directorio en macOS

"Exportar" es declarar que una carpeta queda disponible para que otras máquinas la monten
por la red. La carpeta no se mueve ni se copia: sigue donde está, y quien la monta la ve
como si fuera suya.

```bash
mkdir -p /Users/Shared/imagenes_compartidas
```

```bash
echo "/Users/Shared/imagenes_compartidas -mapall=$(id -u):$(id -g) -network 192.168.0.0 -mask 255.255.255.0" | sudo tee /etc/exports
```

```bash
echo "nfs.server.mount.require_resv_port = 0" | sudo tee -a /etc/nfs.conf
```

```bash
sudo nfsd enable && sudo nfsd restart
```

| Parte | Qué hace |
|---|---|
| `-mapall=UID:GID` | todo lo que llegue por NFS se escribe como tu usuario. Sin esto macOS mapea el `root` del contenedor a `nobody` y las escrituras fallan |
| `-network` y `-mask` | solo la red local puede montarlo. Ajustalo a tu rango |
| `require_resv_port = 0` | macOS solo acepta montajes desde puertos <1024; el de Docker Desktop llega con uno alto porque pasa por traducción de red |

Comprobar que quedó publicado:

```bash
showmount -e localhost
```

Tiene que listar la carpeta. **Si sale vacío, el export no se cargó** y de nada sirve seguir.

**La carpeta no puede estar en `~/Documents`, `~/Desktop` ni `~/Downloads`.** macOS las
protege —son `drwx------` y además están bajo el control de privacidad del sistema—, así
que `nfsd` no puede leerlas: el export se ignora en silencio y `showmount` devuelve una
lista vacía. Por eso `/Users/Shared`, que es `drwxrwxrwt`.

**Y hay que reiniciar `nfsd` cada vez que se toca `/etc/exports`.** Si ya estaba corriendo,
no relee el archivo solo.

#### Exportar el directorio en Linux

```bash
sudo mkdir -p /srv/imagenes && sudo chown $(id -u):$(id -g) /srv/imagenes
echo "/srv/imagenes 192.168.0.0/24(rw,sync,no_subtree_check,all_squash,anonuid=$(id -u),anongid=$(id -g))" | sudo tee /etc/exports
sudo exportfs -ra && sudo systemctl enable --now nfs-server
```

#### Apuntar el proyecto al servidor

Las dos únicas cosas que cambian de una máquina a otra viven en `.env`, que **no está
versionado**:

```bash
cp .env.example .env
```

y adentro:

```bash
NFS_SERVIDOR=192.168.0.100
NFS_EXPORT=/Users/augusto/Documents/imagenes_compartidas
```

**Cómo averiguar la IP.** Es la de la máquina que sirve el NFS, en la red local:

```bash
ipconfig getifaddr en0
```

En Linux, `hostname -I | awk '{print $1}'`.

**No sirve `127.0.0.1`**, aunque el NFS lo sirva la misma máquina: el montaje lo hace el
demonio de Docker, que en macOS corre dentro de una máquina virtual con su propio loopback.
Si la IP la asigna el router por DHCP, puede cambiar al conectarse a otra red — y entonces
hay que actualizar `.env`.

Sin esas dos variables, `docker compose up` no arranca y dice cuál falta. Es deliberado:
montar en un lugar equivocado sería peor que no arrancar.

#### Sobre la versión de NFS

El compose no fija `nfsvers`, así que se usa **NFSv3**, que es lo que sirven tanto macOS
como Linux. Está medido: contra el `nfsd` de macOS, forzar `nfsvers=4` hace que el montaje
**se cuelgue sin dar error** —su servidor v4 necesita configurarle una raíz aparte—,
mientras que v3 funciona directo.

`nfsvers=4` solo hace falta contra un servidor que no ofrezca v3, que es el caso de los
servidores NFS empaquetados en contenedor: no traen el portmapper del puerto 111 y el
montaje falla con `connection refused`.

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
