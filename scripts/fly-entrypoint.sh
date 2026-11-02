#!/bin/sh
# Fly.io volumes mount root-owned. Give /data to the app user, then drop
# root before starting Riff. The app itself never runs as root.
set -eu
mkdir -p /data/catalog
chown -R 10001:10001 /data
exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
