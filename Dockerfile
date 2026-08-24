FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache curl su-exec \
    && pip install --no-cache-dir "psycopg[binary]>=3.2,<4"

COPY server.py runner.py /app/
COPY index.html styles.css app.js /app/public/
COPY docker-entrypoint.sh /usr/local/bin/stockroom-entrypoint

RUN addgroup -S stockroom && adduser -S stockroom -G stockroom \
    && chown -R stockroom:stockroom /app \
    && chmod 755 /usr/local/bin/stockroom-entrypoint

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/stockroom-entrypoint"]
CMD ["python", "/app/runner.py"]
