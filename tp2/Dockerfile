# Tres etapas sobre la misma base:
#   base  -> Python + dependencias de la app
#   tests -> base + pytest + el código de tests  (docker compose run --rm tests)
#   app   -> base + el código de la app          (la que levanta `up`)
# La última es la que se construye por defecto con un `docker build` pelado.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Las dependencias van antes que el código: si sólo cambia el código, Docker
# reutiliza la capa del pip install y el build tarda segundos.
COPY requirements.txt .
RUN pip install -r requirements.txt


FROM base AS tests
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pytest.ini .
COPY app ./app
COPY tests ./tests
CMD ["python", "-m", "pytest", "-v"]


FROM base AS app
COPY app ./app
EXPOSE 8000
# Forma exec (lista JSON), no forma shell: así Python es el PID 1 y recibe
# directamente el SIGTERM de `docker compose stop/down`. Con la forma shell
# (`CMD python -m app.main`) el PID 1 sería /bin/sh, que no reenvía la señal:
# Docker esperaría el stop_grace_period y terminaría con SIGKILL, sin apagado
# ordenado ni volcado.
CMD ["python", "-m", "app.main"]
