#!/bin/sh
# Prepare a clean clone for the backend suite against a disposable PostgreSQL 18 on 127.0.0.1:55432 (CI service or a local
# server). Writes .env (superuser password, from $POSTGRES_PASSWORD) and creates restricted roles including lv_authn with separate synthetic credentials.
# Never run against a database you care about.
set -eu
cd "$(dirname "$0")/.."
: "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD to the disposable server's superuser password}"
umask 077
[ -f .env ] || printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD" > .env
python -m tools.dev_db prepare
