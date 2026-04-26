#!/bin/sh
# Daily backup scheduler. Runs cq-backup once per day at
# BACKUP_HOUR_UTC (default 03:00 UTC). To trigger an ad-hoc
# backup without waiting:  docker exec cq-backup cq-backup

set -eu

: "${BACKUP_HOUR_UTC:=3}"

echo "[cq-backup-scheduler] entrypoint start; daily run at ${BACKUP_HOUR_UTC}:00 UTC"

while true; do
  H=$(date -u +%H | sed 's/^0//')
  M=$(date -u +%M | sed 's/^0//')
  S=$(date -u +%S | sed 's/^0//')
  H=${H:-0}; M=${M:-0}; S=${S:-0}
  SOD=$((H*3600 + M*60 + S))
  TGT=$((BACKUP_HOUR_UTC*3600))
  if [ "$SOD" -lt "$TGT" ]; then
    WAIT=$((TGT - SOD))
  else
    WAIT=$((86400 - SOD + TGT))
  fi
  echo "[cq-backup-scheduler] sleeping ${WAIT}s until ${BACKUP_HOUR_UTC}:00 UTC"
  sleep "$WAIT"

  echo "[cq-backup-scheduler] triggering backup"
  /usr/local/bin/cq-backup || echo "[cq-backup-scheduler] run failed; continuing"
done
