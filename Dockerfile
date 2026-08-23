FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache curl su-exec

COPY server.py /app/server.py
COPY index.html styles.css app.js /app/public/
COPY docker-entrypoint.sh /usr/local/bin/stockroom-entrypoint

RUN mkdir -p /data && addgroup -S stockroom && adduser -S stockroom -G stockroom \
    && chown -R stockroom:stockroom /app /data \
    && chmod 755 /usr/local/bin/stockroom-entrypoint

ENV STOCKROOM_DB_PATH=/data/stockroom.db

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/stockroom-entrypoint"]
CMD ["python", "/app/server.py"]

