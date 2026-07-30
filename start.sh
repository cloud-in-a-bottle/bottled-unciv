#!/bin/bash
# openhost-unciv supervisor.
#
# Runs two long-running processes and dies if either exits, so
# OpenHost restarts the container on any component failure:
#
#   1. UncivServer  — the game-save HTTP API, on loopback 127.0.0.1:8081.
#   2. auth_proxy   — the public seam on 0.0.0.0:8080 (health, owner
#                     landing page, transparent forward to UncivServer).
#
# UncivServer stores each multiplayer game as a file under its
# "MultiplayerFiles" folder.  We point that at the OpenHost persistent
# data dir so saves survive container restarts and redeploys.

set -euo pipefail

PERSIST="${OPENHOST_APP_DATA_DIR:-/data/app_data/unciv}"
FILES_DIR="$PERSIST/MultiplayerFiles"

mkdir -p "$FILES_DIR"

# UncivServer's per-user "auth v1" (each player may set a password that
# guards writes to their own save slot) is enabled by default.  Keep it
# on: the game API is publicly reachable, so this is the one guardrail a
# player has against someone else overwriting their game.  The
# UncivServerAuth env var is a BOOLEAN flag (true = auth enabled), not
# the CLI string; operators can force it off with UncivServerAuth=false
# for a fully open, no-password server for a trusted group.
export UncivServerAuth="${UncivServerAuth:-true}"

UPSTREAM_PORT="${AUTH_PROXY_UPSTREAM_PORT:-8081}"
LISTEN_PORT="${AUTH_PROXY_LISTEN_PORT:-8080}"

echo "[start] persistent files dir: $FILES_DIR"
echo "[start] UncivServer -> 127.0.0.1:${UPSTREAM_PORT}, proxy -> 0.0.0.0:${LISTEN_PORT}"

# 1. UncivServer on loopback.  -p port, -f files folder.
java -jar /opt/UncivServer.jar -p "${UPSTREAM_PORT}" -f "$FILES_DIR" &
UNCIV_PID=$!

# 2. Public auth/landing proxy.
python3 /opt/openhost-unciv/auth_proxy.py &
PROXY_PID=$!

# If either exits, tear the whole container down so OpenHost restarts it.
# `wait -n` returns when the first child exits; we then kill the other.
set +e
wait -n "$UNCIV_PID" "$PROXY_PID"
EXIT_CODE=$?
echo "[start] a child process exited (code ${EXIT_CODE}); shutting down"
kill "$UNCIV_PID" "$PROXY_PID" 2>/dev/null
wait 2>/dev/null
exit "${EXIT_CODE}"
