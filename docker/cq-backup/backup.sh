#!/bin/sh
# Daily Postgres backup → GCS dual-region bucket.
# Audits every run to the backup_runs table so the dashboard
# can surface freshness and operators can audit DR posture.

set -eu

: "${POSTGRES_HOST:?missing}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_DB:?missing}"
: "${POSTGRES_USER:?missing}"
: "${POSTGRES_PASSWORD:?missing}"
: "${GCS_BACKUP_BUCKET:?missing}"
: "${GCS_KEY_FILE:?missing}"

TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
DUMP_FILE="/tmp/cq-backup-${TS}.dump"
GCS_OBJECT="daily/cq-backup-${TS}.dump"
START_EPOCH=$(date -u +%s)

PSQL="psql -h ${POSTGRES_HOST} -p ${POSTGRES_PORT} -U ${POSTGRES_USER} -d ${POSTGRES_DB} -v ON_ERROR_STOP=1 -t -A"

log() { echo "[cq-backup ${TS}] $*"; }

audit_update() {
  status="$1"
  extra_sql="${2:-}"
  PGPASSWORD="${POSTGRES_PASSWORD}" $PSQL -c "
    UPDATE backup_runs
    SET status='${status}',
        completed_at=now(),
        duration_seconds=$(( $(date -u +%s) - START_EPOCH ))
        ${extra_sql}
    WHERE id=${RUN_ID};
  " >/dev/null
}

cleanup() {
  rc=$?
  rm -f "${DUMP_FILE}"
  if [ -n "${RUN_ID:-}" ] && [ "${BACKUP_DONE:-0}" = "0" ]; then
    audit_update "failure" ", error_message='interrupted (rc=${rc})'"
  fi
}
trap cleanup EXIT INT TERM

log "starting"

gcloud auth activate-service-account --key-file="${GCS_KEY_FILE}" --quiet >/dev/null 2>&1 || {
  log "ERROR: gcloud auth failed (key file unreadable or invalid)"
  exit 1
}

RUN_ID=$(PGPASSWORD="${POSTGRES_PASSWORD}" $PSQL -c \
  "INSERT INTO backup_runs (status) VALUES ('running') RETURNING id;" | head -n1 | tr -d '[:space:]')

if [ -z "${RUN_ID}" ]; then
  log "ERROR: failed to insert audit row"
  exit 1
fi
log "audit row id=${RUN_ID}"

if ! PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
  -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DB}" \
  --format=custom --compress=9 --file="${DUMP_FILE}" 2>/tmp/pg_dump.err; then
  err=$(head -c 500 /tmp/pg_dump.err | tr "'" '`')
  audit_update "failure" ", error_message='pg_dump failed: ${err}'"
  log "ERROR: pg_dump failed: ${err}"
  exit 1
fi

DUMP_BYTES=$(wc -c < "${DUMP_FILE}" | tr -d ' ')
log "pg_dump complete: ${DUMP_BYTES} bytes"

if ! gcloud storage cp "${DUMP_FILE}" "gs://${GCS_BACKUP_BUCKET}/${GCS_OBJECT}" --quiet 2>/tmp/gcs.err; then
  err=$(head -c 500 /tmp/gcs.err | tr "'" '`')
  audit_update "failure" ", error_message='gcs upload failed: ${err}'"
  log "ERROR: gcs upload failed: ${err}"
  exit 1
fi

audit_update "success" ", gcs_object='${GCS_OBJECT}', size_bytes=${DUMP_BYTES}"
BACKUP_DONE=1
log "success: ${DUMP_BYTES} bytes -> gs://${GCS_BACKUP_BUCKET}/${GCS_OBJECT}"
