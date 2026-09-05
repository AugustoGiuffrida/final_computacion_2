# Pendientes y mejoras futuras

Lo que el sistema **no** hace, ordenado por lo que costaría y lo que aportaría. La mayoría
son recortes conscientes: quedaron afuera por alcance, no por olvido.

---

## Lo que falta para completar el diseño actual

### Persistir los eventos del ciclo de vida

**El más importante de la lista.** El monitor detecta los cambios de estado y actualiza el
índice en memoria, pero **no los escribe en la base**. La tabla `events` existe en el
esquema y está vacía.

Arrastra dos consecuencias visibles:

- **La deduplicación no dispara sola.** Consulta la base buscando trabajos en `DONE`, y la
  base sigue viendo todo `QUEUED`. La funcionalidad está implementada y probada, pero en
  uso real no llega a activarse.
- **El historial de sesiones anteriores mostraría estados desactualizados**, por lo mismo.

*Cómo:* el monitor le manda los eventos al proceso de ingreso por el pipe —que ya existe— y
este los escribe. Por el pipe pasarían dos clases de mensaje: la revisión, que espera
respuesta, y los eventos, que no.

*Tamaño:* unas 85 líneas.

### Historial completo contra la base

Hoy `history` se arma solo con el índice en memoria de la sesión actual. Consultar un
trabajo viejo **por su identificador** sí funciona —el registro cae a la base—, pero
listarlos todos no.

Depende del punto anterior: mezclar memoria y base solo tiene sentido cuando el estado
guardado es confiable.

### Despliegue en contenedores

`Dockerfile` y `docker-compose.yml` con cuatro servicios: Redis, un servidor NFS, el
servidor y uno o más workers.

Está preparado: la base ya se separó del volumen compartido justamente para esto, y se
verificó que el montaje NFS entre contenedores funciona. El detalle a cuidar es que
`addr=` en el volumen tiene que ser una **IP** y no un nombre de servicio, porque el
montaje lo hace el demonio de Docker, que no está en la red de compose.

---

## Robustez

### Recuperar los trabajos en vuelo tras un reinicio

Si el servidor se reinicia mientras hay trabajos procesándose, **pierde de vista esas
tareas**. El worker las termina igual y el resultado queda en Redis, pero nadie actualiza
el índice y el trabajo queda mostrando el último estado conocido.

*Cómo:* guardar el `task_id` de Celery junto al trabajo y, al arrancar, volver a vigilar
los que estén sin terminar.

### Limpieza de resultados viejos

Nada borra las imágenes procesadas: `storage/` crece indefinidamente. Debería haber una
tarea periódica —Celery tiene `beat` para esto— que elimine los resultados pasado cierto
tiempo.

El servidor ya contempla el caso: si un trabajo figura `DONE` pero su archivo no está,
responde un error claro en vez de fallar.

### Reintentos de tareas fallidas

Celery los soporta con `max_retries` y `default_retry_delay`, y hoy no se usan. Una tarea
que falla queda en `ERROR` sin segundo intento. Habría que distinguir los fallos que
tienen sentido reintentar —un disco lleno, un error transitorio— de los que no, como una
imagen corrupta.

---

## Funcionalidad

### Autenticación

`--user` es **declarativo**: el cliente dice quién es y el servidor le cree. La regla de
propiedad funciona —nadie ve trabajos ajenos— pero se apoya en la honestidad del cliente.

Es un recorte consciente: el enunciado no pide autenticación y agregarla habría desviado
el foco de los mecanismos que la materia evalúa. En un sistema real haría falta, y el
protocolo ya tiene dónde ponerla: un campo más en el header.

### Cifrado del transporte

Los mensajes viajan en claro. Para un servicio que procesa fotos personales, TLS sería
obligatorio. `asyncio.start_server` acepta un contexto SSL, así que el cambio sería
acotado.

### Más operaciones

- **Detección de patentes o matrículas**: existe una cascada de OpenCV, pero suma riesgo de
  falsos positivos y no aporta un mecanismo nuevo.
- **Marcas de agua**, **recorte**, **rotación**: todas son una tarea más con el patrón que
  ya está.

### Mejor detección de caras

La cascada Haar solo detecta **caras de frente**. Se le escapan las de perfil, las muy
inclinadas y las parcialmente tapadas. Un detector basado en redes —el `FaceDetectorYN` de
OpenCV 5— sería bastante más preciso, a cambio de un modelo binario en el repositorio y de
perder la explicabilidad del método actual.

---

## Escalabilidad

### Varios procesos de ingreso

Hoy hay uno solo, y las revisiones hacen fila. Con varios habría que reemplazar el pipe por
una cola —que sí admite varios consumidores— y repartir el trabajo.

No es urgente: revisar una imagen es rápido comparado con procesarla, que es lo que ya está
paralelizado en los workers.

### Interfaz visual

El enunciado lo menciona como adicional. Una interfaz web mostraría mejor el antes y
después de una imagen, que en la terminal hay que abrir aparte. El protocolo no cambiaría:
sería otro cliente hablando el mismo idioma.

---

## Deuda técnica menor

- **`--verbose` no llega al proceso hijo.** Sube el detalle del registro del proceso
  principal, pero el de ingreso se queda en INFO: recibe el nivel al construirse el canal.
  Son tres líneas.
- **`ProgressCallback` en `protocol.py`** quedó con el comentario al costado, el único con
  el estilo viejo: la limpieza de constantes solo alcanzó a las que tenían `Final`.
- **`image_server.py` tiene 42% de docstring**, bastante más que el resto. Merece la misma
  pasada de recorte que se le hizo a los otros módulos.
