#!/bin/sh
set -eu

if [ -z "${STOCKROOM_USERNAME:-}" ]; then
  echo "STOCKROOM_USERNAME is verplicht." >&2
  exit 1
fi

if [ -z "${STOCKROOM_PASSWORD:-}" ]; then
  echo "STOCKROOM_PASSWORD is verplicht." >&2
  exit 1
fi

htpasswd -bc /etc/nginx/.htpasswd "$STOCKROOM_USERNAME" "$STOCKROOM_PASSWORD" >/dev/null
chmod 640 /etc/nginx/.htpasswd

unset STOCKROOM_PASSWORD

exec nginx -g "daemon off;"

