#!/bin/sh
set -eu

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL is verplicht}"
backup=${1:?Gebruik: restore-postgres.sh /pad/backup.dump}
test -r "$backup"
if [ -f "$backup.sha256" ]; then
  sha256sum -c "$backup.sha256"
fi
pg_restore --dbname="$RESTORE_DATABASE_URL" --clean --if-exists --no-owner --no-acl --single-transaction "$backup"
