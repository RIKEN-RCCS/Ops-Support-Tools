FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUPPORT_AI_QUEUE_DIR=/data/queue \
    SUPPORT_AI_AGENTS_FILE=/config/agents.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/queue /config && \
    cp /app/agents.example.json /config/agents.json && \
    useradd --system --create-home --uid 10001 supportai && \
    chown -R supportai:supportai /data /app /config

USER supportai

EXPOSE 8080
ENTRYPOINT ["python", "startup.py"]
CMD ["python", "webhook.py"]
