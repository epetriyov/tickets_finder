FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Слой зависимостей — кешируется отдельно от кода
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY afisha_api.py basta_watcher.py telegram_notify.py ./

# state.json и watcher.log — в volume (см. docker-compose.yml)
ENV DATA_DIR=/data

CMD ["python", "basta_watcher.py"]
