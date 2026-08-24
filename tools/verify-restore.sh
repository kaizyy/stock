#!/bin/sh
set -eu

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL moet naar een aparte verificatiedatabase wijzen}"
backup=${1:?Gebruik: verify-restore.sh /pad/backup.dump}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/restore-postgres.sh" "$backup"
psql "$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -Atc "SELECT CASE WHEN to_regclass('public.users') IS NOT NULL AND to_regclass('public.stockrooms') IS NOT NULL AND to_regclass('public.memberships') IS NOT NULL AND to_regclass('public.sessions') IS NOT NULL THEN 'restore-ok' ELSE 'restore-invalid' END" | grep -qx restore-ok
psql "$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -Atc "SELECT count(*) >= 0 FROM users" | grep -qx t
echo restore-verification-ok
