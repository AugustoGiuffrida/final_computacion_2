# Pendientes y mejoras futuras

Lo que el sistema **no** hace, ordenado por lo que costaría y lo que aportaría. La mayoría
son recortes conscientes: quedaron afuera por alcance, no por olvido.

**El diseño está completo**: lo que sigue son mejoras y decisiones de alcance, no partes
faltantes.

---

## Robustez

### Recuperar los trabajos en vuelo tras un reinicio

Si el servidor se reinicia mientras hay trabajos procesándose, **pierde de vista esas
tareas**. El worker las termina igual y el resultado queda en Redis, pero nadie actualiza
el índice y el trabajo queda mostrando el último estado conocido.

*Cómo:* guardar el `task_id` de Celery junto al trabajo y, al arrancar, volver a vigilar
los que estén sin terminar.

### El volumen NFS es un punto único de falla

Si el servidor NFS se cae o la máquina que lo sirve se suspende, el servidor no puede
guardar imágenes y todos los envíos fallan. Con `soft` al menos fallan rápido y con un
mensaje, en vez de colgarse, pero el sistema queda inutilizable hasta que vuelva.

*Cómo se resolvería:* almacenamiento replicado, o de objetos (S3), donde el cliente
reintenta contra otro nodo. Está fuera del alcance de un trabajo final, pero es la
limitación que primero aparecería en uso real.

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
- **`image_server.py` es el que más docstring acumula**: 261 líneas, 49% del archivo. Como
  proporción hay módulos más altos —`incoming.py` llega a 74%— pero ninguno tiene tanto
  texto junto. Merece la misma pasada de recorte que se le hizo a los otros.
