FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache curl su-exec postgresql-client \
    && pip install --no-cache-dir "psycopg[binary]>=3.2,<4" "reportlab>=4.2,<5"

COPY server.py runner.py dashboard_runner.py app_runner.py order_management.py order_delete.py warehouse_ops.py business_tools.py platform_admin.py billing.py account_tools.py email_events.py security_integrations.py backup_status.py documents_v2.py v11_integrations.py extended_runner.py /app/
COPY index.html styles.css app.js dashboard_metrics.js settings.js settings_tools.js security_integrations_ui.js backup_status_ui.js platform_v2_ui.js documents_v2_ui.js features.js features_optional_fix.js role_dashboard.js analytics_dashboard.js inventory_intelligence.js barcode_scanner_fallback.js crm_orders.js order_delete_ui.js dynamic_navigation.js warehouse_ops.js business_tools.js platform_admin_ui.js billing_ui.js /app/public/
COPY docker-entrypoint.sh /usr/local/bin/stockroom-entrypoint
COPY tools/backup-postgres.sh tools/restore-postgres.sh tools/verify-restore.sh /usr/local/bin/

# Generate a fresh frontend asset version for every image build. Any source change
# invalidates the preceding COPY layer, so deployments can never keep serving an
# older JS/CSS URL from a browser/proxy cache.
RUN ASSET_VERSION="$(date +%s)" \
    && printf '\nimport("/security_integrations_ui.js?v=%s").catch(()=>{});\n' "$ASSET_VERSION" >> /app/public/settings_tools.js \
    && printf '\nimport("/backup_status_ui.js?v=%s").catch(()=>{});\nimport("/platform_v2_ui.js?v=%s").catch(()=>{});\n' "$ASSET_VERSION" "$ASSET_VERSION" >> /app/public/platform_admin_ui.js \
    && printf '\nimport("/documents_v2_ui.js?v=%s").catch(()=>{});\n' "$ASSET_VERSION" >> /app/public/crm_orders.js \
    && find /app/public -type f \( -name '*.html' -o -name '*.js' -o -name '*.css' \) -exec sed -i -E "s/v=20[0-9A-Za-z._-]+/v=${ASSET_VERSION}/g" {} + \
    && sed -i -E "s/v=20[0-9A-Za-z._-]+/v=${ASSET_VERSION}/g" /app/extended_runner.py \
    && printf '%s\n' "$ASSET_VERSION" > /app/asset-version

RUN addgroup -S stockroom && adduser -S stockroom -G stockroom \
    && chown -R stockroom:stockroom /app \
    && chmod 755 /usr/local/bin/stockroom-entrypoint /usr/local/bin/backup-postgres.sh /usr/local/bin/restore-postgres.sh /usr/local/bin/verify-restore.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent --show-error http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/stockroom-entrypoint"]
CMD ["python", "-c", "import security_integrations; security_integrations.install(); import v11_integrations; v11_integrations.install(); import email_events; email_events.install(); import runpy; runpy.run_path('/app/extended_runner.py', run_name='__main__')"]
