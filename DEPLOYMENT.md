# Context Quilt Deployment Guide

## Overview
You are deploying `ContextQuilt` to a Google Cloud VM to utilize hardware acceleration for the `qwen2.5-coder` model. Local CPU emulation on macOS is insufficient for performance.

## Recommended Infrastructure (L4 GPU)
We utilize the **NVIDIA L4 GPU** (via the `g2-standard-4` machine type). This offers:
- **High Performance:** ~2x faster than T4, capable of 100+ tokens/sec.
- **Modern Architecture:** Ada Lovelace architecture, optimized for AI inference.
- **Cost:** ~$0.75/hr (or ~$0.23/hr for Spot instances).

---

## 1. Create a GPU-enabled VM
Run this command in your local terminal (requires `gcloud` installed):

```bash
gcloud compute instances create context-quilt-gpu \
    --project=contextquilt-dev-01 \
    --zone=us-central1-a \
    --machine-type=g2-standard-4 \
    --accelerator=type=nvidia-l4,count=1 \
    --maintenance-policy=TERMINATE \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=200GB \
    --metadata="install-nvidia-driver=True"
```

*Note: If `us-central1-a` is full (ZONE_RESOURCE_POOL_EXHAUSTED), try `us-east4-a`.*

## 2. Install Dependencies on the VM
SSH into the machine (`gcloud compute ssh context-quilt-gpu`) and run this setup script:

```bash
# 1. Install Docker & Nvidia Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y git docker.io docker-compose nvidia-container-toolkit

# 2. Config Docker to use GPU (Critical Step)
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 3. Add yourself to docker group (so you don't need sudo)
sudo usermod -aG docker $USER
newgrp docker

# 4. (Optional) Manual Driver Install if the auto-installer fails
# sudo apt-get install -y nvidia-driver-535 nvidia-utils-535
```

## 3. Deployment & Development Workflow

### Option A: VS Code Remote (Recommended)
This allows you to edit files on your Mac but run them on the Cloud GPU instantly.

1.  **Configure local SSH:**
    ```bash
    gcloud compute config-ssh --project=contextquilt-dev-01
    ```
2.  **Connect:** Open VS Code -> `Remote-SSH: Connect to Host...` -> Select `context-quilt-gpu`.
3.  **Run:** Open the terminal in VS Code (which is now remote) and run:
    ```bash
    cd contextquilt
    docker-compose up -d
    ```
4.  **View:** Use the **PORTS** tab in VS Code to forward port `8000` (FastAPI) or `8501` (Streamlit) to your local machine.

### Option B: Manual Upload
You can use `SFTP` (Forklift/Transmit) or `scp` to move files.

```bash
gcloud compute scp --recurse . context-quilt-gpu:~/contextquilt --project=contextquilt-dev-01
```

## 4. Cost Management (Important)

**To Stop Billing (Pause):**
Stops compute costs (~$0.75/hr), but you still pay for disk storage (~$8/mo).
```bash
gcloud compute instances stop context-quilt-gpu
```

**To Delete (Reset):**
Stops ALL costs, but deletes your data/code.
```bash
gcloud compute instances delete context-quilt-gpu
```

---

# Backup & Disaster Recovery

This section is the operator runbook for production data protection. Service-level commitment:
- **RPO ≤ 24 hours** — at most one day of recently-written memories may be lost in a disaster.
- **RTO ≤ 1 hour** — service is fully restored within an hour of declaring an incident.

## Architecture

The `cq-backup` sidecar runs on the prod VM alongside the main stack. Once per day at `BACKUP_HOUR_UTC` (default 03:00 UTC), it:

1. Inserts a row into `backup_runs` with status `running`.
2. Runs `pg_dump` against `cq-postgres` in custom (compressed) format.
3. Uploads the dump to `gs://${GCS_BACKUP_BUCKET}/daily/cq-backup-<UTC-timestamp>.dump`.
4. Updates the `backup_runs` row to `success` (with size/object) or `failure` (with error).

The bucket is **dual-region GCS** with versioning, a 30-day retention policy, and a lifecycle rule that deletes objects older than 35 days. Redis is **not** backed up — it's a cache and rebuilds from Postgres via the pre-warm path.

The admin dashboard's *System Health* tab surfaces the latest backup status, freshness, and a 30-day run history.

## One-time provisioning (operator)

These commands assume your private operator runbook defines `${GCP_PROJECT_ID}`, `${GCS_BACKUP_BUCKET}`, `${PROD_VM_IP}`, and `${PROD_SSH_USER}`. Run them only on the first deploy.

### 1. Create the backup bucket (dual-region, versioned, retention-locked)

```bash
gcloud storage buckets create "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" \
  --location=nam4 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention

gcloud storage buckets update "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" --versioning

# Lifecycle: delete live objects after 35d, prune old non-current versions after 7d
cat > /tmp/lifecycle.json <<'EOF'
{
  "lifecycle": {
    "rule": [
      {"action": {"type": "Delete"}, "condition": {"age": 35, "isLive": true}},
      {"action": {"type": "Delete"}, "condition": {"numNewerVersions": 1, "daysSinceNoncurrentTime": 7}}
    ]
  }
}
EOF
gcloud storage buckets update "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" --lifecycle-file=/tmp/lifecycle.json

# 30-day retention policy. UNLOCKED initially — lock only after the
# first restore drill validates everything works end-to-end. Once
# locked, retention cannot be removed or shortened.
gcloud storage buckets update "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" --retention-period=30d
```

### 2. Create the backup service account (bucket-scoped only)

```bash
gcloud iam service-accounts create cq-backup-sa \
  --project="${GCP_PROJECT_ID}" \
  --display-name="ContextQuilt Backup SA"

# IAM scoped to the bucket — NOT to the project
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" \
  --member="serviceAccount:cq-backup-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Generate a JSON key (handle this file like a password)
gcloud iam service-accounts keys create ~/.cq-backup-sa.json \
  --iam-account="cq-backup-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --project="${GCP_PROJECT_ID}"
chmod 400 ~/.cq-backup-sa.json
```

### 3. Place the key on the prod VM

```bash
ssh "${PROD_SSH_USER}@${PROD_VM_IP}" 'sudo mkdir -p /opt/contextquilt/secrets && sudo chmod 700 /opt/contextquilt/secrets'
scp ~/.cq-backup-sa.json "${PROD_SSH_USER}@${PROD_VM_IP}:/tmp/cq-backup-sa.json"
ssh "${PROD_SSH_USER}@${PROD_VM_IP}" 'sudo mv /tmp/cq-backup-sa.json /opt/contextquilt/secrets/cq-backup-sa.json && sudo chmod 400 /opt/contextquilt/secrets/cq-backup-sa.json && sudo chown root:root /opt/contextquilt/secrets/cq-backup-sa.json'
shred -u ~/.cq-backup-sa.json   # remove from local disk
```

### 4. Update `.env.prod` on the VM

Add (or update) on the VM at `/opt/contextquilt/.env.prod`:

```
GCS_BACKUP_BUCKET=<your bucket name>
BACKUP_HOUR_UTC=3
```

### 5. Apply migration 16 and bring up the sidecar

```bash
ssh "${PROD_SSH_USER}@${PROD_VM_IP}"
cd /opt/contextquilt
sudo git fetch origin main && sudo git reset --hard origin/main

# Apply backup_runs migration
sudo docker exec -i cq-postgres psql -U postgres -d context_quilt -v ON_ERROR_STOP=1 \
  < init-db/16_backup_runs.sql

# Build + start the cq-backup sidecar (no impact on running services)
sudo docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build cq-backup

# Verify it's healthy
sudo docker ps --filter name=cq-backup
sudo docker logs cq-backup --tail 20
```

### 6. Trigger an ad-hoc backup to validate

Don't wait until 03:00 UTC. Run one immediately:

```bash
sudo docker exec cq-backup /usr/local/bin/cq-backup
```

Expected output ends with `success: <bytes> -> gs://<bucket>/daily/cq-backup-<ts>.dump`. The admin dashboard's System Health tab should now show **Backup & DR: Healthy**.

### 7. Run the restore drill (CRITICAL — do not skip)

An untested backup is not a backup. Validate end-to-end:

```bash
# On any machine with docker + gcloud
export GCS_BACKUP_BUCKET=<your bucket>
export GCS_KEY_FILE=~/.cq-backup-sa.json   # operator's local copy (re-generate via step 2)
export POSTGRES_PASSWORD=throwaway-pw-just-for-restore-test

./scripts/restore.sh
# Pick the most recent object from the listed backups
```

The script downloads the dump, launches a throwaway Postgres on `localhost:55432`, restores into it, and prints row counts for the key tables (`patches`, `entities`, `applications`, etc.). Compare against the live DB:

```bash
ssh "${PROD_SSH_USER}@${PROD_VM_IP}" \
  'sudo docker exec cq-postgres psql -U postgres -d context_quilt -c "SELECT (SELECT COUNT(*) FROM context_patches) AS patches, (SELECT COUNT(*) FROM applications) AS apps;"'
```

Counts should match (give or take any rows written between the dump and the comparison).

When done:
```bash
docker rm -f cq-restore-test
```

### 8. Lock the retention policy (after drill succeeds)

```bash
gcloud storage buckets update "gs://${GCS_BACKUP_BUCKET}" \
  --project="${GCP_PROJECT_ID}" \
  --lock-retention-period
```

⚠️ **This is irreversible.** Once locked, retention cannot be removed or shortened. The 30-day window means an attacker with project-owner access cannot delete recent backups.

## Daily operations

Most days you do nothing — the sidecar runs at 03:00 UTC, the dashboard reflects status. Check in:

- **Daily / on-call rotation:** glance at *System Health → Backup & DR* on the dashboard. Status should be `Healthy`.
- **Weekly:** scan the run history. Investigate any `failure` rows.
- **Monthly:** run a fresh restore drill against a recent backup to confirm restorability hasn't regressed.

### Investigating a failed backup

```bash
ssh "${PROD_SSH_USER}@${PROD_VM_IP}"

# Recent log
sudo docker logs cq-backup --tail 100

# What does the audit table say?
sudo docker exec cq-postgres psql -U postgres -d context_quilt -c \
  "SELECT id, started_at, completed_at, status, size_bytes, error_message FROM backup_runs ORDER BY started_at DESC LIMIT 10;"

# Force a re-run
sudo docker exec cq-backup /usr/local/bin/cq-backup
```

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `gcloud auth failed (key file unreadable)` | Bind mount missing or chmod wrong | Confirm `/opt/contextquilt/secrets/cq-backup-sa.json` exists, is `chmod 400`, owned by root |
| `pg_dump failed: ... server version mismatch` | Postgres image upgraded past the backup image's `pg_dump` version | Bump `postgres:15-alpine` → matching version in `docker/cq-backup/Dockerfile` |
| `gcs upload failed: ... 403 Forbidden` | SA lost IAM binding | Re-run the IAM grant from step 2 |
| `gcs upload failed: ... resumable upload aborted` | Transient network issue | Re-run with `docker exec cq-backup cq-backup`. If persistent, check VM egress |

## Disaster recovery — promote a restored DB

If `cq-postgres` data is lost or corrupted:

1. **Stop incoming writes:**
   ```bash
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod stop context-quilt context-quilt-worker
   ```
2. **Move the corrupt volume aside (don't delete — for forensics):**
   ```bash
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod stop cq-postgres
   sudo docker volume rename contextquilt_cq-postgres-data contextquilt_cq-postgres-data-corrupt-$(date +%s) || true
   ```
   *(If `docker volume rename` is unavailable, copy out the underlying directory under `/var/lib/docker/volumes/` instead.)*

3. **Restore the latest backup into a fresh `cq-postgres`:**
   ```bash
   # Bring up cq-postgres with an empty volume
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod up -d cq-postgres
   sleep 10

   # Pull the latest backup
   gcloud auth activate-service-account --key-file=/opt/contextquilt/secrets/cq-backup-sa.json
   LATEST=$(gcloud storage ls "gs://${GCS_BACKUP_BUCKET}/daily/" | sort | tail -1)
   gcloud storage cp "$LATEST" /tmp/restore.dump

   # Restore into the fresh container
   sudo docker cp /tmp/restore.dump cq-postgres:/tmp/restore.dump
   sudo docker exec -e PGPASSWORD="$(grep ^POSTGRES_PASSWORD .env.prod | cut -d= -f2)" cq-postgres \
     pg_restore --clean --if-exists --no-owner -U postgres -d context_quilt /tmp/restore.dump
   ```

4. **Re-apply any migrations newer than the dump** (defensive — they should be no-ops on a fresh restore but pinning to current main keeps schema in sync):
   ```bash
   for f in init-db/*.sql; do
     sudo docker exec -i cq-postgres psql -U postgres -d context_quilt -v ON_ERROR_STOP=1 < "$f"
   done
   ```

5. **Restart application services:**
   ```bash
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
   ```

6. **Verify** via the admin dashboard. Patch counts, user counts, and recent ingestion log should reflect the data as of the dump's timestamp.

7. **Post-incident:** file an entry under `docs/ops/incidents/` documenting cause, RPO actually realized, and any fix to prevent recurrence.
