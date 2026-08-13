# Definición de la aplicación

*Servicio de anonimización y sanitización de imágenes para publicación segura.*

## 1. Problema que soluciona

Cada foto que se publica en internet filtra más información de la que se ve:

- **Caras de terceros** que no dieron consentimiento para aparecer publicados.
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
- Un **proceso auditor** (IPC con el servidor) persiste historial y estadísticas en **SQLite**.
- Todo el stack se despliega con **Docker Compose**.

## 3. Operaciones soportadas (v1)

Todas reciben una imagen (JPEG/PNG) y devuelven un archivo resultado, salvo `inspect`
que devuelve solo un informe JSON.

| Operación   | Parámetros                                   | Salida                                                        |
|-------------|----------------------------------------------|---------------------------------------------------------------|
| `inspect`   | —                                            | **Auditoría de privacidad** (JSON): caras detectadas, GPS presente (y dónde apunta), fecha, cámara/serie, peso. No modifica la imagen. |
| `anonymize` | `mode` (`blur`/`pixelate`/`box`), `strength` | Imagen con las caras cubiertas + cantidad de caras detectadas |
| `clean`     | —                                            | Imagen sin metadatos EXIF + informe de qué se eliminó         |
| `convert`   | `format` (`webp`/`jpeg`/`png`), `quality`    | Imagen en el nuevo formato                                    |
| `compress`  | `quality` (1-95), `max_size` (lado máximo)   | Imagen recomprimida y/o reescalada, más liviana               |
| `sanitize`  | combina los anteriores                       | **Pipeline completo**: anonymize → clean → compress/convert en una sola solicitud |

Notas de diseño:

- **`sanitize` es la operación estrella**: representa el caso de uso real y se
  implementa con **composición de tareas Celery (`chain`)** — cada etapa recibe la
  salida de la anterior. Es el argumento de composición de tareas en la defensa.
- **`inspect` es la demo de apertura**: mostrar que una foto cualquiera del celular
  revela las coordenadas de dónde se tomó vale más que cualquier explicación.
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

- **Usuario**: identificado por nombre pasado por CLI (`--user`). Sin contraseñas en v1.
- **Trabajo (job)**: una solicitud de procesamiento. `job_id` (UUID), usuario,
  operación, parámetros, imagen de entrada, estado, timestamps, resultado o error.
- **Artefacto**: archivo producido (imagen procesada y/o informe JSON).
- **Evento de auditoría**: registro de cada transición, persistido por el auditor.

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
- `--user`, `--host`, `--port` (conexión e identidad).
- `--action submit --file foto.jpg --op <operación> [parámetros]` → devuelve `job_id`.
- `--action status --job-id X` → estado actual.
- `--action download --job-id X -o salida` → descarga resultado(s).
- `--action history [--limit N]` → últimos trabajos del usuario.
- `--wait` en el submit: espera asíncrona y descarga directa al terminar.
- Validación local antes de enviar (archivo existe, formato soportado, tamaño máximo).

### Servidor
- Acepta N clientes concurrentes (asyncio, TCP, IPv4/IPv6).
- Valida solicitudes, guarda el original en el almacenamiento compartido, encola en Celery.
- Responde estado (result backend), historial (vía auditor) y descargas.
- Notifica cada evento al auditor por IPC.
- Apagado limpio ante SIGINT/SIGTERM.

### Workers Celery
- Una tarea por operación (`inspect`, `anonymize`, `clean`, `convert`, `compress`).
- `sanitize` como `chain` de subtareas.
- Reintentos automáticos ante fallos transitorios; errores definitivos con motivo.
- Escalado horizontal: `docker compose up --scale worker=N`.

### Proceso auditor
- Recibe eventos por `multiprocessing.Queue`; único escritor de SQLite.
- Responde consultas de historial/estadísticas reenviadas por el servidor.

## 8. Alcance y recortes (v1)

**Dentro**: todo lo anterior.
**Fuera (y por qué)**:
- Detección de patentes/matrículas → existe cascada de OpenCV pero suma riesgo; TODO.
- Detección de caras con redes neuronales (DNN) → mejora la tasa de acierto pero
  agrega peso y complejidad sin aportar a los objetivos de la materia; TODO.
- Autenticación con contraseña → usuario por CLI alcanza para demostrar el sistema.
- Interfaz web propia → Flower (panel de Celery) ya da visibilidad sin código extra.
- Cifrado del canal (TLS) → documentado como mejora en TODO.
