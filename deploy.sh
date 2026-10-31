#!/bin/sh
# Ship the current code to the server and rebuild the container.
# Data stays on the server in ~/docker/volvo-dash/data and is never overwritten.
set -e
cd "$(dirname "$0")"
# Where to deploy: set DEPLOY_HOST (user@host) and optionally DEPLOY_DIR, either
# in the environment or in a git-ignored deploy.env next to this script.
[ -f deploy.env ] && . ./deploy.env
HOST="${DEPLOY_HOST:?Set DEPLOY_HOST=user@host, e.g. in deploy.env}"
DIR="${DEPLOY_DIR:-docker/volvo-dash}"

rsync -az --delete \
  --exclude node_modules --exclude .venv --exclude dist --exclude 'data*' \
  --exclude __pycache__ --exclude '*.egg-info' --exclude .pytest_cache \
  --exclude .git --exclude deploy.env \
  ./ "$HOST:$DIR/"
ssh "$HOST" "cd $DIR && docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build && docker image prune -f >/dev/null"
echo "Deployed to http://${HOST#*@}:8000"
