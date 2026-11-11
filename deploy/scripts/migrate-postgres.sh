#!/bin/sh
set -eu

compose() {
  docker compose --env-file .env.production -f docker-compose.prod.yml "$@"
}

db_user="${POSTGRES_USER:-ticketdrop}"
db_name="${POSTGRES_DB:-ticketdrop}"

compose exec -T postgres psql \
  --username "$db_user" \
  --dbname "$db_name" \
  --set ON_ERROR_STOP=1 \
  --command "CREATE TABLE IF NOT EXISTS schema_migrations (
      version text PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT now()
  )"

for migration in db/migrations/*.sql; do
  version="$(basename "$migration" .sql)"
  applied="$(
    compose exec -T postgres psql \
      --username "$db_user" \
      --dbname "$db_name" \
      --tuples-only --no-align \
      --set ON_ERROR_STOP=1 \
      --command "SELECT 1 FROM schema_migrations WHERE version = '$version'"
  )"
  if [ "$applied" = "1" ]; then
    echo "already applied $version"
    continue
  fi

  {
    printf 'BEGIN;\n'
    cat "$migration"
    printf "\nINSERT INTO schema_migrations (version) VALUES ('%s');\nCOMMIT;\n" "$version"
  } | compose exec -T postgres psql \
    --username "$db_user" \
    --dbname "$db_name" \
    --set ON_ERROR_STOP=1
  echo "applied $version"
done
