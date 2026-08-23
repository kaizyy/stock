FROM nginx:1.27-alpine

RUN apk add --no-cache apache2-utils

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html styles.css app.js /usr/share/nginx/html/
COPY docker-entrypoint.sh /usr/local/bin/stockroom-entrypoint

RUN chmod 755 /usr/local/bin/stockroom-entrypoint

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1/health || exit 1

ENTRYPOINT ["/usr/local/bin/stockroom-entrypoint"]

