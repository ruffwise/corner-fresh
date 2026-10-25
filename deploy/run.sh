#!/usr/bin/env bash
# Run one collector command by hand, as the corners user, with the secrets loaded.
#   sudo /opt/corners/app/deploy/run.sh af-bootstrap
set -euo pipefail
exec sudo -u corners bash -c 'set -a; . /etc/corners/secrets.env; set +a; cd /opt/corners/app && exec python3 -m collector --config /opt/corners/config.toml "$@"' _ "$@"
