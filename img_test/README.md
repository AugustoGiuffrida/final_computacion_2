# Imágenes de prueba

Las que usa el guion de demostración (`docs/05_demostracion.md`). Están versionadas para
que la demostración no dependa de generar nada en el momento.

| Archivo | Qué es | Para qué |
|---|---|---|
| `grupo.jpg` | una foto grupal con 12 caras, 2 metadatos EXIF | el envío principal |
| `grupo_copia.jpg` | copia byte a byte del anterior | la deduplicación: mismo contenido, otro nombre |
| `paisaje.jpg` | un paisaje, sin personas | que no haya caras es un resultado válido |
| `rota.jpg` | `grupo.jpg` sin sus últimos 5 bytes | el rechazo por imagen corrupta |

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
- Los 2 metadatos EXIF de `grupo.jpg` se agregaron a propósito —marca de cámara y
  software de edición— porque son los que una cámara real deja escritos, y borrarlos es
  una de las tres cosas que hace `sanitize`.

`tests/imagenes/con_cara.jpg` es otra imagen, la que usan las pruebas automáticas de
detección de caras. Está separada para que cambiar las de la demostración no rompa las
pruebas.
