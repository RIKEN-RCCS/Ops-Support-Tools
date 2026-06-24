FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KNOWLEDGE_DB=/data/db.sqlite \
    KNOWLEDGE_FIELD_KEY_FILE=/run/secrets/knowledge_field_key

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && \
    useradd --system --create-home --uid 10002 knowledge && \
    chown -R knowledge:knowledge /data /app

USER knowledge

EXPOSE 8080
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8080"]
