#!/usr/bin/env bash
# Bootstraps the Nexus relay server on a fresh Oracle Cloud Always Free
# Ubuntu VM. Run this ON THE VM after SSH'ing in, as the default 'ubuntu' user
# (it uses sudo where needed):
#
#   curl -fsSL https://raw.githubusercontent.com/AdithKB/nexus/main/deploy/oracle_setup.sh | bash
#
# or, after cloning manually:
#
#   bash AgenticPKMApp/deploy/oracle_setup.sh
#
# Before running, in the OCI Console you must have already:
#   1. Created the instance from an "Always Free eligible" shape
#      (VM.Standard.A1.Flex with <=4 OCPU/24GB, or VM.Standard.E2.1.Micro).
#      The console flags eligible shapes — don't pick a billable one.
#   2. Opened a Security List / Network Security Group ingress rule for
#      TCP 80 and 443 from 0.0.0.0/0 on the instance's VCN. This is a
#      cloud-level firewall separate from the VM's own iptables — the most
#      common reason "it's running but unreachable" on Oracle. This script
#      only handles the VM's local iptables, not the OCI console side.
#   3. Pointed nexus.adithkb.dev's DNS A record at this instance's public IP
#      (Caddy needs the DNS live before it can issue a TLS cert).
set -euo pipefail

REPO_DIR="$HOME/AgenticPKMApp"
REPO_URL="https://github.com/AdithKB/nexus.git"

echo "== 1/6: System packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl debian-keyring debian-archive-keyring apt-transport-https

echo "== 2/6: Caddy (official repo, gives automatic HTTPS) =="
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -y
    sudo apt-get install -y caddy
fi

echo "== 3/6: Local firewall (iptables) — open 80/443 =="
# Oracle's Ubuntu images ship with iptables DROP rules beyond SSH by default.
# This is separate from (and in addition to) the OCI Security List step above.
sudo iptables -C INPUT -p tcp --dport 80  -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 6 -p tcp --dport 80  -j ACCEPT
sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || sudo iptables-save | sudo tee /etc/iptables/rules.v4 >/dev/null || true

echo "== 4/6: Clone repo + venv =="
if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r deploy/requirements-server.txt

echo "== 5/6: .env =="
if [ ! -f "$REPO_DIR/.env" ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "  Created .env from .env.example — EDIT IT NOW with real keys:"
    echo "    nano $REPO_DIR/.env"
    echo "  Required for the full-cloud path: GEMINI_API_KEY, SPACES_ACCESS_KEY,"
    echo "  SPACES_SECRET_KEY, SPACES_BUCKET, SPACES_ENDPOINT_URL (if not DO Spaces)."
fi

echo "== 6/6: systemd + Caddy =="
sudo cp "$REPO_DIR/deploy/obsidian-capture.service" /etc/systemd/system/obsidian-capture.service
sudo cp "$REPO_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo systemctl enable obsidian-capture

echo
echo "Done. Next steps:"
echo "  1. Edit $REPO_DIR/.env with real API keys (if you haven't already)."
echo "  2. sudo systemctl restart obsidian-capture"
echo "  3. sudo systemctl status obsidian-capture   # check it started clean"
echo "  4. journalctl -u obsidian-capture -f          # tail logs"
echo "  5. curl -s http://127.0.0.1:8000/health        # sanity check, no DNS/TLS needed"
echo "  6. Once DNS has propagated: curl -s https://nexus.adithkb.dev/health"
