#!/usr/bin/env bash
# Install ProvisionKit Control Center on the controller as a systemd service. Safe to run again.
#   sudo scripts/install_panel.sh [--user NAME] [--port 8080]
#   scripts/install_panel.sh --print-unit        # show the systemd unit it would install, change nothing
#   sudo scripts/install_panel.sh --no-systemd   # everything except the service (used for testing)
# --user is the controller account that holds the deploy key and known_hosts. Default: the user who ran sudo.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-}"
PORT=8080
PRINT_UNIT=0
NO_SYSTEMD=0
SKIP_USER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER="${2:?--user needs a name}"; shift 2 ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --print-unit) PRINT_UNIT=1; shift ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --skip-admin) SKIP_USER=1; shift ;;
    -h | --help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$PORT" in '' | *[!0-9]*) echo "Port must be a number." >&2; exit 2 ;; esac

home_of() { getent passwd "$1" | cut -d: -f6 || true; }

render_unit() {
  local home_dir
  home_dir="$(home_of "$RUN_USER")"
  sed -e "s|@RUN_USER@|$RUN_USER|g" -e "s|@REPO@|$REPO|g" -e "s|@PORT@|$PORT|g" \
    -e "s|@HOME_DIR@|${home_dir:-/home/$RUN_USER}|g" \
    "$REPO/panel/deploy/provisionkit-panel.service"
}

if [ "$PRINT_UNIT" = 1 ]; then
  RUN_USER="${RUN_USER:-controller-user}"
  render_unit
  exit 0
fi

die() { echo "Error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[ "$(id -u)" = 0 ] || die "run with sudo."
[ -n "$RUN_USER" ] && [ "$RUN_USER" != root ] || die "pass --user NAME (the controller account with the deploy key, not root)."
id "$RUN_USER" > /dev/null 2>&1 || die "no such user: $RUN_USER"
[ "$(stat -c %U "$REPO")" = "$RUN_USER" ] || die "$REPO is owned by $(stat -c %U "$REPO"), not $RUN_USER. Clone the repo as $RUN_USER."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11 or newer is required."
python3 -c 'import venv, ensurepip' 2> /dev/null || die "install python3-venv first: apt install python3-venv"

as_user() { sudo -u "$RUN_USER" -H "$@"; }
in_repo() { as_user bash -c "cd '$REPO' && $*"; }

step "Python environment ($REPO/.venv)"
[ -x "$REPO/.venv/bin/python" ] || as_user python3 -m venv "$REPO/.venv"
as_user "$REPO/.venv/bin/pip" install --quiet --upgrade pip
as_user "$REPO/.venv/bin/pip" install --quiet -r "$REPO/panel/requirements.txt" "ansible-core<2.20"

step "Inventory"
if [ -d "$REPO/inventories/local" ]; then
  echo "inventories/local exists, leaving it alone."
else
  in_repo ".venv/bin/python -m panel init-inventory"
  echo "Edit inventories/local before collecting: it still holds the documentation addresses and example keys."
fi

step "Data directory"
as_user install -d -m 0700 "$REPO/panel/instance"
# The service sees the home directory read-only, except ~/.ansible, which must exist to be made writable.
as_user install -d -m 0700 "$(home_of "$RUN_USER")/.ansible" "$(home_of "$RUN_USER")/.ssh"
# known_hosts must exist to be made writable for the service; the panel only appends to it.
as_user bash -c 'touch "$HOME/.ssh/known_hosts" && chmod 600 "$HOME/.ssh/known_hosts"'

if [ "$SKIP_USER" = 0 ]; then
  step "Admin account"
  count="$(in_repo ".venv/bin/python -c \"
import sqlite3, pathlib
p = pathlib.Path('panel/instance/panel.db')
print(sqlite3.connect(p).execute('select count(*) from users').fetchone()[0] if p.exists() else 0)\"" 2> /dev/null || echo 0)"
  if [ "$count" = 0 ]; then
    read -rp "Admin username: " admin
    in_repo ".venv/bin/python -m panel create-user '$admin' --role admin"
  else
    echo "$count user(s) already exist."
  fi
fi

step "Command line tool"
python3 "$REPO/scripts/provisionkit" install
echo "Update later with: provisionkit update"

if [ "$NO_SYSTEMD" = 1 ]; then
  step "Skipped the service (--no-systemd). Unit that would be installed:"
  render_unit
  exit 0
fi

step "Settings file (/etc/provisionkit-panel.env)"
if [ -e /etc/provisionkit-panel.env ]; then
  echo "Exists, leaving it alone."
else
  install -m 0640 -o root -g "$RUN_USER" /dev/null /etc/provisionkit-panel.env
  cat > /etc/provisionkit-panel.env << 'ENV'
# Everything is off for the first run over an SSH tunnel to http://127.0.0.1:8080.
# Turn these on when you put the panel behind HTTPS or a Cloudflare tunnel (docs/cloudflare-tunnel.md).
#PANEL_SECURE_COOKIE=1
#PANEL_ALLOWED_HOSTS=panel.example.org
#PANEL_TRUST_CF_IP=1
#PANEL_CF_ACCESS_TEAM=your-team-name
#PANEL_CF_ACCESS_AUD=application-audience-tag
ENV
fi

step "systemd service"
render_unit > /etc/systemd/system/provisionkit-panel.service
systemctl daemon-reload
systemctl enable provisionkit-panel.service
systemctl restart provisionkit-panel.service

for _ in $(seq 1 20); do
  # Any HTTP answer means the panel is up. With PANEL_ALLOWED_HOSTS set it answers 400 to a bare 127.0.0.1 Host header.
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)"
  if [ -n "$code" ] && [ "$code" != 000 ]; then
    echo
    echo "The panel is running on 127.0.0.1:$PORT (loopback only)."
    echo "From your own computer: ssh -L $PORT:127.0.0.1:$PORT $RUN_USER@<controller address>"
    echo "then open http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
done
journalctl -u provisionkit-panel.service -n 30 --no-pager || true
die "the panel did not answer on port $PORT within 20 seconds. Log above."
