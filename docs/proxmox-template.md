# Proxmox template / cloud-init

The full template lives at [`../proxmox/`](../proxmox/). This page is
the quick pointer.

## TL;DR

```bash
# Edit the user-data with your secrets + SSH key.
$EDITOR proxmox/user-data.yml

# Drop into Proxmox's snippets dir.
cp proxmox/user-data.yml /var/lib/vz/snippets/orclaw-user-data.yml

# Clone your Ubuntu 24.04 cloud-init template into a new VM.
qm clone <TEMPLATE_VMID> <NEW_VMID> --name orclaw-01 --full
qm set <NEW_VMID> --cicustom "user=local:snippets/orclaw-user-data.yml"
qm set <NEW_VMID> --ipconfig0 ip=dhcp
qm set <NEW_VMID> --ciuser ubuntu
qm start <NEW_VMID>

# 3-4 minutes later:
ssh ubuntu@<vm-ip> systemctl status 'orclaw-*'
```

See [`../proxmox/README.md`](../proxmox/README.md) for the full guide,
including how to build the base template, sizing recommendations, and
adapting the same file for Hetzner / EC2 / GCP / DigitalOcean.
