FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ROAD_DAMAGE_RESULT_DIR=/tmp/road-damage-results

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . .
USER appuser

EXPOSE 8080

CMD exec gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --threads 8 \
    --timeout 3600 \
    --access-logfile - \
    --error-logfile - \
    "road_damage:create_app()"