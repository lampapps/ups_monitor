#!/usr/bin/env bash
# Installs the systemd units and the sudoers rule that lets the service user power off this host.
# Run after ./setup.sh, from any directory:  sudo deploy/install.sh [--user NAME] [--no-start]
# Preview without touching the system:       deploy/install.sh --render-only DIR
set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The app is run as "python -m app", which only works from the project root.
cd "$APP_DIR"
SERVICE_USER=${SUDO_USER:-}
START=1
RENDER_ONLY=""

usage() {
    echo "Usage: sudo $0 [--user NAME] [--no-start]   |   $0 --render-only DIR" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --user) [[ $# -ge 2 ]] || usage; SERVICE_USER=$2; shift 2 ;;
        --no-start) START=0; shift ;;
        --render-only) [[ $# -ge 2 ]] || usage; RENDER_ONLY=$2; shift 2 ;;
        *) usage ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ -n $SERVICE_USER ]] || die "cannot tell which user should run the service; pass --user NAME"
[[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "unusual user name: $SERVICE_USER"
[[ $SERVICE_USER != root ]] || die "do not run the service as root"
[[ $APP_DIR =~ ^[A-Za-z0-9_./-]+$ ]] || die "app directory path has characters this installer does not support: $APP_DIR"

render() {  # render SRC DEST
    sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" "$1" > "$2"
}

if [[ -n $RENDER_ONLY ]]; then
    mkdir -p "$RENDER_ONLY"
    for unit in ups-monitor ups-web; do
        render "$APP_DIR/deploy/$unit.service" "$RENDER_ONLY/$unit.service"
    done
    echo "Rendered units into $RENDER_ONLY"
    exit 0
fi

[[ $EUID -eq 0 ]] || die "run with sudo"
id "$SERVICE_USER" >/dev/null 2>&1 || die "user $SERVICE_USER does not exist"

# The service must already be runnable by that user: venv, config and secrets in place.
[[ -x $APP_DIR/.venv/bin/python ]] || die "no .venv found; run ./setup.sh first"
[[ -f $APP_DIR/config.yaml && -f $APP_DIR/.env ]] || die "config.yaml or .env missing; run ./setup.sh first"
# Files created under sudo end up root-owned and unreadable by the service user.
for entry in .env config.yaml keys data; do
    [[ -e $APP_DIR/$entry ]] && chown -R "$SERVICE_USER": "$APP_DIR/$entry"
done
runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m app config \
    || die "config check failed as $SERVICE_USER (fix config.yaml/.env, or file ownership)"

SYSTEMCTL=$(readlink -f "$(command -v systemctl)")
[[ $SYSTEMCTL == /usr/bin/systemctl ]] \
    || die "systemctl is $SYSTEMCTL but the app runs /usr/bin/systemctl; adjust POWEROFF_COMMAND in app/settings.py"

# Sudo rules: the poweroff command and its --dry-run form, validated before they are put in place.
SUDOERS=/etc/sudoers.d/ups-monitor
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n%s\n' \
    "$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" \
    "$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --dry-run" > "$tmp"
visudo -cf "$tmp" >/dev/null || die "generated sudoers rule failed validation"
install -m 0440 -o root -g root "$tmp" "$SUDOERS"
echo "Installed $SUDOERS"

for unit in ups-monitor ups-web; do
    render "$APP_DIR/deploy/$unit.service" "$tmp"
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/$unit.service"
    echo "Installed /etc/systemd/system/$unit.service"
done
systemctl daemon-reload

if ((START)); then
    systemctl enable --now ups-monitor.service ups-web.service
else
    systemctl enable ups-monitor.service ups-web.service
fi

WEB_PORT=$(cd "$APP_DIR" && runuser -u "$SERVICE_USER" -- .venv/bin/python -c \
    'from app.config import load_config; print(load_config(check_env=False).web.port)')

cat <<EOF

Done. Shutdown stays in dry-run until you change shutdown.dry_run in config.yaml.
  Status:  systemctl status ups-monitor ups-web
  Logs:    journalctl -u ups-monitor -f
  Web UI:  http://$(hostname):$WEB_PORT/
  Restart after editing config.yaml:  sudo systemctl restart ups-monitor ups-web
EOF
