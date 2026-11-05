#!/usr/bin/env sh
# Self-signed certificate for trying the TLS profile locally. Use a real certificate (e.g. from your CA or
# Let's Encrypt) as fullchain.pem / privkey.pem for anything else.
set -eu
cd "$(dirname "$0")"
HOST="${1:-localhost}"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout privkey.pem -out fullchain.pem \
    -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST,DNS:localhost,IP:127.0.0.1"
chmod 600 privkey.pem
echo "Wrote deploy/tls/fullchain.pem and privkey.pem (valid 30 days, CN=$HOST)."
echo "Agents on other machines can trust it with OBSERVER_AGENT_TLS_CA=<copy of fullchain.pem>."
