#!/usr/bin/env sh
set -eu

MYSQL_HOST="${MYSQL_HOST:-localhost}"
MYSQL_PORT="${MYSQL_PORT:-3306}"

echo "Waiting for database connection at ${MYSQL_HOST}:${MYSQL_PORT} ..."
# Add a timeout (-w 2) to avoid hanging indefinitely
until nc -z -w 2 "$MYSQL_HOST" "$MYSQL_PORT"; do
  sleep 1
done
echo "Database connected."

echo "Running alembic..."
uv run --no-sync g0v0-migrate upgrade-all

# Hand over control to the final command
exec "$@"
