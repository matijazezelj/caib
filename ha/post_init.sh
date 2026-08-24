#!/bin/bash
# Patroni bootstrap post_init: runs once on the first leader, local trust
# connection URL in $1. The migrate entrypoint does everything else.
set -euo pipefail
psql "$1" -c "CREATE DATABASE caib"
