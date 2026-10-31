#!/bin/sh
# Run the app locally. Usage:
#   ./dev.sh        real mode: connect your car in Settings
#   ./dev.sh sim    simulated car, no Volvo account needed
set -e
cd "$(dirname "$0")"

if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -e 'backend[dev]'
fi
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi
(cd frontend && npm run build)

if [ "$1" = "sim" ]; then
  export VOLVODASH_SIM="${VOLVODASH_SIM:-ex40}" VOLVODASH_NO_AUTH=1
fi
export VOLVODASH_DATA="${VOLVODASH_DATA:-$PWD/data}"
echo "Volvo Dash on http://localhost:${PORT:-8000}"
exec backend/.venv/bin/python -m volvodash
