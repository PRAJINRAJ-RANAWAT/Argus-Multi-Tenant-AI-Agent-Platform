# Registering the Argus GitHub App

This needs a browser and your GitHub account, so it's a manual step. Do it after the VPS and domain from `docs/deploy.md` work (the webhook URL must be reachable).

## 1. Create the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App** (or an organization's settings, if the org should own it).

| Field | Value |
|---|---|
| GitHub App name | `Argus Review` (must be globally unique; add a suffix if taken) |
| Homepage URL | `https://argus.<your-domain>` |
| Webhook → Active | ✅ |
| Webhook URL | `https://argus.<your-domain>/webhooks/github` |
| Webhook secret | output of `openssl rand -hex 32` (keep it for step 3) |
| Where can this app be installed? | **Any account** (so anyone can install it) |

**Repository permissions**

| Permission | Access | Why |
|---|---|---|
| Contents | Read-only | read files, diffs, `ARGUS.md`, indexing |
| Pull requests | Read & write | read PRs, post reviews and comments |
| Issues | Read & write | Q&A agent opens issues (after confirmation) |
| Checks | Read-only | CI status tool |
| Commit statuses | Read-only | CI status tool |
| Metadata | Read-only | mandatory |

**Subscribe to events:** `Pull request`, `Push`. (Installation events are always sent.)

Create the App. On its page:
1. Note the **App ID**.
2. **Generate a private key** → a `.pem` file downloads. Treat it like a password.

## 2. Install it on a test repo

App page → **Install App** → choose your account → **Only select repositories** → pick a test repo.

## 3. Configure the server

On the VPS, append to `/opt/argus/.env` (single quotes keep the `\n` escapes literal; Argus converts them):

```bash
cd /opt/argus
echo "ARGUS_GITHUB_APP_ID=<app id>" >> .env
echo "ARGUS_GITHUB_WEBHOOK_SECRET=<secret from step 1>" >> .env
echo "ARGUS_GITHUB_PRIVATE_KEY='$(awk 'NF {printf "%s\\n", $0}' /path/to/argus.private-key.pem)'" >> .env
# Until a real LLM key is configured, reviews come from the bundled mock LLM:
echo "ARGUS_LLM_BASE_URL=http://mock-llm:8000/v1" >> .env
chmod 600 .env
ARGUS_IMAGE_TAG=$(docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' $(docker ps -qf name=argus-api-1) 2>/dev/null) \
  docker compose -f docker-compose.prod.yml up -d   # or just re-run the Deploy workflow
```

(Simplest: after editing `.env`, re-run the **Deploy** workflow: `gh workflow run deploy.yml --ref main`.)

## 4. Real LLM (optional, costs money)

Any OpenAI-compatible endpoint works, for example:

```bash
ARGUS_LLM_BASE_URL=https://api.openai.com/v1
ARGUS_LLM_API_KEY=<key>
ARGUS_LLM_MODEL=<model id>
```

## 5. Verify

1. Open a PR in the test repo.
2. App page → **Advanced → Recent Deliveries** should show `202`.
3. Within seconds a comment titled "Argus review for `<sha>`" appears on the PR.
4. **Redeliver** the same delivery: it returns `200 {"status":"duplicate"}` and no second comment appears.
