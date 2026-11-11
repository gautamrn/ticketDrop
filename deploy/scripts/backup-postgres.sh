#!/bin/sh
set -eu

destination="${BACKUP_DIR:-./backups}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$destination"

docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T postgres pg_dump \
  --username "${POSTGRES_USER:-ticketdrop}" \
  --dbname "${POSTGRES_DB:-ticketdrop}" \
  --format=custom \
  > "$destination/ticketdrop-$timestamp.dump"

find "$destination" -type f -name 'ticketdrop-*.dump' -mtime "+$retention_days" -delete
echo "wrote $destination/ticketdrop-$timestamp.dump"
