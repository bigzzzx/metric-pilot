#!/bin/sh
set -eu

cd "$(dirname "$0")"
chmod 755 init/00-reader-role.sh
chmod 700 prepare-env.sh verify.sh
./prepare-env.sh
docker compose config --quiet
docker compose up -d
