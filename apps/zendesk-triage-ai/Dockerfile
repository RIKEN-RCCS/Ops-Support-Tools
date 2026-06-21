FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRIAGE_SPOOL_DIR=/data/spool \
    TRIAGE_AGENTS_FILE=/config/agents.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/spool /config && \
    cp /app/agents.example.json /config/agents.json && \
    useradd --system --create-home --uid 10001 triage && \
    chown -R triage:triage /data /app /config

USER triage

EXPOSE 8080
ENTRYPOINT ["python", "startup.py"]
CMD ["python", "webhook.py"]
