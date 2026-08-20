#!/usr/bin/env bash
# Dumps the main server's PostgreSQL database to a gzip-compressed file,
# named with a UTC timestamp, and optionally uploads it to S3-compatible
# storage. Meant to run on a schedule (cron/systemd timer), not by hand.
#
# Required env: DATABASE_URL (same value the server itself uses, e.g.
#   postgresql://user:pass@host:5432/dbname -- pg_dump wants the plain
#   postgresql:// scheme, not the +asyncpg one the app uses internally).
#
# Optional env, to also upload (needs the `aws` CLI configured/installed):
#   BACKUP_S3_BUCKET  -- e.g. s3://my-bucket/vpn-3x-backups
#
# Retention: deletes local dumps older than BACKUP_RETENTION_DAYS (default 7)
# -- this only prunes the LOCAL copy; if you upload to S3, manage retention
# there separately (e.g. a bucket lifecycle rule) so a bug here can't wipe
# your only remaining copy.
#
# Restore:
#   gunzip -c vpn3x_YYYYMMDDTHHMMSSZ.sql.gz | psql "$DATABASE_URL"
# Restoring does NOT stop the running server first -- do that yourself if
# restoring onto a live database, or point DATABASE_URL at a fresh one.

set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpn-3x}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$BACKUP_DIR/vpn3x_${timestamp}.sql.gz"

# pg_dump doesn't understand the +asyncpg driver suffix SQLAlchemy uses.
plain_database_url="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"

pg_dump "$plain_database_url" | gzip > "$dump_file"
echo "wrote $dump_file"

if [[ -n "${BACKUP_S3_BUCKET:-}" ]]; then
    aws s3 cp "$dump_file" "$BACKUP_S3_BUCKET/$(basename "$dump_file")"
    echo "uploaded to $BACKUP_S3_BUCKET"
fi

find "$BACKUP_DIR" -name 'vpn3x_*.sql.gz' -mtime "+${BACKUP_RETENTION_DAYS}" -delete
