#!/usr/bin/env bash
# Install (or update) the spotify-panel systemd service on the Raspberry Pi.
# Run from the repo:  bash deploy/install_panel.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! python3 -c "import flask" 2>/dev/null; then
    echo "Flask missing; installing python3-flask with apt..."
    sudo apt-get install -y python3-flask
fi

sudo cp deploy/spotify-panel.service /etc/systemd/system/spotify-panel.service
sudo systemctl daemon-reload
sudo systemctl enable spotify-panel
sudo systemctl restart spotify-panel
sleep 2
systemctl --no-pager --lines=5 status spotify-panel || true
echo "panel: http://$(hostname -I | awk '{print $1}'):5060"
