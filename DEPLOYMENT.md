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
