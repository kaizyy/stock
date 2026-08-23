FROM python:3.12-alpine

WORKDIR /app

COPY server.py /app/server.py
COPY index.html styles.css app.js /app/public/

RUN mkdir -p /data && addgroup -S stockroom && adduser -S stockroom -G stockroom \
    && chown -R stockroom:stockroom /app /data

USER stockroom

ENV STOCKROOM_DB_PATH=/data/stockroom.db

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:8000/health || exit 1

CMD ["python", "/app/server.py"]

