#!/usr/bin/env bash
# Prepares a machine to be shut down by ups_monitor: a locked-down user, the monitor's public key,
# and sudo rules for exactly one command (plus its --dry-run form, used by the readiness check). Run as root ON THE TARGET MACHINE.
#
#   sudo ./setup-target.sh --pubkey "$(cat ups_shutdown.pub)" --from 10.10.1.50
#
# --pubkey  contents of keys/ups_shutdown.pub from the monitor host
# --from    IP address(es) of the monitor host, comma separated; the key then works only from there
# --user    account to create (default upsshutdown, must match shutdown.machines[].user)
# --dry-run print what would be done, change nothing (no root needed)
set -euo pipefail

USER_NAME=upsshutdown
PUBKEY=""
FROM=""
DRY=0

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --pubkey) [[ $# -ge 2 ]] || usage; PUBKEY=$2; shift 2 ;;
        --from) [[ $# -ge 2 ]] || usage; FROM=$2; shift 2 ;;
        --user) [[ $# -ge 2 ]] || usage; USER_NAME=$2; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) usage ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ -n $PUBKEY ]] || die "--pubkey is required"
[[ $PUBKEY =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]] || die "--pubkey must be a single ssh-ed25519 public key line"
[[ -z $FROM || $FROM =~ ^[0-9A-Fa-f:.,]+$ ]] || die "--from must be IP addresses separated by commas"
[[ $USER_NAME =~ ^[a-z_][a-z0-9_-]*$ && $USER_NAME != root ]] || die "bad --user: $USER_NAME"

KEY_LINE="restrict${FROM:+,from=\"$FROM\"} $PUBKEY"
SUDOERS_FILE=/etc/sudoers.d/ups-shutdown
SUDOERS_LINE="$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff"
# Lets the daily readiness check run the same command without powering off.
SUDOERS_DRY_LINE="$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --dry-run"

if ((DRY)); then
    echo "Would create user $USER_NAME (no password) if missing"
    echo "Would write to ~$USER_NAME/.ssh/authorized_keys:"
    echo "  $KEY_LINE"
    echo "Would write to $SUDOERS_FILE:"
    echo "  $SUDOERS_LINE"
    echo "  $SUDOERS_DRY_LINE"
    exit 0
fi

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v visudo >/dev/null || die "visudo not found; install sudo first"
SYSTEMCTL=$(readlink -f "$(command -v systemctl)")
[[ $SYSTEMCTL == /usr/bin/systemctl ]] \
    || echo "warning: systemctl is at $SYSTEMCTL; set a custom 'command:' for this machine in config.yaml" >&2

if ! id "$USER_NAME" >/dev/null 2>&1; then
    if command -v adduser >/dev/null; then
        adduser --disabled-password --gecos "" "$USER_NAME"
    else
        useradd --create-home --shell /bin/bash "$USER_NAME"
    fi
    echo "Created user $USER_NAME"
fi

HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
SSH_DIR=$HOME_DIR/.ssh
install -d -m 700 -o "$USER_NAME" -g "$(id -gn "$USER_NAME")" "$SSH_DIR"
AUTH=$SSH_DIR/authorized_keys
touch "$AUTH"
# Replace any earlier line for the same key so re-running updates --from instead of duplicating.
key_body=${PUBKEY#ssh-ed25519 }
key_body=${key_body%% *}
grep -vF "$key_body" "$AUTH" > "$AUTH.new" || true
echo "$KEY_LINE" >> "$AUTH.new"
mv "$AUTH.new" "$AUTH"
chown "$USER_NAME:$(id -gn "$USER_NAME")" "$AUTH"
chmod 600 "$AUTH"
echo "Installed key in $AUTH"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
printf '%s\n%s\n' "$SUDOERS_LINE" "$SUDOERS_DRY_LINE" > "$tmp"
visudo -cf "$tmp" >/dev/null || die "generated sudoers rule failed validation"
install -m 0440 -o root -g root "$tmp" "$SUDOERS_FILE"
echo "Installed $SUDOERS_FILE"

if ! systemctl is-active --quiet ssh 2>/dev/null && ! systemctl is-active --quiet sshd 2>/dev/null; then
    echo "warning: the ssh server does not look active; install/enable openssh-server" >&2
fi

cat <<EOF

Target ready. Remaining checks, from the monitor host:
  .venv/bin/python -m app check --trust-new-hosts   # first time only, pins this machine's host key
  .venv/bin/python -m app check
The machine must also answer ping: the monitor uses it to confirm the machine has shut down.
EOF
