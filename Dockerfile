FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml ./
COPY readeck_tts ./readeck_tts
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home worker \
    && mkdir /data && chown worker:worker /data
USER worker
VOLUME ["/data"]
ENTRYPOINT ["readeck-tts"]
CMD ["run"]
