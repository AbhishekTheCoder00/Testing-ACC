#!/usr/bin/env bash
# Deploy acc-connector to EC2 from GitHub Actions.
# Required env: SSH_PRIVATE_KEY, DEPLOY_HOST, DEPLOY_USER, PUBLIC_HOST
set -euo pipefail

: "${SSH_PRIVATE_KEY:?SSH_PRIVATE_KEY secret is missing or empty}"
: "${DEPLOY_HOST:?DEPLOY_HOST secret is missing or empty}"
: "${DEPLOY_USER:?DEPLOY_USER secret is missing or empty}"
: "${PUBLIC_HOST:?PUBLIC_HOST secret is missing or empty}"

# Accept host with or without scheme (https://example.com → example.com)
PUBLIC_HOST="${PUBLIC_HOST#https://}"
PUBLIC_HOST="${PUBLIC_HOST#http://}"
PUBLIC_HOST="${PUBLIC_HOST%/}"

SSH_KEY="$HOME/.ssh/deploy_key"
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"
REMOTE_APP="/opt/acc-connector/app"
SERVICE_USER="accconnector"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=yes)

echo "==> Setting up SSH for ${REMOTE}"
mkdir -p ~/.ssh
# Strip Windows CR if the key was pasted from a Windows machine
printf '%s\n' "$SSH_PRIVATE_KEY" | tr -d '\r' > "$SSH_KEY"
chmod 600 "$SSH_KEY"
ssh-keyscan -H "$DEPLOY_HOST" >> ~/.ssh/known_hosts 2>/dev/null

echo "==> Ensuring remote app directory exists"
ssh "${SSH_OPTS[@]}" "$REMOTE" "sudo mkdir -p ${REMOTE_APP}"

echo "==> Syncing files to ${REMOTE}:${REMOTE_APP}/"
# /opt/acc-connector/app is root-owned, so the deploy user cannot create rsync's
# temp files there (mkstemp "Permission denied"). Run rsync as root on the remote
# via passwordless sudo. Excluded files (.env, connector.db) keep their existing
# ownership so the service user (accconnector) can still read/write them.
# --chown ensures synced dirs/files are owned by the service user; without this
# SQLite cannot create journal files beside connector.db (OAuth writes fail).
rsync -avz --delete \
  -e "ssh -i ${SSH_KEY} -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=yes" \
  --rsync-path="sudo rsync" \
  --chown="${SERVICE_USER}:${SERVICE_USER}" \
  --exclude '.env' \
  --exclude 'connector.db' \
  --exclude '*.bak-*' \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  ./ "${REMOTE}:${REMOTE_APP}/"

echo "==> Fixing ownership for service user (follow app symlink if present)"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "APP_DIR=\$(readlink -f ${REMOTE_APP}); \
   sudo chown -R ${SERVICE_USER}:${SERVICE_USER} \"\${APP_DIR}\"; \
   sudo chown ${SERVICE_USER}:${SERVICE_USER} \"\${APP_DIR}/.env\" \"\${APP_DIR}/backend/repositories/connector.db\" 2>/dev/null || true"

echo "==> Installing Python dependencies on remote"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "sudo ${REMOTE_APP%/*}/venv/bin/pip install -r ${REMOTE_APP}/requirements.txt"

echo "==> Restarting acc-connector service"
ssh "${SSH_OPTS[@]}" "$REMOTE" 'sudo systemctl restart acc-connector'

echo "==> Verifying service is active"
if ! ssh "${SSH_OPTS[@]}" "$REMOTE" 'sudo systemctl is-active --quiet acc-connector'; then
  echo "ERROR: acc-connector service is not active after restart"
  ssh "${SSH_OPTS[@]}" "$REMOTE" \
    'sudo systemctl status acc-connector --no-pager || true; sudo journalctl -u acc-connector -n 30 --no-pager || true' || true
  exit 1
fi

HEALTH_URL="https://${PUBLIC_HOST}/health"
echo "==> Waiting for health check at ${HEALTH_URL}"
for attempt in $(seq 1 30); do
  if curl -sf "$HEALTH_URL"; then
    echo ""
    echo "Health check passed (attempt ${attempt})"
    exit 0
  fi
  echo "  attempt ${attempt}/30 — not ready yet, waiting 5s..."
  sleep 5
done

echo "ERROR: Health check failed after 30 attempts (${HEALTH_URL})"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  'sudo systemctl status acc-connector --no-pager || true; sudo journalctl -u acc-connector -n 30 --no-pager || true' || true
exit 1
