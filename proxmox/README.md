# Proxmox / cloud-init template

This directory ships a portable cloud-init `user-data.yml` that boots a
fresh Ubuntu 24.04 cloud-image into a working Orclaw node — zero manual
post-install steps. It's documented for Proxmox here, but the same file
works on any cloud-init host (Hetzner, EC2, GCP, DigitalOcean,
OpenStack, libvirt…).

## What you get

A VM that, on first boot:

1. Installs OS deps.
2. Creates the `orclaw` system user.
3. Pulls this repo, sets up the venv.
4. Reads pre-seeded `/etc/orclaw/secrets.env` (you put your tokens there
   in `user-data.yml` before applying).
5. Installs + enables every systemd unit.
6. Starts the dashboard on `127.0.0.1:8766`.
7. Drops a friendly motd so future SSH sessions know what they're on.

Total wall time on a clean Ubuntu 24.04 cloud image: **~3–4 minutes**.

## Proxmox usage

Assumes you already have a cloud-init-ready Ubuntu 24.04 template (one of
the [`noble-server-cloudimg-amd64.img`](https://cloud-images.ubuntu.com/noble/)
images converted with `qm importdisk`). If not, see "Building the base
template" below.

```bash
# 1. Edit user-data.yml — fill in your SSH key + secrets.
$EDITOR proxmox/user-data.yml

# 2. Drop it in Proxmox's snippets storage (the storage must have
#    'Snippets' enabled — typically 'local').
cp proxmox/user-data.yml /var/lib/vz/snippets/orclaw-user-data.yml

# 3. Clone your base template into a new VM.
qm clone <TEMPLATE_VMID> <NEW_VMID> --name orclaw-01 --full

# 4. Wire the cloud-init user-data.
qm set <NEW_VMID> --cicustom "user=local:snippets/orclaw-user-data.yml"
qm set <NEW_VMID> --ipconfig0 ip=dhcp           # or static
qm set <NEW_VMID> --ciuser ubuntu               # bootstrap user
qm set <NEW_VMID> --serial0 socket --vga serial0

# 5. Boot.
qm start <NEW_VMID>

# 6. Watch it provision (3–4 min):
qm terminal <NEW_VMID>     # if you set serial0 above
# or:
ssh ubuntu@<vm-ip> sudo tail -f /var/log/orclaw-install.log
```

That's it. The dashboard is live on `127.0.0.1:8766` on the VM; front
it with a Cloudflare Tunnel + Access policy for remote access (full
recipe in [`../docs/dashboard.md`](../docs/dashboard.md)).

## Building the base template (one-time)

```bash
cd /var/lib/vz/template/iso
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

# Replace 9000 with whatever VMID you want for the template.
qm create 9000 --name ubuntu-2404-cloudinit --memory 2048 --cores 2 \
  --net0 virtio,bridge=vmbr0
qm importdisk 9000 noble-server-cloudimg-amd64.img local-lvm
qm set 9000 --scsihw virtio-scsi-pci --scsi0 local-lvm:vm-9000-disk-0
qm set 9000 --ide2 local-lvm:cloudinit
qm set 9000 --boot c --bootdisk scsi0
qm set 9000 --serial0 socket --vga serial0
qm resize 9000 scsi0 +18G    # 20 GB total; tune to your needs
qm template 9000             # mark as template
```

You now have VMID 9000 as a clonable template. Use it in step 3 above.

## Other hypervisors

`user-data.yml` is **cloud-init standard** — drop it into any host that
accepts user-data:

- **Hetzner Cloud**: paste it into the "User data" field when creating a
  server.
- **AWS EC2**: paste into "User data" in the Launch Instance wizard.
- **GCP**: `gcloud compute instances create ... --metadata-from-file user-data=user-data.yml`.
- **DigitalOcean**: `doctl compute droplet create ... --user-data-file user-data.yml`.
- **libvirt / virt-install**: `--cloud-init user-data=user-data.yml`.

## Sizing

Minimum sane spec:

| Resource | Min | Recommended |
|---|---|---|
| RAM | 1 GB | 2 GB |
| vCPU | 1 | 2 |
| Disk | 12 GB | 20 GB |

The orchestrator itself sips ~80 MB. RAM headroom is for the GitHub
Actions self-hosted runner (each concurrent `@claude` action peaks
around 600–900 MB during `npm` / `pytest` runs). Plan for
`runner_RAM ≈ 1 GB × max_in_flight`.

## What this doesn't do

The cloud-init bootstrap leaves three things to you, intentionally:

1. **Register the GitHub Actions self-hosted runner** in your target
   repo. Pro-plan billing depends on the runner being yours — the
   installer can't do this without you naming the repo + getting the
   short-lived registration token.
2. **Wire `cloudflared`** with your tunnel credentials. The installer
   *installs* the binary, but you still need to `cloudflared tunnel
   login` once with a browser, then run `cloudflared tunnel create
   orclaw` and add the DNS route.
3. **Add a Cloudflare Access policy** in front of the tunnel hostname.
   Recipe in [`../docs/dashboard.md`](../docs/dashboard.md).

Together these take ~10 minutes of point-and-click work in two browser
tabs. Could be scripted further; PRs welcome.
