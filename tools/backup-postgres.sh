#!/bin/sh
set -eu

: "${DATABASE_URL:?DATABASE_URL is verplicht}"
BACKUP_DIR=${BACKUP_DIR:-/data/backups}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$BACKUP_DIR/stockroom-$stamp.dump"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT HUP INT TERM
pg_dump --dbname="$DATABASE_URL" --format=custom --no-owner --no-acl --file="$partial"
pg_restore --list "$partial" >/dev/null
mv "$partial" "$target"
trap - EXIT HUP INT TERM
sha256sum "$target" >"$target.sha256"
find "$BACKUP_DIR" -type f \( -name 'stockroom-*.dump' -o -name 'stockroom-*.dump.sha256' \) -mtime "+$RETENTION_DAYS" -delete
printf '%s\n' "$target"
