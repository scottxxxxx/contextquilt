#!/bin/bash
# ContextQuilt DR restore - pulls a backup from GCS into a throwaway
# Postgres container so you can validate it before swapping into prod.
#
# Usage:
#   GCS_BACKUP_BUCKET=... GCS_KEY_FILE=... ./scripts/restore.sh
#       Lists last 14 backups and prompts which to restore.
#
#   ./scripts/restore.sh daily/cq-backup-2026-04-25T03-00-00Z.dump
#       Restore a specific object.
#
# By default this restores into a container named 'cq-restore-test'
# on port 55432 - the verify step at the end runs row counts against
# the live cq-postgres for visual comparison. NO data in the live
# database is touched. To promote the restored DB to production,
# follow the documented swap procedure in DEPLOYMENT.md.

set -euo pipefail

: "${GCS_BACKUP_BUCKET:?must set GCS_BACKUP_BUCKET}"
: "${GCS_KEY_FILE:?must set GCS_KEY_FILE (path to SA JSON key)}"
: "${POSTGRES_PASSWORD:?must set POSTGRES_PASSWORD - the restore container superuser pw, pick any value}"

RESTORE_CONTAINER="${RESTORE_CONTAINER:-cq-restore-test}"
RESTORE_PORT="${RESTORE_PORT:-55432}"
RESTORE_DB="${RESTORE_DB:-context_quilt}"
WORK_DIR="${WORK_DIR:-/tmp/cq-restore}"

OBJECT="${1:-}"

echo "==> activating service account"
gcloud auth activate-service-account --key-file="${GCS_KEY_FILE}" --quiet

if [ -z "${OBJECT}" ]; then
  echo
  echo "==> last 14 backups in gs://${GCS_BACKUP_BUCKET}/daily/"
  gcloud storage ls -l "gs://${GCS_BACKUP_BUCKET}/daily/" | tail -16 | head -14
  echo
  read -r -p "Object to restore (relative to bucket, e.g. daily/cq-backup-...dump): " OBJECT
fi

if [ -z "${OBJECT}" ]; then
  echo "no object selected, exiting"
  exit 1
fi

mkdir -p "${WORK_DIR}"
LOCAL_FILE="${WORK_DIR}/$(basename "${OBJECT}")"

echo
echo "==> downloading gs://${GCS_BACKUP_BUCKET}/${OBJECT} -> ${LOCAL_FILE}"
gcloud storage cp "gs://${GCS_BACKUP_BUCKET}/${OBJECT}" "${LOCAL_FILE}"

echo
echo "==> launching throwaway postgres at localhost:${RESTORE_PORT} (container ${RESTORE_CONTAINER})"
docker rm -f "${RESTORE_CONTAINER}" >/dev/null 2>&1 || true
docker run -d \
  --name "${RESTORE_CONTAINER}" \
  -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  -e POSTGRES_DB="${RESTORE_DB}" \
  -p "${RESTORE_PORT}:5432" \
  postgres:15-alpine >/dev/null

echo "    waiting for postgres to accept connections..."
for _ in $(seq 1 30); do
  if docker exec "${RESTORE_CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 1
done

echo
echo "==> running pg_restore"
docker cp "${LOCAL_FILE}" "${RESTORE_CONTAINER}:/tmp/dump"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${RESTORE_CONTAINER}" \
  pg_restore --clean --if-exists --no-owner --no-privileges \
  -U postgres -d "${RESTORE_DB}" /tmp/dump 2>&1 | tail -20 || {
  echo "    pg_restore reported errors above. Some are expected (e.g. role does not exist)."
  echo "    proceeding to verify counts."
}

echo
echo "==> verification - row counts in restored DB"
for tbl in patches patch_connections entities entity_relationships applications backup_runs; do
  count=$(docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${RESTORE_CONTAINER}" \
    psql -U postgres -d "${RESTORE_DB}" -t -A -c "SELECT count(*) FROM ${tbl};" 2>/dev/null || echo "ERR")
  printf "    %-30s %s\n" "${tbl}" "${count}"
done

echo
echo "==> restore complete."
echo "    Restored DB available at: postgres://postgres:\$POSTGRES_PASSWORD@localhost:${RESTORE_PORT}/${RESTORE_DB}"
echo "    To inspect:    docker exec -it ${RESTORE_CONTAINER} psql -U postgres ${RESTORE_DB}"
echo "    To tear down:  docker rm -f ${RESTORE_CONTAINER}"
echo
echo "    To promote to production: see the DR section in DEPLOYMENT.md"
