#!/bin/sh
set -eu

mkdir -p /data
chown -R stockroom:stockroom /data

exec su-exec stockroom "$@"

