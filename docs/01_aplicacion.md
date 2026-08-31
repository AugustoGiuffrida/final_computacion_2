# Definición de la aplicación

*Servicio de anonimización y sanitización de imágenes para publicación segura.*

## 1. Problema que soluciona

Cada foto que se publica en internet filtra más información de la que se ve:

- **Caras de terceros** que no dieron su consentimiento para aparecer.
- **Metadatos invisibles**: coordenadas GPS del lugar donde se tomó (tu casa,
  tu trabajo), fecha y hora exactas, modelo y número de serie de la cámara.
- Además, las fotos originales **pesan** mucho más de lo necesario para la web.

Antes de publicar una foto (una persona, un diario digital, una organización que
documenta eventos) hay que **sanitizarla**: cubrir caras, borrar metadatos, optimizar
peso. Hacerlo a mano, foto por foto, no escala; y el procesamiento (en particular la
detección de caras) es **CPU-bound y lento**.

**La aplicación** es un servicio de red que resuelve esto: recibe imágenes de múltiples
clientes concurrentes, encola el procesamiento pesado en una cola distribuida, y
permite consultar el estado y descargar los resultados. La recepción nunca se bloquea
por el procesamiento, y la capacidad se escala agregando workers.

## 2. Objetivo

Construir una aplicación cliente-servidor en Python donde:

- El **cliente CLI** envía imágenes, elige la operación, consulta estados, descarga
  resultados y revisa su historial.
- El **servidor** (asyncio) atiende N clientes concurrentes y delega el procesamiento
  en **workers Celery** a través de un broker **Redis**.
- Un **proceso de ingreso** (IPC con el servidor) revisa cada imagen que entra —la
  verifica, la identifica y descarta duplicados— y persiste el historial en **SQLite**.
- Todo el stack se despliega con **Docker Compose**.

## 3. Operaciones soportadas (v1)

Todas reciben una imagen JPEG o PNG. Cada operación produce **a lo sumo un archivo de
salida** —descargable con el `job_id`— y un conjunto de **datos**, que son unos pocos
cientos de bytes y llegan en la respuesta de `status`, sin necesidad de descargar nada.

| Operación   | Parámetros                                   | Archivo de salida        | Datos que devuelve |
|-------------|----------------------------------------------|--------------------------|--------------------|
| `inspect`   | —                                            | **ninguno**              | Auditoría de privacidad completa: caras detectadas, GPS presente (y dónde apunta), fecha, cámara y número de serie, peso |
| `anonymize` | `mode` (`blur`/`pixelate`/`box`), `strength` | imagen con caras cubiertas | cuántas caras detectó |
| `clean`     | —                                            | imagen sin metadatos     | qué metadatos eliminó |
| `convert`   | `format` (`webp`/`jpeg`/`png`), `quality`    | imagen en el nuevo formato | formato origen y destino, tamaño final |
| `compress`  | `quality` (1-95), `max_size` (lado máximo)   | imagen recomprimida      | tamaño original y final |
| `sanitize`  | `mode`, `strength`, `quality`, `max_size`    | imagen saneada           | resumen de las tres etapas |

Notas de diseño:

- **`sanitize` es la operación estrella**: representa el caso de uso real —dejar una foto
  lista para publicar— y encadena `clean` → `anonymize` → `compress`. Se implementa con
  **composición de tareas Celery (`chain`)**, donde cada etapa recibe la salida de la
  anterior. Es el argumento de composición de tareas en la defensa.

  El orden importa y se descubrió probándolo: la limpieza de metadatos va **primera**
  porque es la única etapa que puede contar cuántos había. Cualquier etapa que guarde la
  imagen antes se los lleva puestos —Pillow escribe metadatos solo si se le pasan— y la
  limpieza informaría cero. De paso, los archivos intermedios nunca llevan las
  coordenadas GPS de la foto. No incluye `convert`
  porque `compress` ya reescribe el archivo; convertir de formato es una decisión
  explícita del usuario, no parte del saneamiento.
- **`inspect` es la demo de apertura**: mostrar que una foto cualquiera del celular
  revela las coordenadas de dónde se tomó vale más que cualquier explicación. Es también
  la única operación que no genera archivo.
- `convert` y `compress` comparten implementación (el guardado de Pillow) pero son
  operaciones distintas de cara al usuario.

## 4. Tecnología del procesamiento

- **Detección de caras**: OpenCV con cascadas de Haar (`haarcascade_frontalface`),
  incluidas en el paquete `opencv-python` — sin descargas externas ni modelos de ML
  pesados. Limitación conocida y asumida: detecta mejor caras frontales que de perfil
  (mitigación en TODO: sumar la cascada de perfil).
- **Difuminado/pixelado y re-encodeo**: Pillow (GaussianBlur, resize, save con
  quality/format, re-guardado sin EXIF).
- **Lectura de EXIF**: Pillow (`Image.getexif()`), incluyendo el bloque GPS.

## 5. Entidades del dominio

- **Usuario**: identificado por nombre pasado por CLI (`--user`). Sin contraseñas en v1;
  el nombre se declara, no se autentica (ver limitación en
  [04_protocolo.md](04_protocolo.md), sección 2.3).
- **Trabajo (job)**: una solicitud de procesamiento. Se identifica con un **`job_id`
  (UUID v4)** que genera el servidor al aceptarlo y devuelve al cliente. Contiene además
  usuario, operación, parámetros, imagen de entrada, estado, timestamps y resultado o
  error. **Pertenece al usuario que lo creó**: solo él puede consultarlo y descargarlo.
- **Resultado**: lo que produce un trabajo. Tiene dos partes: **a lo sumo un archivo de
  salida** (la imagen procesada), identificado por el propio `job_id` y descargable con
  `download`; y los **datos** de la operación (cuántas caras se detectaron, qué
  metadatos se eliminaron, el informe de `inspect`), que son unos pocos cientos de bytes
  y llegan en la respuesta de `status`, sin necesidad de descargar nada. `inspect` es el
  caso en que solo hay datos y ningún archivo.
- **Evento**: registro de cada transición de un trabajo, persistido por el proceso de
  ingreso. Son cuatro: `queued`, `started`, `done` y `failed`.
- **Huella del contenido (`sha256`)**: identifica una imagen por lo que *es*, no por
  cómo se llama. Dos archivos con nombres distintos y el mismo contenido tienen la misma
  huella. Es lo que permite detectar que un trabajo ya fue hecho.

## 6. Ciclo de vida de un trabajo

```
            cliente envía          servidor encola        worker toma        worker termina
  (no existe) ────────► QUEUED ─────────────────► PROCESSING ─────────► DONE
                                                       │
                                                       └──────────────► ERROR (con motivo)
```

Estados expuestos al cliente: `QUEUED`, `PROCESSING`, `DONE`, `ERROR`.
(Mapeados desde los estados de Celery: PENDING/STARTED/SUCCESS/FAILURE.)

## 7. Funcionalidades por entidad

### Cliente CLI
- `--user`, `--host`, `--port` (identidad y conexión; el puerto por defecto es 9000).
  `--host` acepta tanto direcciones IPv4 como IPv6, o un nombre de host.
- `--action submit --file foto.jpg --op <operación> [parámetros]` → devuelve `job_id`.
  Si esa imagen ya fue procesada con esa misma operación, devuelve el `job_id` anterior
  ya terminado, y lo indica.
- `--action status --job-id X` → estado actual y datos del resultado.
- `--action download --job-id X -o salida` → descarga el archivo del trabajo.
- `--action history [--limit N]` → últimos trabajos del usuario.
- `--wait` en el submit: espera asíncrona y descarga directa al terminar, con
  `--timeout` (300 s por defecto) como límite.
- Validación local antes de enviar: que el archivo exista, que el formato esté soportado
  (JPEG o PNG) y que no supere el tamaño máximo (25 MB por defecto, configurable en el
  servidor).

### Servidor
- Acepta N clientes concurrentes (asyncio, TCP), **escuchando en IPv4 e IPv6 a la vez**
  mediante un socket de escucha por familia, ambos sobre el mismo puerto.
- Valida la **forma** de cada solicitud y guarda el original en el almacenamiento
  compartido. **Nunca abre la imagen**: para el servidor son bytes.
- Le pide al proceso de ingreso que la revise y espera su confirmación; solo encola en
  Celery si la aprobó y no es duplicada.
- Responde consultas de estado, historial y descargas, resolviendo cada trabajo contra su
  índice en memoria y, si no está ahí, contra SQLite en modo solo lectura.
- Notifica los eventos posteriores al proceso de ingreso por IPC, y lo relanza si muere.
- Apagado limpio ante SIGINT/SIGTERM.

### Workers Celery
- Una tarea por operación (`inspect`, `anonymize`, `clean`, `convert`, `compress`).
- `sanitize` como `chain` de subtareas.
- Reintentos automáticos ante fallos transitorios; errores definitivos con motivo.
- Escalado horizontal: `docker compose up --scale worker=N`.

### Proceso de ingreso
- Por cada imagen que llega: la **verifica** (que sea válida y no esté corrupta),
  calcula su **SHA-256** y busca si ese contenido ya fue procesado por ese usuario con
  esa misma operación y parámetros. Le confirma al servidor una de tres cosas: inválida,
  duplicada o nueva.
- Es el **único proceso que abre imágenes fuera de los workers**, y por eso vive
  aislado: decodificar contenido no confiable puede tirar abajo el proceso, y acá esa
  caída no arrastra al servidor.
- Es el **único escritor** de SQLite. El servidor lee de la base por su cuenta, en modo
  solo lectura, para responder el historial y para recuperar los trabajos en curso
  cuando se reinicia.

## 8. Alcance y recortes (v1)

**Dentro**: todo lo anterior.
**Fuera (y por qué)**:
- Detección de patentes/matrículas → existe cascada de OpenCV pero suma riesgo; TODO.
- Detección de caras con redes neuronales (DNN) → mejora la tasa de acierto pero
  agrega peso y complejidad sin aportar a los objetivos de la materia; TODO.
- Autenticación con contraseña → usuario por CLI alcanza para demostrar el sistema.
- Interfaz web propia → Flower (panel de Celery) ya da visibilidad sin código extra.
- Cifrado del canal (TLS) → documentado como mejora en TODO.
