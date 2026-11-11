#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 backups/ticketdrop-YYYYMMDDTHHMMSSZ.dump" >&2
  exit 2
fi

backup="$1"
test -f "$backup"

echo "This replaces the ${POSTGRES_DB:-ticketdrop} database. Type RESTORE to continue:"
read -r confirmation
test "$confirmation" = "RESTORE"

docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres dropdb \
  --username "${POSTGRES_USER:-ticketdrop}" \
  --if-exists "${POSTGRES_DB:-ticketdrop}"
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres createdb \
  --username "${POSTGRES_USER:-ticketdrop}" \
  "${POSTGRES_DB:-ticketdrop}"
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres pg_restore \
  --username "${POSTGRES_USER:-ticketdrop}" \
  --dbname "${POSTGRES_DB:-ticketdrop}" \
  --clean --if-exists \
  < "$backup"
