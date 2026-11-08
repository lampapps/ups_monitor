#!/usr/bin/env bash
# First-run setup for ups_monitor. Idempotent: safe to re-run, never overwrites existing files.
# Usage: ./setup.sh [--dev]   (--dev also installs test dependencies)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
APP_DIR=$PWD

dev=0
case "${1:-}" in
    "") ;;
    --dev) dev=1 ;;
    *) echo "Usage: $0 [--dev]" >&2; exit 2 ;;
esac

# 1. Prerequisites
missing=()
command -v python3 >/dev/null || missing+=("python3")
python3 -c 'import venv, ensurepip' 2>/dev/null || missing+=("python3-venv")
command -v ssh >/dev/null || missing+=("openssh-client")
command -v ssh-keygen >/dev/null || missing+=("openssh-client")
command -v ping >/dev/null || missing+=("iputils-ping")
if ((${#missing[@]})); then
    echo "Missing prerequisites. Install with:" >&2
    echo "  sudo apt install $(printf '%s\n' "${missing[@]}" | sort -u | tr '\n' ' ')" >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    echo "Python 3.11 or newer is required (found $(python3 --version))." >&2
    exit 1
}

# 2. Virtual environment and dependencies
if [[ ! -x .venv/bin/python ]]; then
    echo "Creating .venv"
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
if ((dev)); then
    .venv/bin/python -m pip install --quiet -r requirements-dev.txt
fi

# 3. Directories
mkdir -p data keys
chmod 700 keys

# 4. Config files from examples, only if absent
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env (fill in secrets)"
fi
chmod 600 .env
if [[ ! -f config.yaml ]]; then
    cp config.example.yaml config.yaml
    echo "Created config.yaml (edit machines and thresholds)"
fi

# 5. SSH key used to shut down the other machines
if [[ ! -f keys/ups_shutdown ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "ups_monitor@$(hostname)" -f keys/ups_shutdown
    echo "Generated keys/ups_shutdown"
fi
chmod 600 keys/ups_shutdown
touch keys/known_hosts
chmod 600 keys/known_hosts

# 6. Keep secrets out of git
touch .gitignore
for entry in .env keys/ data/ .venv/ config.yaml; do
    grep -qxF "$entry" .gitignore || echo "$entry" >> .gitignore
done

# 7. Empty database
.venv/bin/python -m app initdb

cat <<EOF

Setup complete in $APP_DIR.

Next steps:
  1. Edit .env (secrets) and config.yaml (UPS, machines, thresholds).
  2. Prepare each target machine with deploy/setup-target.sh (see README.md); this is its public key:
       $(cat keys/ups_shutdown.pub)
  3. Validate the config:  .venv/bin/python -m app config
EOF
