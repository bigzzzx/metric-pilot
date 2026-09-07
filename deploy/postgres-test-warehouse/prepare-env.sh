#!/bin/sh
set -eu

cd "$(dirname "$0")"
if [ -f .env ]; then
  chmod 600 .env
  exit 0
fi

umask 077
admin_password="$(openssl rand -hex 32)"
reader_password="$(openssl rand -hex 32)"
sed \
  -e "s/replace-with-a-random-secret/$admin_password/" \
  -e "s/replace-with-another-random-secret/$reader_password/" \
  .env.example > .env
chmod 600 .env
