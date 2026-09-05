# Una sola imagen para el servidor y los workers: comparten el código y las dependencias.
# Lo que los distingue es el comando con que se los arranca, no lo que llevan adentro.
#
# `slim` alcanza: OpenCV se instala en su variante `headless`, que no arrastra las
# bibliotecas de interfaz gráfica que un servidor no tiene ni necesita.
FROM python:3.14-slim

WORKDIR /app

# Las dependencias primero y el código después. Docker guarda una capa por instrucción y
# reutiliza las que no cambiaron: mientras requirements.txt siga igual, cambiar el código
# reconstruye en segundos en vez de reinstalar todo.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Sin esto Python retiene la salida en un buffer y `docker compose logs` no muestra nada
# hasta que se llena. En un servicio que se mira por su registro, es esencial.
#
# Es lo único que se fija acá: es cómo se comporta Python, y vale igual en cualquier
# despliegue. Las rutas y las URLs las decide el compose, que es quien monta los volúmenes
# y le pone el nombre a Redis.
ENV PYTHONUNBUFFERED=1

# El valor por defecto es el servidor; el compose se lo cambia a los workers.
CMD ["python", "-m", "app.server", "--host", "0.0.0.0"]
