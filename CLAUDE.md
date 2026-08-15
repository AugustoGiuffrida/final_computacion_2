# Contexto del proyecto

Trabajo final de **Computación II** (Universidad de Mendoza). El enunciado original está
en <https://gitlab.com/d1cor/UM_C2/-/blob/master/FINAL.md> y una copia en
`../compu2_um_2025/FINAL_REQUERIMIENTOS.md`, junto con el material del cursado.

El final consiste en desarrollar una aplicación que integre los contenidos de la materia,
**acordando previamente el tema y la arquitectura con el profesor**, y defenderla
oralmente. El desarrollo debe hacerse de forma incremental, con commits frecuentes.

---

## 1. Estado actual — LEER ANTES DE ESCRIBIR CÓDIGO

La documentación de diseño está **completa**. El código **todavía no empezó**.

El profesor aprueba el proyecto **por partes**. Hasta ahora aprobó:

| Aprobado | Detalle |
|---|---|
| ✅ El tema y el planteo general | anonimización y sanitización de imágenes |
| ✅ El **cliente** | interfaz por línea de comandos |
| ✅ El **proceso principal del servidor** | el que atiende a los clientes |
| ✅ Que se comuniquen por **sockets** | la comunicación cliente-servidor |

**No aprobado todavía** (diseñado pero pendiente): el protocolo de aplicación (catálogo
de mensajes, códigos de error, modelo de identidad), el proceso de ingreso, Celery y los
workers, Redis, SQLite, Docker.

**Regla práctica**: no escribir código de lo no aprobado. Si algo es imprescindible para
lo aprobado —como delimitar mensajes sobre TCP— implementarlo en su versión mínima y
aislada, para poder cambiarlo sin arrastrar el resto.

### Distinción importante: framing vs protocolo

Surgió al empezar a codear y conviene conservarla:

- El **framing** (delimitar mensajes: dónde empieza y termina cada uno) es **inevitable**,
  porque TCP entrega un flujo continuo de bytes sin separación. Es parte de "comunicarlos
  por sockets", que sí está aprobado. Vive en `app/common/protocol.py` y no sabe nada de
  la aplicación: no menciona imágenes, operaciones ni `job_id`.
- El **protocolo de aplicación** (qué mensajes existen, qué campos, qué responde cada uno)
  es lo que está pendiente de aprobación.

Analogía usada con el profesor: el framing es el sobre; el protocolo es el idioma y el
contenido de la carta.

Ojo con el vocabulario: en redes, "trama" es la unidad de la **capa de enlace**
(Ethernet). Lo nuestro son mensajes de **capa de aplicación**. Misma idea, otra capa.

### Próximos pasos acordados

1. `app/common/protocol.py` + `app/common/config.py` (mínimo imprescindible).
2. `app/client/client.py` — CLI completa.
3. `app/server/main.py` + `app/server/server.py` — proceso principal con `asyncio`.

---

## 2. Qué hace la aplicación

Servicio de red que recibe imágenes y las **sanitiza antes de publicarlas**: cubre caras,
borra metadatos EXIF (que incluyen las coordenadas GPS de dónde se sacó la foto) y
optimiza el peso.

Operaciones definidas (v1):

| Operación | Archivo de salida | Datos que devuelve |
|---|---|---|
| `inspect` | **ninguno** | auditoría de privacidad: caras, GPS, fecha, cámara |
| `anonymize` | imagen con caras cubiertas | cuántas caras detectó |
| `clean` | imagen sin metadatos | qué metadatos eliminó |
| `convert` | imagen en otro formato | formato origen y destino |
| `compress` | imagen recomprimida | tamaño original y final |
| `sanitize` | imagen saneada | resumen de las tres etapas |

`sanitize` encadena `anonymize → clean → compress` (con `chain` de Celery).
`inspect` es la demo de apertura: mostrar que una foto del celular revela dónde se tomó.

**El proyecto no tiene nombre propio** — decisión explícita del usuario. Referirse a él
como "la aplicación" o "el sistema".

---

## 3. Arquitectura en breve

Dos mitades a ritmos distintos, unidas por una cola: una **atiende** (I/O, tiene que ser
rápida siempre) y otra **procesa** (CPU, tarda segundos por imagen).

```
Clientes CLI ──socket TCP──► Servidor (asyncio) ──2×mp.Queue──► Proceso de ingreso
                                   │                                    │
                                   └──► Redis (cola) ──► Workers        └──► SQLite
                                              │              │
                                              └──────────────┴──► Volumen compartido
```

Cinco canales, cada uno con su alcance: socket TCP (entre máquinas), `mp.Queue` (procesos
emparentados de la misma máquina), Redis (procesos sin relación), volumen compartido (los
archivos), SQLite (el historial, que el ingreso escribe y el servidor lee).

El detalle completo está en `docs/`. **Consultarlo antes de tomar cualquier decisión de
diseño**, porque casi todo ya está resuelto y justificado ahí.

---

## 4. Documentación

| Archivo | Contenido |
|---|---|
| `docs/01_aplicacion.md` | problema, objetivo, operaciones, entidades, alcance y recortes |
| `docs/02_arquitectura.md` | componentes, canales, IPC, modelo de datos, módulos, flujo |
| `docs/03_tecnologias.md` | qué es cada tecnología y por qué se eligió frente a alternativas |
| `docs/04_protocolo.md` | especificación del protocolo cliente-servidor |
| `docs/img/arquitectura.svg` | diagrama, embebido en `02` |

Separación entre documentos, respetada estrictamente: **`03`** explica tecnologías (qué es
un socket, cómo funciona asyncio); **`02`** explica cómo se combinan en este sistema;
**`04`** especifica el diálogo cliente-servidor. No duplicar contenido entre ellos.

---

## 5. Decisiones de diseño ya tomadas

No re-litigarlas sin motivo; están justificadas en los documentos.

- **asyncio en el servidor**, no un hilo por cliente: atender clientes es espera pura, y
  el GIL haría que los hilos no den paralelismo real.
- **El servidor nunca abre una imagen.** Para él son bytes que recibe, guarda y entrega.
  Interpretar contenido no confiable puede tirar abajo el proceso (Pillow y OpenCV son
  código nativo), y eso arrastraría todas las conexiones abiertas.
- **El proceso de ingreso es un proceso y no un hilo** por una única razón: aislamiento
  ante fallas. Es lo único que un proceso ofrece y un hilo no.
- **Celery y no `multiprocessing.Pool`**: los procesos de un Pool son hijos del servidor,
  mueren con él y no pueden estar en otra máquina.
- **Por la cola viajan rutas, no imágenes.** El broker es para mensajes de control.
- **Un trabajo produce a lo sumo un archivo**, identificado por su `job_id`.
- **`job_id` es UUID v4**, no un contador: no es enumerable y no necesita coordinación.
- **La deduplicación filtra por usuario**, por privacidad: reutilizar el trabajo de otro
  le revelaría que procesó la misma imagen.
- **SQLite con un único escritor** (el ingreso) y lectores libres (el servidor). SQLite
  admite muchos lectores; la restricción es solo sobre escritores.
- **Redis es estado vivo y efímero** (expira a las 24 h); **SQLite es la verdad
  permanente**. Por eso una descarga nunca consulta Redis.
- **Dual-stack IPv4/IPv6** en el servidor: la cátedra dedicó material a IPv6 y sostenerlo
  no cuesta trabajo extra.

## 6. Decisiones descartadas — no reintroducir

- **Artefactos** (un segundo nivel de direccionamiento `(job_id, artifact)`): se descartó
  porque ninguna operación produce más de un archivo. Era complejidad sin uso.
- **Comparación con HTTP en `04_protocolo.md`**: se sacó por pedido del usuario. La
  justificación de "sockets directos y no un framework HTTP" vive en `03_tecnologias.md`.
- **Nombre propio del proyecto**: se descartó.
- **El proceso hijo como mero escritor de logs**: se rechazó porque no justificaba ser un
  proceso (un hilo alcanzaba). De ahí nació el proceso de ingreso.

---

## 7. Convenciones de código

### Tipado obligatorio

**Todas** las funciones llevan anotaciones de tipo en parámetros y retorno. Sin
excepciones, incluidas las privadas y las corrutinas.

```python
def pack_header(header: dict[str, Any]) -> bytes: ...
async def recv_payload(reader: asyncio.StreamReader, size: int) -> bytes: ...
async def send_message(writer: asyncio.StreamWriter, header: dict[str, Any],
                       payload: bytes = b"") -> None: ...
```

Usar los tipos nativos de Python moderno (`dict[str, Any]`, `list[str]`, `str | None`),
no los de `typing` salvo cuando haga falta (`Any`, `Callable`, `Protocol`).

### Docstrings obligatorios

Toda función lleva docstring con: **qué hace**, **qué recibe** (tipo y para qué sirve cada
parámetro), **qué devuelve** y **qué excepciones lanza** si corresponde. En español,
estilo Google.

```python
async def send_file(writer: asyncio.StreamWriter, header: dict[str, Any],
                    path: Path) -> None:
    """Envía un mensaje cuyo payload es un archivo, transmitido en bloques.

    No carga el archivo en memoria: lo lee y lo escribe de a CHUNK_SIZE bytes.
    El drain() posterior a cada bloque frena al emisor cuando el receptor no da
    abasto, y de paso le devuelve el control al event loop.

    Args:
        writer: Stream de escritura de una conexión ya establecida.
        header: Campos del mensaje; se le agrega 'payload_size' automáticamente.
        path: Ruta del archivo cuyo contenido se envía como payload.

    Returns:
        None. El archivo queda enviado cuando la corrutina termina.

    Raises:
        ProtocolError: Si el archivo supera MAX_PAYLOAD.
        OSError: Si el archivo no se puede abrir o leer.
    """
```

Los módulos también llevan docstring: qué problema resuelven y, cuando aplica, el formato
de datos con el que trabajan (ver el ejemplo del framing en `protocol.py`).

### Nombres explicativos

**El nombre de una función o una variable tiene que decir qué es o qué hace.** Nunca una
sola letra, ni abreviaturas que haya que adivinar. Si el nombre necesita un comentario al
lado para entenderse, el nombre está mal elegido.

```python
# mal
n = int.from_bytes(p, "big")
for c in f.read(CHUNK_SIZE): ...
def sf(w, h, p): ...

# bien
header_length = int.from_bytes(length_prefix, "big")
for chunk in image_file.read(CHUNK_SIZE): ...
def send_file(writer, header, path): ...
```

Esto vale también para los contadores y las variables temporales: `remaining_bytes` en
lugar de `r`, `client_address` en lugar de `addr`. Las únicas excepciones aceptables son
las convenciones universales del lenguaje —`self`, `_` para un valor que se descarta— y
nada más.

Las funciones se nombran con un **verbo** que diga qué hacen (`send_file`, `recv_header`,
`pack_header`, `resolve_job`), y las variables con un **sustantivo** que diga qué
contienen (`payload_size`, `pending_requests`, `shared_volume`).

### Otras convenciones

- **Identificadores en inglés, comentarios y docstrings en español.** Es lo que se usa en
  la documentación y lo que el profesor va a leer.
- **Constantes en un solo lugar** (`app/common/config.py` y las del protocolo en
  `protocol.py`). Nada de números mágicos repartidos.
- **Comentarios que expliquen el porqué, no el qué.** El código ya dice qué hace.
- Los comentarios que justifican una decisión de diseño deben coincidir con lo que dice
  `docs/`. Si difieren, uno de los dos está desactualizado.

---

## 8. Cómo trabajar en este repo

**Commits frecuentes e incrementales.** El enunciado lo pide explícitamente y lo evalúa:
el profesor quiere ver la evolución, no un volcado final. Commitear aunque no funcione.

**Explicar antes de escribir.** El usuario está preparando una defensa oral: le sirve más
entender por qué se hace algo que tener el código hecho. Cuando pide una explicación,
darla completa antes de tocar archivos.

**Escribir para alguien que está aprendiendo.** Explicar cada concepto antes de usarlo,
no dar por sabida la jerga. El usuario corrigió explícitamente documentos que "usaban
términos sin explicarlos".

**Revisiones críticas.** Cuando el usuario dice "revisemos X", espera un informe de
hallazgos —inconsistencias, huecos, afirmaciones sin sostén— con una recomendación, no
una edición inmediata. Aplica los cambios recién cuando dice "dale".

**Señalar el sobre-diseño.** El usuario rechazó una abstracción innecesaria (los
artefactos) y tenía razón. Antes de agregar una capa, verificar que resuelva un problema
que el proyecto realmente tiene.

**Coherencia cruzada.** Después de cualquier cambio de diseño, verificar que los cuatro
documentos y el diagrama sigan diciendo lo mismo: valores numéricos, nombres de módulos,
estados, eventos, códigos de error y referencias entre secciones.

**Honestidad sobre los límites.** Los documentos declaran explícitamente lo que el
sistema no resuelve (el usuario se declara y no se autentica; las cascadas de Haar fallan
con caras de perfil; el volumen compartido limita la distribución de workers). Mantener
ese criterio: es lo que hace defendible el trabajo.
