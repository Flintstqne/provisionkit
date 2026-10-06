#!/usr/bin/env bash
# Try the Control Center with synthetic data. Needs python3 (3.11+) with venv. No sudo, no servers, no Ansible run.
#   bash scripts/demo.sh            then open http://127.0.0.1:8080 and sign in as demo / demo-password-123
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11 or newer is required." >&2; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r panel/requirements.txt
exec .venv/bin/python -m panel demo --port "${1:-8080}"
