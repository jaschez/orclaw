# Deployment — Oracle Cloud (Always Free)

Despliegue de la engine en un VPS ARM Ampere del Always Free tier de Oracle Cloud. ~45-60 min de setup la primera vez (Oracle Cloud es más liosa que Hetzner pero gratis para siempre).

## Por qué Oracle Cloud

- **Always Free tier**: 4 OCPU ARM Ampere A1 + 24 GB RAM gratis para siempre (overkill para la engine)
- **Object Storage**: 10 GB gratis, perfecto para backups SQLite (<1 MB comprimidos)
- **Networking**: 10 TB/mes de tráfico saliente gratis
- **Caveats conocidos**:
  - Conseguir la Ampere A1 puede requerir varios intentos (alta demanda en algunas regiones)
  - Consola más liosa que Hetzner — requiere setup de VCN, subnet, security list
  - Oracle dice que pueden reclamar instancias "idle" pero en la práctica raro si la usas

## Provisioning

### 1. Cuenta Oracle Cloud

1. Crear cuenta en https://www.oracle.com/cloud/free/
2. Verificar email + tarjeta (no cobran nada, es verificación de identidad)
3. Elegir home region — Frankfurt (eu-frankfurt-1) o Amsterdam (eu-amsterdam-1) para latencia EU
4. Esperar 5-10 min a que la cuenta se active

### 2. Crear la VM Ampere A1

1. **Compute → Instances → Create instance**
2. Configuración:
   - Name: `orclaw`
   - Image: **Canonical Ubuntu 24.04** (Always Free eligible, ARM64)
   - Shape: `VM.Standard.A1.Flex` con **4 OCPU + 24 GB RAM** (todo el free tier de Ampere)
   - Si dice "Out of capacity": cambia región o reintenta. A veces hay que insistir varias veces a horas distintas
3. **Networking**: deja la VCN por defecto que crea Oracle automáticamente (`vcn-<timestamp>`)
4. **SSH key**: sube tu clave pública (`~/.ssh/id_ed25519.pub` o equivalente)
5. **Storage**: 50 GB boot volume (50 es el mínimo, gratis hasta 200 GB total)
6. Click **Create**. Espera ~2 min a que termine de provisionar

Apunta la **IP pública** que aparece en la página de la instancia.

### 3. Abrir puerto SSH en la security list

Oracle por defecto sólo permite SSH desde tu IP en el momento de crear la VM. Si tu IP cambia (DHCP doméstico), ajustar:

1. **Networking → Virtual Cloud Networks → tu VCN → Security Lists → Default Security List**
2. **Ingress Rules → Add ingress rule**:
   - Source CIDR: `0.0.0.0/0` (o tu IP fija si la tienes)
   - IP Protocol: TCP
   - Destination port: 22
3. Save

Si quieres acceso al dashboard vía IP directa (no recomendado, no hay TLS), también abrirías 8080. Mejor SSH tunnel — ver sección Dashboard más abajo.

### 4. Conectar

```bash
ssh ubuntu@<ip>
```

Oracle usa `ubuntu` como user por defecto en la imagen Ubuntu.

### 5. Hardening base

```bash
sudo apt update && sudo apt upgrade -y

# Crear user dedicado (no root, no ubuntu para producción)
sudo adduser engine --gecos "" --disabled-password
sudo usermod -aG sudo engine
sudo mkdir -p /home/engine/.ssh
sudo cp ~/.ssh/authorized_keys /home/engine/.ssh/
sudo chown -R engine:engine /home/engine/.ssh
sudo chmod 700 /home/engine/.ssh
sudo chmod 600 /home/engine/.ssh/authorized_keys

# SSH solo por key, no root
sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# Firewall iptables (Oracle Ubuntu trae iptables, no ufw por defecto)
# La security list de Oracle ya filtra a nivel cloud, esto es defense-in-depth
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw --force enable

# Unattended security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

exit
# Reconecta como engine, no como ubuntu
ssh engine@<ip>
```

### 6. Dependencias base

```bash
sudo apt install -y \
  python3.11 python3.11-venv python3-pip \
  sqlite3 \
  git \
  curl \
  jq

# gh CLI (oficial)
sudo mkdir -p -m 755 /etc/apt/keyrings
out=$(mktemp); wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg
sudo install -m 644 $out /etc/apt/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install -y gh

# OCI CLI (para Object Storage backups)
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
# Acepta las opciones por defecto. Después: oci setup config (necesitará tu OCI user OCID, tenancy OCID, key)

# Verifica
python3.11 --version  # >= 3.11
sqlite3 --version
gh --version
oci --version
```

### 7. Clonar la engine

```bash
sudo mkdir -p /opt/orclaw
sudo chown engine:engine /opt/orclaw

# Auth gh con un PAT (scope: repo, project, workflow)
gh auth login --with-token < /path/to/token.txt
# Alternativa interactiva: gh auth login

git clone https://github.com/${GITHUB_USERNAME}/orclaw.git /opt/orclaw
cd /opt/orclaw
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 8. Mirror de `${TARGET_REPO}`

```bash
sudo mkdir -p /var/lib/orclaw
sudo chown engine:engine /var/lib/orclaw
cd /var/lib/orclaw
git clone --depth 50 https://github.com/${TARGET_REPO}.git ${TARGET_REPO}-mirror
cd ${TARGET_REPO}-mirror
git config core.fsmonitor false
```

### 9. Secrets

```bash
sudo mkdir -p /etc/orclaw
sudo chmod 750 /etc/orclaw
sudo nano /etc/orclaw/secrets.env
```

Contenido (no necesitas API key de Anthropic — recordatorio):

```env
# El server SOLO necesita un PAT de GitHub que impersone al CEO/CTO.
# Claude lo ejecuta claude.yml en ${TARGET_REPO} vía OAuth Pro (ya configurado).
GITHUB_TOKEN=ghp_...
GITHUB_REPO=${TARGET_REPO}

# Notificaciones opcionales (outbound webhooks, sin dominio necesario)
SLACK_WEBHOOK_URL=                              # opcional
HEALTHCHECKS_URL=                               # opcional, ping al iniciar y cada N min
HEALTHCHECKS_BACKUP_URL=                        # opcional, ping en backup script

# Oracle Object Storage para backups (alternativa a B2/S3)
OCI_OBJECT_STORAGE_NAMESPACE=                   # de oci os ns get
OCI_OBJECT_STORAGE_BUCKET=orclaw-backups  # nombre del bucket
```

```bash
sudo chmod 640 /etc/orclaw/secrets.env
sudo chown root:engine /etc/orclaw/secrets.env
```

El PAT en `GITHUB_TOKEN` debe ser classic con scopes: `repo`, `project`, `workflow`. Generarlo en https://github.com/settings/tokens. Expiración recomendada: 90 días.

### 10. SQLite DB

```bash
mkdir -p /var/lib/orclaw/data
sqlite3 /var/lib/orclaw/data/engine.db < /opt/orclaw/orchestrator/state/schema.sql
```

### 11. Object Storage bucket en Oracle

Desde la consola de Oracle Cloud:

1. **Storage → Buckets → Create Bucket**
2. Name: `orclaw-backups`
3. Storage tier: `Standard`
4. Encryption: managed by Oracle (default)
5. Resto por defecto

Apunta el namespace que sale (lo verás también con `oci os ns get` tras configurar OCI CLI).

## Systemd units

Idénticas a las del plan original (no dependen del provider):

- `/etc/systemd/system/orclaw-orchestrator.service` (always-on)
- `/etc/systemd/system/orclaw-batch-planner.{service,timer}` (cada 10 min)
- `/etc/systemd/system/orclaw-mirror-sync.{service,timer}` (cada 5 min)
- `/etc/systemd/system/orclaw-backup.{service,timer}` (diario 04:00 UTC)

Las unit files viven en `/opt/orclaw/infra/systemd/`. Copialas:

```bash
sudo cp /opt/orclaw/infra/systemd/*.service /etc/systemd/system/
sudo cp /opt/orclaw/infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  orclaw-orchestrator.service \
  orclaw-batch-planner.timer \
  orclaw-mirror-sync.timer \
  orclaw-backup.timer

sudo systemctl status orclaw-orchestrator
sudo systemctl list-timers | grep orclaw
```

## Dashboard de estado — Cloudflare Tunnel + Zero Trust

Acceso al dashboard desde PC y móvil vía **`orclaw.<YOUR_TEAM>.com`**, protegido por Cloudflare Zero Trust (auth por email OTP / Google). El server NO abre ningún puerto público — el daemon `cloudflared` hace conexión saliente a Cloudflare y CF redirige tráfico autenticado.

### Pre-requisitos

- Dominio `<YOUR_TEAM>.com` con nameservers en Cloudflare (DNS gestionado por CF)
- Cuenta Cloudflare con Zero Trust activado (free tier hasta 50 usuarios — sobra)

### Setup del tunnel

#### 1. En Cloudflare Zero Trust dashboard

1. https://one.dash.cloudflare.com → Zero Trust dashboard
2. **Networks → Tunnels → Create a tunnel**
3. Connector type: `Cloudflared`
4. Nombre: `orclaw`
5. Save. Te da un **tunnel token** largo (`eyJhIjoi...`). Cópialo, lo usaremos en el server.

#### 2. En el server (Oracle Cloud)

Instalar `cloudflared`:

```bash
# ARM64 (Ampere A1)
curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb

# Verifica
cloudflared --version
```

Configura como systemd service con el token:

```bash
# Sustituye TOKEN_AQUI por el que copiaste arriba
sudo cloudflared service install TOKEN_AQUI

# Verifica
sudo systemctl status cloudflared
```

`cloudflared` ya conecta con Cloudflare. Vuelve a la dashboard de Zero Trust: el tunnel debería aparecer como `HEALTHY`.

#### 3. Public hostname

En la misma pantalla del tunnel:

1. **Public Hostname → Add a public hostname**
2. Subdomain: `orclaw`
3. Domain: `<YOUR_TEAM>.com`
4. Service: `HTTP` `localhost:8080`
5. Save

CF crea el DNS automáticamente. `https://orclaw.<YOUR_TEAM>.com` ya apunta al tunnel (no responde aún porque falta el Access policy).

#### 4. Access policy (Zero Trust auth)

1. **Access → Applications → Add an application**
2. Type: `Self-hosted`
3. Application name: `Orclaw Dashboard`
4. Application domain: `orclaw.<YOUR_TEAM>.com`
5. Session duration: 24 hours (o lo que prefieras)
6. **Policies → Add a policy**:
   - Name: `Only me`
   - Action: `Allow`
   - Include → Selector `Emails` → tu email (`<YOUR_EMAIL>` u otro)
7. Identity providers: deja `One-time PIN` activado (email OTP). Opcional: añade Google.
8. Save.

#### 5. Test

Visita `https://orclaw.<YOUR_TEAM>.com` en tu navegador. Te redirige a Cloudflare Access:

1. Pide tu email
2. Te manda un código OTP
3. Lo introduces
4. Sesión válida 24h, ves el dashboard

Funciona igual desde móvil. **Cero puertos abiertos en Oracle**. **Cero auth en el server**.

### Acceso alternativo (cuando CF caiga o sea más fácil)

Si necesitas debug rápido sin pasar por CF:

```bash
# SSH tunnel desde tu portátil
ssh -L 8080:localhost:8080 engine@<ip>
# Abre http://localhost:8080 en el navegador local
```

O CLI puro desde el server: `orclaw status`.

### Si pierdes acceso al tunnel

```bash
# Logs
sudo journalctl -u cloudflared -f

# Reinstalar
sudo systemctl restart cloudflared
```

Si el token caduca o se rota, vuelve a generar uno en CF dashboard y `sudo cloudflared service install NEW_TOKEN`.

## Acceso al dashboard — otros modos (fallback)

### Modo A — CLI desde SSH (siempre disponible)

Cero infra HTTP. El orchestrator expone los datos vía un CLI `orclaw status`:

```bash
ssh engine@<ip>
orclaw status

┌──────────────────────────────────────────────────────────┐
│ Orclaw · Status                                    │
│ Uptime: 12d 4h · Last batch: 8 min ago                   │
├──────────────────────────────────────────────────────────┤
│ Active in flight (cap 2):  1                             │
│ Quota observation:         🟢 healthy (0 fails in 10m)   │
│ Mentions last 5h:          18 (15 success, 1 fail)       │
│ Mentions last 24h:         88 (76 success, 89% rate)     │
│                                                          │
│ Current batch (layer 3):                                 │
│   #142 cookie banner    [implementer · queued 32s]       │
│                                                          │
│ Next layer (waiting):                                    │
│   #143 RGPD form  · #145 dashboard KPIs                  │
│                                                          │
│ OPS pending (your hand):                                 │
│   #129 procurar texto legal                              │
│   #130 Sentry signup                                     │
└──────────────────────────────────────────────────────────┘
```

Otros comandos del CLI:

```bash
orclaw status                    # vista de arriba
orclaw status --watch             # refresca cada 5s
orclaw runs list --limit 20      # últimos runs de @claude
orclaw quota show                 # detalle de quota observada
orclaw specialist                 # entra en modo specialist (ver más abajo)
orclaw pause                      # pausa orquestador
orclaw resume
```

### Modo B — SSH tunnel (debug local)

Si CF se cae o quieres bypass para debugging:

```bash
ssh -L 8080:localhost:8080 engine@<ip>
# Abre http://localhost:8080 en navegador local
```

## Integraciones externas (sin dominio)

Todas son **outbound only**, no necesitas IP pública con TLS:

| Integración | Para qué | Cómo |
|---|---|---|
| **Slack/Discord webhook** | Alertas de saturación, hard-stops, errores | URL de webhook → `SLACK_WEBHOOK_URL` env var |
| **Healthchecks.io** | Detectar si el orchestrator deja de pingar (server caído) | URL de check → `HEALTHCHECKS_URL` env var |
| **Email** | Reports diarios opcionales | SMTP via Resend / Mailgun / Gmail App Password |
| **Telegram** | Notif al móvil sin app extra | Bot token + chat ID |

Cero superficie expuesta. Cero TLS que mantener. Cero DNS que configurar.

## Backup con Oracle Object Storage

`/opt/orclaw/infra/scripts/backup.sh` (versión Oracle):

```bash
#!/usr/bin/env bash
set -euo pipefail

DB=/var/lib/orclaw/data/engine.db
STAMP=$(date -u +%Y%m%d-%H%M%S)
TMP=/tmp/engine-backup-$STAMP.sqlite
GZIP=$TMP.gz

# Hot backup (safe mientras orchestrator escribe)
sqlite3 "$DB" ".backup '$TMP'"
gzip -9 "$TMP"

# Upload a Oracle Object Storage
oci os object put \
  --namespace-name "$OCI_OBJECT_STORAGE_NAMESPACE" \
  --bucket-name "$OCI_OBJECT_STORAGE_BUCKET" \
  --name "engine-db/$(date -u +%Y/%m)/engine-backup-$STAMP.sqlite.gz" \
  --file "$GZIP" \
  --content-encoding gzip \
  >/dev/null

# Retain local 7 días
find /tmp/engine-backup-*.sqlite.gz -mtime +7 -delete 2>/dev/null || true

# Health ping (opcional)
[ -n "${HEALTHCHECKS_BACKUP_URL:-}" ] && curl -fsS -m 10 --retry 3 "$HEALTHCHECKS_BACKUP_URL" >/dev/null || true

echo "Backup uploaded: engine-db/$(date -u +%Y/%m)/engine-backup-$STAMP.sqlite.gz"
```

Setup del OCI CLI (una vez, sigue las instrucciones interactivas):

```bash
oci setup config
# Te pedirá: user OCID, tenancy OCID, region, generar nuevo API key pair
# El user OCID lo sacas de la consola: Profile → User Settings → OCID
# El tenancy OCID: Profile → Tenancy → OCID

# Tras setup, sube la public key generada a tu user en la consola Oracle:
# Profile → User Settings → Add API Key → Paste public key

# Verifica
oci os ns get
```

## Logs y diagnóstico

```bash
# Live tail del orchestrator
sudo journalctl -u orclaw-orchestrator -f

# Última hora de todos los orclaw-*
sudo journalctl --since "1 hour ago" -u 'orclaw-*'

# Estado actual
sudo systemctl status 'orclaw-*'

# Inspect SQLite
sqlite3 /var/lib/orclaw/data/engine.db
> .tables
> SELECT * FROM batches WHERE status='in_progress';
> SELECT agent, status, COUNT(*) FROM runs
  WHERE started_at >= date('now', 'start of day')
  GROUP BY agent, status;
```

## Update workflow

Sin downtime relevante (orchestrator restart de 5 s):

```bash
ssh engine@<ip>
cd /opt/orclaw
git pull
source .venv/bin/activate
pip install -r requirements.txt   # si hay deps nuevas
sudo systemctl restart orclaw-orchestrator
```

## Coste mensual

| Item | Coste |
|---|---|
| Oracle Cloud Always Free (VM Ampere A1 4 OCPU 24 GB) | **0 €** |
| Oracle Cloud Object Storage (10 GB free) | **0 €** |
| Networking (10 TB salida free) | **0 €** |
| Anthropic API tokens | **0 €** (Pro plan ya pagado, sin extras) |
| **Total mensual** | **0 €** |

## Cuándo migrar fuera de Oracle Free

Si en algún momento:

- La Ampere A1 se reclama por idle (raro pero ocurre)
- Necesitas más recursos (>24 GB RAM, >4 OCPU)
- Te cansas de la consola de Oracle

→ Migrar a Hetzner CX22 (~5 €/mes) es trivial: re-correr este documento contra una VM de Hetzner. La engine es portable.

## Disaster recovery

Si la VM explota o Oracle la reclama:

1. Crear nueva instancia Ampere A1 (5-15 min)
2. Re-correr este documento desde paso 4 (~30 min)
3. Restaurar `engine.db` desde el último backup en Object Storage (1 min):
   ```bash
   oci os object get --namespace-name "$NS" --bucket-name "$BUCKET" \
     --name "engine-db/2026/05/engine-backup-XXX.sqlite.gz" --file /tmp/restore.gz
   gunzip /tmp/restore.gz
   mv /tmp/restore /var/lib/orclaw/data/engine.db
   ```
4. Restart services (1 min)
5. Engine recoge donde estaba. Lo único perdido: runs en vuelo al momento de la caída (próximo planner los reclasifica como `pending`).
