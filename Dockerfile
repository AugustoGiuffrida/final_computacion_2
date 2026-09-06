FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Sin esto Python retiene la salida en un buffer y `docker compose logs` no muestra nada
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.server", "--host", "0.0.0.0"]
