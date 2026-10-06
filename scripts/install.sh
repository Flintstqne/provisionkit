#!/usr/bin/env bash
# Install ProvisionKit on a controller with one command. Safe to run again.
#
#   curl -fsSLO https://raw.githubusercontent.com/Flintstqne/provisionkit/main/scripts/install.sh
#   less install.sh        # read it first
#   bash install.sh
#
# Options:
#   --dir DIR          where the checkout lives (default ~/provisionkit). Ignored when run from inside a checkout.
#   --admin NAME       the panel's first admin account (default: admin). A password is generated and shown once.
#   --admin-key KEY    public key for the admin account on managed nodes (default: first key in ~/.ssh/authorized_keys)
#   --address IP       this controller's address (default: detected)
#   --port N           panel port (default 8080)
#   --no-service       set up everything except the systemd service (no sudo needed, start it with: python -m panel run)
#   --yes              do not ask before installing system packages
#   --check            only report what is missing, change nothing
#   --repo URL         clone from here instead of GitHub
# Run as the normal controller user. The script uses sudo itself for system packages and the service.
set -euo pipefail

REPO_URL="https://github.com/Flintstqne/provisionkit.git"
DIR="$HOME/provisionkit"
ADMIN=admin
ADMIN_KEY=
ADDRESS=
PORT=8080
SERVICE=1
YES=0
CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift 2 ;;
    --admin) ADMIN="${2:?--admin needs a name}"; shift 2 ;;
    --admin-key) ADMIN_KEY="${2:?--admin-key needs a public key}"; shift 2 ;;
    --address) ADDRESS="${2:?--address needs an IP}"; shift 2 ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --repo) REPO_URL="${2:?--repo needs a URL or path}"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --yes | -y) YES=1; shift ;;
    --check) CHECK=1; shift ;;
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "Error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
case "$PORT" in '' | *[!0-9]*) die "the port must be a number." ;; esac
case "$ADMIN" in *[!A-Za-z0-9._-]* | '') die "the admin name may contain letters, digits, dot, dash and underscore." ;; esac
[ "$(id -u)" != 0 ] || die "run this as your normal user, not root. It asks for sudo when it needs it."

# ---- 1. prerequisites ---------------------------------------------------------------------------------------------
step "Checking prerequisites"
missing=()
need() { command -v "$1" > /dev/null 2>&1 || missing+=("$2"); }
need git git
need ssh-keygen openssh-client
need curl curl
if command -v python3 > /dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || missing+=("python3 (3.11 or newer)")
  python3 -c 'import venv, ensurepip' 2> /dev/null || missing+=(python3-venv)
else
  missing+=(python3 python3-venv)
fi
[ "$SERVICE" = 0 ] || [ "$CHECK" = 1 ] || command -v sudo > /dev/null 2>&1 || missing+=(sudo)
[ "$SERVICE" = 0 ] || command -v systemctl > /dev/null 2>&1 || echo "Note: no systemd here, so use --no-service."

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Missing: ${missing[*]}"
  [ "$CHECK" = 0 ] || exit 1
  command -v apt-get > /dev/null 2>&1 || die "install those with your package manager, then run this again."
  if [ "$YES" = 0 ]; then
    read -rp "Install them with apt (uses sudo)? [y/N] " ans
    [ "$ans" = y ] || [ "$ans" = Y ] || die "stopped. Install: ${missing[*]}"
  fi
  [ -z "${PROVISIONKIT_INSTALL_RETRY:-}" ] || die "still missing after installing packages: ${missing[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${missing[@]%% *}"
  PROVISIONKIT_INSTALL_RETRY=1 exec bash "${BASH_SOURCE[0]:-$0}" "$@"  # check again from the top
fi
echo "Everything needed is installed."
[ "$CHECK" = 0 ] || { echo "Ready to install."; exit 0; }

# ---- 2. checkout --------------------------------------------------------------------------------------------------
step "Checkout"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2> /dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -d "$HERE/.git" ] && [ -f "$HERE/panel/__main__.py" ]; then
  REPO="$HERE"
  echo "Using $REPO"
elif [ -d "$DIR/.git" ] && [ -f "$DIR/panel/__main__.py" ]; then
  REPO="$DIR"
  echo "Using the existing checkout $REPO"
else
  [ ! -e "$DIR" ] || [ -z "$(ls -A "$DIR" 2> /dev/null)" ] || die "$DIR exists and is not a ProvisionKit checkout."
  git clone --quiet "$REPO_URL" "$DIR"
  REPO="$(cd "$DIR" && pwd)"
  echo "Cloned to $REPO"
fi
cd "$REPO"

# ---- 3. Python environment ----------------------------------------------------------------------------------------
step "Python environment"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r panel/requirements.txt "ansible-core<2.20"
echo "Ready in $REPO/.venv"

# ---- 4. deploy key ------------------------------------------------------------------------------------------------
step "Deploy key"
KEY="$HOME/.ssh/provisionkit_ed25519"
install -d -m 0700 "$HOME/.ssh"
if [ -f "$KEY" ]; then
  echo "$KEY exists, leaving it alone."
else
  # No passphrase: Ansible runs from the panel and from timers, so nobody is there to type one.
  ssh-keygen -q -t ed25519 -N '' -C provisionkit-deploy -f "$KEY"
  echo "Created $KEY (no passphrase, mode 600). It opens every node you add, so protect this account."
fi

# ---- 5. inventory -------------------------------------------------------------------------------------------------
step "Inventory"
if [ -d inventories/local ]; then
  echo "inventories/local exists, leaving it alone."
else
  args=(--auto --deploy-key "$KEY")
  [ -z "$ADMIN_KEY" ] || args+=(--admin-key "$ADMIN_KEY")
  [ -z "$ADDRESS" ] || args+=(--address "$ADDRESS")
  .venv/bin/python -m panel init-inventory "${args[@]}"
fi
if problems="$(.venv/bin/python scripts/validate_inventory.py inventories/local 2>&1)"; then
  echo "The inventory is valid."
else
  echo "The inventory needs attention before you apply a baseline:"
  echo "$problems" | sed 's/^/  /'
  echo "Fix it in inventories/local (or pass --address / --admin-key), then run: provisionkit check"
fi

# ---- 6. admin account and the service -------------------------------------------------------------------------------
users="$(.venv/bin/python -c "
import sqlite3, pathlib
p = pathlib.Path('panel/instance/panel.db')
print(sqlite3.connect(p).execute('select count(*) from users').fetchone()[0] if p.exists() else 0)" 2> /dev/null || echo 0)"
PWFILE=
PASSWORD=
if [ "$users" = 0 ]; then
  PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
  PWFILE="$(mktemp)"
  trap 'rm -f "$PWFILE"' EXIT
  chmod 600 "$PWFILE"
  printf '%s\n' "$PASSWORD" > "$PWFILE"
fi

if [ "$SERVICE" = 1 ]; then
  step "Panel service (asks for your sudo password)"
  svc=(--user "$USER" --port "$PORT")
  if [ -n "$PWFILE" ]; then svc+=(--admin "$ADMIN" --admin-password-file "$PWFILE"); else svc+=(--skip-admin); fi
  sudo "$REPO/scripts/install_panel.sh" "${svc[@]}"
else
  step "Admin account"
  if [ -n "$PWFILE" ]; then
    .venv/bin/python -m panel create-user "$ADMIN" --role admin --password-file "$PWFILE"
  else
    echo "$users user(s) already exist."
  fi
  step "Command line tool"
  python3 scripts/provisionkit version | sed 's/^/version: /'
fi

# ---- 7. what next ---------------------------------------------------------------------------------------------------
ADDR="$(.venv/bin/python -c 'from panel.inventory import detect_address; print(detect_address() or "THIS-CONTROLLER")')"
echo
echo "Installed."
if [ -n "$PASSWORD" ]; then
  echo "  Sign in as: $ADMIN"
  echo "  Password:   $PASSWORD     (shown once, change it by creating a new admin in Settings)"
fi
if [ "$SERVICE" = 1 ]; then
  echo "  Panel:      running on 127.0.0.1:$PORT. From your computer: ssh -L $PORT:127.0.0.1:$PORT $USER@$ADDR"
  echo "              then open http://127.0.0.1:$PORT"
else
  echo "  Start it:   cd $REPO && .venv/bin/python -m panel run   (http://127.0.0.1:$PORT)"
fi
echo "  Next:       Devices, Add device. The checklist gives the one command to bootstrap each server."
echo "  Later:      provisionkit update    (pulls the latest main and checks everything)"
echo "  Remote use: docs/cloudflare-tunnel.md"
