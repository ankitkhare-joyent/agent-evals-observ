#!/usr/bin/env bash
# Generates the two pieces of key material OpenBao needs before its very first start.
# Both live in .bao/ (gitignored, owner-only). Safe to re-run: existing material is never overwritten,
# because regenerating the unseal key would make the existing OpenBao data unreadable.
#
#   .bao/bootstrap/unseal.key       32 random bytes: the static auto-unseal key (production: a KMS key instead)
#   .bao/bootstrap/admin-password   first password of the `admin` userpass user (rotate it after first login)
set -euo pipefail
cd "$(dirname "$0")/.."

umask 077
mkdir -p .bao/bootstrap .bao/approle

if [[ ! -s .bao/bootstrap/unseal.key ]]; then
  openssl rand -out .bao/bootstrap/unseal.key 32
  echo "created .bao/bootstrap/unseal.key"
fi
if [[ ! -s .bao/bootstrap/admin-password ]]; then
  openssl rand -hex 24 | tr -d '\n' > .bao/bootstrap/admin-password
  echo "created .bao/bootstrap/admin-password"
fi

# The OpenBao container runs as uid 100, not as you. Docker Desktop's file sharing ignores host
# ownership, but make the files readable inside the container on Linux hosts too.
chmod 0644 .bao/bootstrap/unseal.key .bao/bootstrap/admin-password
chmod 0755 .bao .bao/bootstrap
echo "OpenBao bootstrap material is in .bao/ (never commit it)"
