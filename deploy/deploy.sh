#!/usr/bin/env bash
# Runs ON the VPS (invoked over SSH by .github/workflows/deploy.yml).
# Usage: deploy.sh <image-tag>
set -euo pipefail
TAG="${1:?image tag required}"
cd "$(dirname "$0")"
export ARGUS_IMAGE_TAG="$TAG"
set -a; . ./.env; set +a

compose() { docker compose -f docker-compose.prod.yml "$@"; }

compose pull --quiet
compose up -d postgres redis
compose --profile migrate run --rm migrate
compose up -d --remove-orphans
docker image prune -f >/dev/null
echo "deployed $TAG"
