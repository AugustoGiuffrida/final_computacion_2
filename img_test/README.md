# Imágenes de prueba

Las que usa el guion de demostración (`docs/05_demostracion.md`). Están versionadas para
que la demostración no dependa de generar nada en el momento.

| Archivo | Qué es | Para qué |
|---|---|---|
| `grupo.jpg` | una foto grupal con 12 caras, 2 metadatos EXIF | el envío principal |
| `grupo_copia.jpg` | copia byte a byte del anterior | la deduplicación: mismo contenido, otro nombre |
| `paisaje.jpg` | un paisaje, sin personas | que no haya caras es un resultado válido |
| `rota.jpg` | `grupo.jpg` sin sus últimos 5 bytes | el rechazo por imagen corrupta |
| `grupo_grande.jpg` | el mismo grupo, ampliado a 5120×4096 (1.8 MB) | medir el paralelismo de los workers |
| `con_metadatos.jpg` | el mismo grupo, con EXIF completo: GPS, fecha, cámara y número de serie | la auditoría de `inspect` |

`rota.jpg` es la más interesante: con cinco bytes de menos, la verificación de estructura
la da por buena y solo la decodificación de píxeles la descubre. Por eso el proceso de
ingreso abre cada imagen **dos veces**.

## De dónde salen

- **`grupo.jpg`**: una foto grupal del conjunto de prueba de detección facial de
  [OpenCV](https://github.com/opencv/opencv_extra). Se eligió una foto de grupo y no un
  retrato a propósito: nadie es el sujeto de la imagen, y para una demostración de
  anonimización se ve mucho mejor el detector encontrando doce caras y cubriéndolas todas.
- **`paisaje.jpg`**: *La noche estrellada* de Van Gogh, dominio público. Viene con los
  ejemplos de OpenCV.
- **`grupo_grande.jpg`**: `grupo.jpg` ampliado cuatro veces, conservando sus metadatos.
  Existe por una razón medida: con la imagen chica, ocho trabajos tardan lo mismo con uno
  que con seis procesos de worker —el costo está en arrancar Python en cada cliente, no en
  procesar—, así que el paralelismo no se puede mostrar. Con esta, los mismos ocho trabajos
  pasan de 14 a 5 segundos. No es más grande por capricho: es el tamaño que hace visible lo
  que se quiere demostrar.
- **`con_metadatos.jpg`**: `grupo.jpg` con los metadatos que deja una cámara real —marca,
  modelo, fecha de captura, número de serie y coordenadas GPS—. Las coordenadas son de la
  Plaza Independencia de Mendoza, un lugar público: la idea es mostrar el mecanismo, no
  publicar la ubicación de nadie. Es la imagen de la apertura de la demostración, la que
  hace visible que una foto dice dónde se sacó.
- Los 2 metadatos EXIF de `grupo.jpg` se agregaron a propósito —marca de cámara y
  software de edición— porque son los que una cámara real deja escritos, y borrarlos es
  una de las tres cosas que hace `sanitize`.

`tests/imagenes/con_cara.jpg` es otra imagen, la que usan las pruebas automáticas de
detección de caras. Está separada para que cambiar las de la demostración no rompa las
pruebas.
