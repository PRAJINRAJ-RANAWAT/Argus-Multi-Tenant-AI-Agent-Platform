# Deploying Argus (single VPS)

Push to `main` → GitHub Actions builds `ghcr.io/prajinraj-ranawat/argus:<sha>` → SSH to the VPS → `deploy/deploy.sh <sha>` (pull, migrate, `up -d`) → smoke test on `https://$ARGUS_DOMAIN/healthz`. Until the secrets below exist, the deploy job prints a warning and skips. See ADR 0002.

## One-time setup

### 1. VPS

Any Ubuntu 24.04 VPS with 2 vCPU / 4 GB RAM is enough for M0–M3 (e.g. Hetzner CX22, DigitalOcean basic 4 GB).

```bash
# as root on the VPS
curl -fsSL https://get.docker.com | sh
adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
mkdir -p /opt/argus && chown deploy:deploy /opt/argus
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
```

### 2. Deploy SSH key (on your laptop)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/argus_deploy -N "" -C "argus-deploy"
ssh-copy-id -i ~/.ssh/argus_deploy.pub deploy@<VPS_IP>   # or append to /home/deploy/.ssh/authorized_keys
ssh -i ~/.ssh/argus_deploy deploy@<VPS_IP> docker ps        # must work without sudo
```

### 3. Production `.env` on the VPS

```bash
ssh -i ~/.ssh/argus_deploy deploy@<VPS_IP>
cd /opt/argus
PW=$(openssl rand -hex 24)
cat > .env <<ENV
ARGUS_ENV=production
ARGUS_LOG_LEVEL=INFO
POSTGRES_USER=argus
POSTGRES_PASSWORD=$PW
POSTGRES_DB=argus
ARGUS_DATABASE_URL=postgresql://argus:$PW@postgres:5432/argus
ARGUS_REDIS_URL=redis://redis:6379/0
ARGUS_DOMAIN=argus.<your-domain>
ENV
chmod 600 .env
```

Later milestones add more keys here (GitHub App, Slack, LLM providers); `.env.example` in the repo lists them all.

### 4. DNS (Cloudflare)

1. Add an `A` record `argus` → `<VPS_IP>` with the proxy **off** (grey cloud) so Caddy can obtain its Let's Encrypt certificate.
2. After the first successful deploy (`https://argus.<your-domain>/healthz` works), switch the record to **proxied** (orange cloud) and set SSL/TLS mode to **Full (strict)**.

### 5. GitHub repository secrets and variables

```bash
gh secret set DEPLOY_HOST --body "<VPS_IP>"
gh secret set DEPLOY_USER --body "deploy"
gh secret set DEPLOY_SSH_KEY < ~/.ssh/argus_deploy
gh secret set DEPLOY_KNOWN_HOSTS --body "$(ssh-keyscan -t ed25519 <VPS_IP> 2>/dev/null)"
gh variable set ARGUS_DOMAIN --body "argus.<your-domain>"
```

Optionally restrict them to the `production` environment (Settings → Environments → production).

### 6. First deploy

```bash
gh workflow run deploy.yml --ref main
gh run watch
curl https://argus.<your-domain>/healthz
```

## Operations

- Logs: `ssh deploy@<VPS_IP> 'cd /opt/argus && ARGUS_IMAGE_TAG=x docker compose -f docker-compose.prod.yml logs -f --tail 100'`
- Roll back: `gh workflow run deploy.yml` on an older commit is not supported (it deploys the branch head); instead run `/opt/argus/deploy.sh <older-sha>` on the VPS. Migrations are forward-only, so they must stay backward compatible.
- Backups (until managed Postgres): add a nightly cron on the VPS:
  `0 3 * * * cd /opt/argus && docker compose -f docker-compose.prod.yml exec -T postgres pg_dump -U argus argus | gzip > /opt/argus/backups/argus-$(date +\%F).sql.gz`
