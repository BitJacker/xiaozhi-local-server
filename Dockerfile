# Husky locale in un container (Linux). Modelli e dati stanno in due volumi: /app/models e /app/data.
#   docker build -t husky-locale .                  (solo processore)
#   docker build -t husky-locale --build-arg GPU=1 .  (schede NVIDIA: aggiunge le librerie CUDA per Whisper)
# Di solito si usa docker compose (vedi docker-compose.yml), che avvia anche Ollama.
FROM python:3.12-slim

ARG GPU=0
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/models/hf \
    HUSKY_DATA=/app/data \
    HUSKY_CONTAINER=1

WORKDIR /app
COPY requirements.txt ./
COPY requirements/ ./requirements/
RUN pip install -r requirements.txt \
    && if [ "$GPU" = "1" ]; then pip install -r requirements/gpu.txt; fi

COPY . .
# il server non gira come root; i volumi appartengono all'utente husky
RUN useradd --system --uid 10001 --home-dir /app husky \
    && mkdir -p /app/data /app/models /app/backups \
    && chown -R husky:husky /app/data /app/models /app/backups \
    && chmod +x docker/entrypoint.sh
USER husky

# 8000 HTTP (dispositivi), 8443 HTTPS (browser), 1883 MQTT, 8884/udp voce dei dispositivi MQTT+UDP
EXPOSE 8000 8443 1883 8884/udp
VOLUME ["/app/data", "/app/models", "/app/backups"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
ENTRYPOINT ["docker/entrypoint.sh"]
