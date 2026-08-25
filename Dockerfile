FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache curl su-exec postgresql-client \
    && pip install --no-cache-dir "psycopg[binary]>=3.2,<4"

COPY server.py runner.py dashboard_runner.py app_runner.py /app/
COPY index.html styles.css app.js dashboard_metrics.js settings.js features.js features_optional_fix.js role_dashboard.js average_sale_price.js analytics_dashboard.js inventory_intelligence.js /app/public/
COPY docker-entrypoint.sh /usr/local/bin/stockroom-entrypoint
COPY tools/backup-postgres.sh tools/restore-postgres.sh tools/verify-restore.sh /usr/local/bin/

RUN addgroup -S stockroom && adduser -S stockroom -G stockroom \
    && chown -R stockroom:stockroom /app \
    && chmod 755 /usr/local/bin/stockroom-entrypoint /usr/local/bin/backup-postgres.sh /usr/local/bin/restore-postgres.sh /usr/local/bin/verify-restore.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/stockroom-entrypoint"]
CMD ["python", "/app/app_runner.py"]
